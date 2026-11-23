"""Firmware-domain adapter driving a real Renode emulation.

The adapter talks to Renode's External Control server (custom-cores branch of
https://github.com/xsession/renode, ``src/Renode/Network/ExternalControl``) and
advances virtual time with ``RunFor`` once per co-simulation activation.  The
firmware itself is unmodified: values enter through emulated peripherals (ADC
channels, GPIO inputs, memory-mapped registers) and leave through GPIO pins
(PWM duty measured from edge timestamps) or register reads.

Binding manifest (``options.bindings``)::

    adapter: renode
    machine: stm32            # default machine for all bindings
    bindings:
      - signal: firmware.pwm_duty
        endpoint: {object: sysbus.gpioPortA, property: gpio_duty, pin: 8, direction: output}
      - signal: firmware.measured_velocity
        endpoint: {object: sysbus.adc1, property: adc, channel: 0, scale: 100.0, direction: input}

Output properties: ``gpio_duty``, ``gpio_pulse_width``, ``gpio_state``, ``sysbus``,
``register_ratio``.
Input properties: ``adc``, ``gpio``, ``sysbus``.  See docs/mechatronics/RENODE.md.

UARTs (``options.uarts``) are exposed by Renode as TCP socket terminals
(``CreateServerSocketTerminal`` + ``connector Connect``). After every step the
adapter drains what the firmware transmitted during that step and publishes
each complete line as an event on the UART's signal (topic). ``uart_write``
sends text to the firmware. That input crosses into the emulation
asynchronously, so it is a logged, non-deterministic boundary, unlike the
lock-step pin bindings::

    uarts:
      - {name: console, peripheral: sysbus.usart2, signal: mcu.console}   # port: 0 = pick a free one
"""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .base import SimulatorAdapter, StepResult
from .renode_api import GpioEvent, RenodeExternalControl


@dataclass
class _Uart:
    name: str
    peripheral: str
    signal: str
    port: int
    sock: socket.socket | None = None
    buffer: bytes = b""
    rx_bytes: int = 0
    tx_bytes: int = 0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

OUTPUT_PROPERTIES = {"gpio_duty", "gpio_pulse_width", "gpio_state", "sysbus", "register_ratio"}
INPUT_PROPERTIES = {"adc", "gpio", "sysbus"}
DEFAULT_RENODE_ARGS = ("--disable-gui", "--console", "--plain")
# Monitor messages that mean the startup script failed and the server will never open.
STARTUP_ERROR_MARKERS = ("There was an error executing command", "Could not find file", "Error E")


def _int(value: Any) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _float32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


@dataclass
class RenodeBinding:
    signal: str
    direction: str
    property: str
    peripheral: str
    machine: str
    options: dict[str, Any] = field(default_factory=dict)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass
class _DutyTracker:
    state: bool = False
    last_us: int = 0
    high_us: int = 0

    def on_event(self, event: GpioEvent, window_start: int, window_end: int) -> None:
        stamp = min(max(event.timestamp_us, window_start), window_end)
        if self.state:
            self.high_us += max(0, stamp - self.last_us)
        self.last_us = max(self.last_us, stamp)
        self.state = event.state


@dataclass
class _PulseTracker:
    """Width of the last complete high pulse, held until the next one ends,
    which is what an RC-servo or ESC input decoder sees."""
    state: bool = False
    rise_us: int | None = None
    last_fall_us: int = 0
    width_us: float = 0.0
    pulses: int = 0

    def on_event(self, event: GpioEvent) -> None:
        if event.state and not self.state:
            self.rise_us = event.timestamp_us
        elif self.state and not event.state:
            if self.rise_us is not None:
                self.width_us = float(event.timestamp_us - self.rise_us)
                self.pulses += 1
            self.last_fall_us = event.timestamp_us
        self.state = event.state

    def value_us(self, now_us: int, timeout_us: float | None) -> float:
        if timeout_us is not None and now_us - self.last_fall_us > timeout_us:
            return 0.0      # signal lost
        return self.width_us


def load_renode_bindings(path: Path | str | None, default_machine: str | None,
                         inline: list[Mapping[str, Any]] | None = None) -> list[RenodeBinding]:
    """Load Renode bindings from a manifest file and/or inline option list."""
    import yaml

    raw_items: list[Mapping[str, Any]] = []
    manifest_machine = default_machine
    if path:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        manifest_machine = default_machine or raw.get("machine")
        manifest_adapter = str(raw.get("adapter", "renode"))
        for item in raw.get("bindings", []):
            endpoint = dict(item.get("endpoint") or {})
            if str(endpoint.get("adapter", manifest_adapter)) != "renode":
                continue
            raw_items.append({"signal": item["signal"], **endpoint,
                              "direction": endpoint.get("direction", item.get("direction", "input"))})
    raw_items.extend(dict(item) for item in inline or [])

    bindings: list[RenodeBinding] = []
    errors: list[str] = []
    reserved = {"signal", "direction", "property", "object", "machine", "adapter", "unit"}
    for item in raw_items:
        signal = str(item.get("signal", ""))
        direction = str(item.get("direction", "input"))
        prop = str(item.get("property", ""))
        peripheral = str(item.get("object", ""))
        machine = str(item.get("machine") or manifest_machine or "")
        options = {k: v for k, v in item.items() if k not in reserved}
        allowed = OUTPUT_PROPERTIES if direction == "output" else INPUT_PROPERTIES if direction == "input" else set()
        if not signal:
            errors.append("binding without signal name")
        elif not allowed:
            errors.append(f"{signal}: direction must be 'input' or 'output', got {direction!r}")
        elif prop not in allowed:
            errors.append(f"{signal}: unsupported {direction} property {prop!r} (allowed: {', '.join(sorted(allowed))})")
        elif not machine:
            errors.append(f"{signal}: no Renode machine name (set 'machine' in the manifest or simulator options)")
        elif prop in {"gpio_duty", "gpio_pulse_width", "gpio_state", "gpio"} and ("pin" not in options or not peripheral):
            errors.append(f"{signal}: {prop} requires 'object' (GPIO peripheral) and 'pin'")
        elif prop == "adc" and ("channel" not in options or not peripheral):
            errors.append(f"{signal}: adc requires 'object' (ADC peripheral) and 'channel'")
        elif prop in {"sysbus", "register_ratio"} and "address" not in options:
            errors.append(f"{signal}: {prop} requires 'address'")
        elif prop == "register_ratio" and "denominator_address" not in options:
            errors.append(f"{signal}: register_ratio requires 'denominator_address'")
        else:
            bindings.append(RenodeBinding(signal, direction, prop, peripheral or "sysbus", machine, options))
    if errors:
        raise ValueError("invalid Renode bindings:\n  " + "\n  ".join(errors))
    return bindings


class RenodeAdapter(SimulatorAdapter):
    """Drive firmware running in Renode through the External Control API."""

    name = "renode"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 21234,
        timeout: float = 10.0,
        machine: str | None = None,
        bindings: str | None = None,
        inline_bindings: list[dict[str, Any]] | None = None,
        launch: bool = False,
        script: str | None = None,
        firmware: str | None = None,
        variables: dict[str, Any] | None = None,
        executable: str | None = None,
        renode_args: list[str] | None = None,
        server_name: str = "embeddyn",
        startup_timeout: float = 120.0,
        log_path: str | None = None,
        uarts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self.machine = machine
        self.bindings_path = bindings
        self.inline_bindings = inline_bindings or []
        self.launch = bool(launch)
        self.script = script
        self.firmware = firmware
        self.variables = dict(variables or {})
        self.executable = executable or os.environ.get("RENODE_BIN") or shutil.which("renode") or "renode"
        self.renode_args = list(renode_args) if renode_args is not None else list(DEFAULT_RENODE_ARGS)
        self.server_name = server_name
        self.startup_timeout = float(startup_timeout)
        self.log_path = log_path
        self.client: RenodeExternalControl | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self.bindings: list[RenodeBinding] = []
        self._origin_us = 0
        self._now_us = 0
        self._duty: dict[str, _DutyTracker] = {}
        self._pulses: dict[str, _PulseTracker] = {}
        self._window = (0, 0)
        self.uarts: list[_Uart] = []
        for spec in uarts or []:
            name, peripheral = str(spec["name"]), str(spec["peripheral"])
            if not name.isidentifier() or any(ch.isspace() or ch in ';"' for ch in peripheral):
                raise ValueError(f"invalid UART name/peripheral: {name!r} {peripheral!r}")
            port = int(spec.get("port") or 0) or (_free_port() if self.launch else 0)
            if not port:
                raise ValueError(f"UART {name}: give `port` when Renode is not launched by embeddyn")
            self.uarts.append(_Uart(name, peripheral, str(spec.get("signal") or f"uart.{name}"), port))

    # ------------------------------------------------------------------ lifecycle
    def prepare(self, context: Mapping[str, Any]) -> None:
        self.bindings = load_renode_bindings(self.bindings_path, self.machine, self.inline_bindings)

    def launch_command(self) -> list[str]:
        if not self.script:
            raise ValueError("Renode launch requires 'script' (a .resc that loads the machine and firmware)")
        script = Path(self.script).resolve()
        paths = [script] + ([Path(self.firmware).resolve()] if self.firmware else [])
        for path in paths:
            if any(ch.isspace() or ch in ';"' for ch in str(path)):
                raise ValueError(f"Renode path must not contain whitespace, ';' or quotes: {path}")
        prefix = f"$bin=@{paths[1]}; " if self.firmware else ""
        for key, value in self.variables.items():
            if not str(key).isidentifier() or any(ch in str(value) for ch in ';"\n'):
                raise ValueError(f"invalid Renode variable {key!r}={value!r}")
            prefix += f'${key}="{value}"; '
        monitor = f'{prefix}include @{script}; emulation CreateExternalControlServer "{self.server_name}" {self.port}'
        for uart in self.uarts:
            monitor += (f'; emulation CreateServerSocketTerminal {uart.port} "{uart.name}" false'
                        f'; connector Connect {uart.peripheral} {uart.name}')
        return [self.executable, *self.renode_args, "-e", monitor]

    def start(self, initial_signals: Mapping[str, float]) -> None:
        if not self.bindings:
            self.prepare({})
        try:
            self._start(initial_signals)
        except BaseException:
            self.stop()  # the manager only stops adapters whose start() succeeded
            raise

    def _start(self, initial_signals: Mapping[str, float]) -> None:
        if self.launch:
            self._launch()
        self.client = self._connect()
        self._origin_us = self._now_us = self.client.get_time_us()
        self._duty.clear()
        self._pulses.clear()
        for binding in self.bindings:
            if binding.property == "gpio_duty":
                pin = _int(binding.opt("pin"))
                tracker = _DutyTracker(state=self.client.gpio_get(binding.machine, binding.peripheral, pin),
                                       last_us=self._now_us)
                self._duty[binding.signal] = tracker
                self.client.gpio_subscribe(binding.machine, binding.peripheral, pin,
                                           lambda event, t=tracker: t.on_event(event, *self._window))
            elif binding.property == "gpio_pulse_width":
                pin = _int(binding.opt("pin"))
                pulse = _PulseTracker(state=self.client.gpio_get(binding.machine, binding.peripheral, pin),
                                      last_fall_us=self._now_us)
                self._pulses[binding.signal] = pulse
                self.client.gpio_subscribe(binding.machine, binding.peripheral, pin, pulse.on_event)
        for uart in self.uarts:
            uart.sock = socket.create_connection((self.host, uart.port), timeout=self.timeout)
            uart.sock.setblocking(False)
        self._apply_inputs(initial_signals)

    def _drain_uarts(self, now_seconds: float) -> list[dict[str, Any]]:
        """Bytes the firmware sent during the step, as one event per complete line."""
        events = []
        for uart in self.uarts:
            if uart.sock is None:
                continue
            while True:
                try:
                    chunk = uart.sock.recv(65536)
                except (BlockingIOError, InterruptedError):
                    break
                if not chunk:
                    break
                uart.buffer += chunk
                uart.rx_bytes += len(chunk)
            *lines, uart.buffer = uart.buffer.split(b"\n")
            for line in lines:
                text = line.rstrip(b"\r").decode("utf-8", errors="replace")
                events.append({"time_seconds": now_seconds, "topic": uart.signal, "payload": text})
        return events

    def uart_write(self, name_or_signal: str, text: str) -> int:
        """Send text to a UART (by name or signal). Asynchronous: lands at Renode's next sync point."""
        for uart in self.uarts:
            if name_or_signal in (uart.name, uart.signal) and uart.sock is not None:
                data = text.encode("utf-8")
                uart.sock.setblocking(True)
                try:
                    uart.sock.sendall(data)
                finally:
                    uart.sock.setblocking(False)
                uart.tx_bytes += len(data)
                return len(data)
        raise KeyError(f"no connected UART {name_or_signal!r}")

    def step(self, time_seconds: float, step_seconds: float, inputs: Mapping[str, float]) -> StepResult:
        if self.client is None:
            raise RuntimeError("Renode adapter is not started")
        # The scheduler passes the activation's target (end) time. Integer-microsecond
        # targets derived from absolute co-simulation time avoid float drift.
        target_us = self._origin_us + round(time_seconds * 1_000_000)
        interval = target_us - self._now_us
        if interval < 0:
            raise RuntimeError(f"Renode is ahead of the co-simulation ({self._now_us} us > {target_us} us)")
        self._apply_inputs(inputs)
        start_us = self._now_us
        for tracker in self._duty.values():
            tracker.high_us, tracker.last_us = 0, start_us
        self._window = (start_us, target_us)
        if interval:
            self.client.run_for(interval)
        self._now_us = target_us
        outputs = self._read_outputs(start_us, target_us)
        diagnostics: dict[str, Any] = {"renode_time_us": target_us, "run_for_us": interval}
        if self.uarts:
            diagnostics["events"] = self._drain_uarts((target_us - self._origin_us) / 1_000_000)
        return StepResult(outputs, diagnostics)

    def stop(self) -> None:
        for uart in self.uarts:
            if uart.sock is not None:
                uart.sock.close()
                uart.sock = None
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def capabilities(self) -> dict[str, bool]:
        return {"deterministic_step": True, "variable_step": True, "checkpoint": False,
                "real_time": False, "events": True, "buses": bool(self.uarts), "firmware_in_the_loop": True}

    def health(self) -> dict[str, object]:
        return {
            "name": self.name,
            "executable": shutil.which(self.executable) or self.executable,
            "endpoint": f"{self.host}:{self.port}",
            "launch": self.launch,
            "script": self.script,
            "firmware": self.firmware,
            "connected": self.client is not None,
            "process_running": self.process is not None and self.process.poll() is None,
            "bindings": len(self.bindings),
        }

    # ------------------------------------------------------------------ helpers
    def _launch(self) -> None:
        command = self.launch_command()
        if not self.log_path:
            # Always capture output so startup failures can be reported.
            handle, self.log_path = tempfile.mkstemp(prefix="embeddyn-renode-", suffix=".log")
            os.close(handle)
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(self.log_path, "wb")
        # stdin stays open: with --console Renode would quit on EOF.
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=self._log_handle,
                                        stderr=subprocess.STDOUT)

    def _startup_error(self) -> str | None:
        """Return the first Monitor error Renode logged while running the script."""
        if not self.log_path or not Path(self.log_path).exists():
            return None
        text = Path(self.log_path).read_text(encoding="utf-8", errors="replace")
        for marker in STARTUP_ERROR_MARKERS:
            index = text.find(marker)
            if index >= 0:
                lines = text[index:].splitlines()
                return " | ".join(line.strip() for line in lines[:2] if line.strip())
        return None

    def _connect(self) -> RenodeExternalControl:
        deadline = time.monotonic() + (self.startup_timeout if self.launch else self.timeout)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process is not None:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Renode exited with code {self.process.returncode} before accepting "
                                       f"connections (see {self.log_path})")
                error = self._startup_error()
                if error:
                    raise RuntimeError(f"Renode script failed: {error} (see {self.log_path})")
            try:
                return RenodeExternalControl.connect(self.host, self.port, timeout=self.timeout)
            except (ConnectionRefusedError, socket.timeout, OSError) as exc:
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"cannot reach Renode External Control server at {self.host}:{self.port}: {last_error}"
                           + (f" (see {self.log_path})" if self.log_path else ""))

    def _apply_inputs(self, inputs: Mapping[str, Any]) -> None:
        assert self.client is not None
        for binding in self.bindings:
            if binding.direction != "input" or binding.signal not in inputs:
                continue
            raw = float(inputs[binding.signal]) * float(binding.opt("scale", 1.0)) + float(binding.opt("offset", 0.0))
            if binding.property == "adc":
                raw = min(max(raw, float(binding.opt("min", 0))), float(binding.opt("max", 0xFFFFFFFF)))
                self.client.adc_set(binding.machine, binding.peripheral, _int(binding.opt("channel")), round(raw))
            elif binding.property == "gpio":
                level = raw >= float(binding.opt("threshold", 0.5))
                if binding.opt("invert", False):
                    level = not level
                self.client.gpio_set(binding.machine, binding.peripheral, _int(binding.opt("pin")), level)
            elif binding.property == "sysbus":
                value = _float32_bits(raw) if binding.opt("float32", False) else round(raw)
                width = 4 if binding.opt("float32", False) else int(binding.opt("width", 4))
                self.client.bus_write(binding.machine, _int(binding.opt("address")), value,
                                      width=width, context=binding.peripheral)

    def _read_outputs(self, start_us: int, end_us: int) -> dict[str, float]:
        assert self.client is not None
        outputs: dict[str, float] = {}
        for binding in self.bindings:
            if binding.direction != "output":
                continue
            scale, offset = float(binding.opt("scale", 1.0)), float(binding.opt("offset", 0.0))
            if binding.property == "gpio_duty":
                tracker = self._duty[binding.signal]
                if tracker.state:
                    tracker.high_us += max(0, end_us - tracker.last_us)
                tracker.last_us = end_us
                span = end_us - start_us
                value = (tracker.high_us / span) if span else float(tracker.state)
                if binding.opt("invert", False):
                    value = 1.0 - value
            elif binding.property == "gpio_pulse_width":
                # seconds (scale 1e6 for microseconds); `timeout` seconds without a pulse -> 0
                timeout = binding.opt("timeout")
                value = self._pulses[binding.signal].value_us(
                    end_us, None if timeout is None else float(timeout) * 1e6) * 1e-6
            elif binding.property == "gpio_state":
                value = float(self.client.gpio_get(binding.machine, binding.peripheral, _int(binding.opt("pin"))))
                if binding.opt("invert", False):
                    value = 1.0 - value
            elif binding.property == "sysbus" and binding.opt("float32", False):
                bits = self.client.bus_read(binding.machine, _int(binding.opt("address")), width=4,
                                            context=binding.peripheral)
                value = struct.unpack("<f", struct.pack("<I", bits))[0]
            elif binding.property == "sysbus":
                width = int(binding.opt("width", 4))
                value = float(self._read_register(binding, binding.opt("address"), width))
            else:  # register_ratio, e.g. timer CCR / (ARR + 1) -> PWM duty
                width = int(binding.opt("width", 4))
                numerator = self._read_register(binding, binding.opt("address"), width)
                denominator = self._read_register(binding, binding.opt("denominator_address"), width)
                denominator += float(binding.opt("denominator_offset", 0))
                value = numerator / denominator if denominator else 0.0
            outputs[binding.signal] = value * scale + offset
        return outputs

    def _read_register(self, binding: RenodeBinding, address: Any, width: int) -> int:
        assert self.client is not None
        value = self.client.bus_read(binding.machine, _int(address), width=width, context=binding.peripheral)
        mask = int(binding.opt("mask", 0))
        if mask:
            value &= mask
        if binding.opt("signed", False) and value & (1 << (8 * width - 1)):
            value -= 1 << (8 * width)
        return value


# Backwards-compatible name used by earlier factory/tests.
RenodeSocketAdapter = RenodeAdapter
