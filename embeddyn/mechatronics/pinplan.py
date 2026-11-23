"""Pin plan (CodeLayer Forge Pin Configurator) -> Renode co-simulation bindings.

A Forge pin plan assigns design signals to device pins. When the device file
also says which Renode peripheral drives each pin capability, the plan fully
determines the GPIO/PWM/ADC bindings of a firmware instance, so they are
generated instead of written by hand:

```json
// device pin (Pin Configurator device database, optional "renode" block)
{"name": "PA0", "capabilities": ["GPIO", "PWM"],
 "renode": {"GPIO": {"object": "sysbus.gpioPortA", "pin": 0},
            "PWM":  {"object": "sysbus.timer2", "pin": 0}}}

// plan signal (optional "cosim" block; defaults per capability below)
{"id": "servo_pulse", "requires": ["PWM"],
 "cosim": {"signal": "mcu.servo_pulse", "property": "gpio_pulse_width", "timeout": 0.05}}
```

Defaults per capability: GPIO -> `gpio_state` output, GPIO_IN -> `gpio`
input, PWM -> `gpio_duty` output, ADC -> `adc` input in microvolts. Bus
capabilities (UART/SPI/I2C/CAN) are reported as diagnostics until the Renode
adapter has bus bindings.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULTS: dict[str, dict[str, Any]] = {
    "GPIO": {"property": "gpio_state", "direction": "output"},
    "GPIO_OUT": {"property": "gpio_state", "direction": "output"},
    "GPIO_IN": {"property": "gpio", "direction": "input"},
    "PWM": {"property": "gpio_duty", "direction": "output"},
    "ADC": {"property": "adc", "direction": "input", "scale": 1.0e6, "unit": "V"},
}
BUS_CAPABILITIES = ("UART", "SPI", "I2C", "CAN", "LIN", "USB", "ETH")
_ENDPOINT_ORDER = ("object", "property", "pin", "channel", "address", "timeout", "scale", "min", "max",
                   "float32", "direction", "unit")


def _diagnostic(severity: str, code: str, message: str, signal: str | None = None) -> dict[str, Any]:
    item = {"severity": severity, "code": code, "message": message}
    if signal:
        item["signal"] = signal
    return item


def renode_bindings(plan: Mapping[str, Any], device: Mapping[str, Any], machine: str | None = None,
                    prefix: str = "mcu") -> dict[str, Any]:
    """Binding manifest (as in a bindings YAML file) for the assigned pins of `plan`.

    Returns ``{"adapter", "machine", "bindings", "diagnostics"}``. Signals whose
    pin has no Renode mapping for any required capability are skipped with an
    error diagnostic; nothing is guessed.
    """
    if int(plan.get("schemaVersion", 1)) != 1:
        raise ValueError("unsupported pin plan schemaVersion")
    if plan.get("device") and device.get("id") and plan["device"] != device["id"]:
        raise ValueError(f"pin plan is for device {plan['device']}, not {device['id']}")
    pins = {str(pin["name"]): pin for pin in device.get("pins", [])}
    assignments = dict(plan.get("assignments") or {})
    bindings: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen_signals: set[str] = set()
    for signal in plan.get("signals", []):
        signal_id = str(signal["id"])
        pin_name = assignments.get(signal_id)
        if not pin_name:
            diagnostics.append(_diagnostic("error", "unassigned", f"{signal_id} has no pin assignment", signal_id))
            continue
        pin = pins.get(str(pin_name))
        if pin is None:
            diagnostics.append(_diagnostic("error", "unknown-pin", f"{signal_id} is assigned to unknown pin {pin_name}",
                                           signal_id))
            continue
        requires = [str(c) for c in signal.get("requires", [])] or ["GPIO"]
        cosim = dict(signal.get("cosim") or {})
        renode = dict(pin.get("renode") or {})
        capability = next((c for c in requires if c in renode and c in DEFAULTS), None)
        if capability is None:
            bus = next((c for c in requires if c.split("_")[0] in BUS_CAPABILITIES), None)
            if bus:
                diagnostics.append(_diagnostic("warning", "bus-not-bindable",
                                               f"{signal_id} ({bus} on {pin_name}) is a bus signal; bind it in Renode "
                                               "(UART terminal, CAN bridge) instead of a pin binding", signal_id))
            else:
                diagnostics.append(_diagnostic("error", "no-renode-mapping",
                                               f"pin {pin_name} has no Renode mapping for {', '.join(requires)}",
                                               signal_id))
            continue
        endpoint: dict[str, Any] = {**DEFAULTS[capability], **dict(renode[capability])}
        name = str(cosim.pop("signal", f"{prefix}.{signal_id}"))
        endpoint.update(cosim)
        if name in seen_signals:
            diagnostics.append(_diagnostic("error", "duplicate-signal", f"signal {name} is bound twice", signal_id))
            continue
        seen_signals.add(name)
        ordered = {key: endpoint[key] for key in _ENDPOINT_ORDER if key in endpoint}
        ordered.update({k: v for k, v in endpoint.items() if k not in ordered})
        bindings.append({"signal": name, "endpoint": ordered, "pin": str(pin_name)})
    return {"adapter": "renode", "machine": machine or plan.get("machine"), "bindings": bindings,
            "diagnostics": diagnostics}


def merge_bindings(generated: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    """Generated pin bindings plus the entries of `existing` they don't replace
    (for example sysbus telemetry), keeping the existing machine name."""
    existing = dict(existing or {})
    pinned = {b["signal"] for b in generated["bindings"]}
    kept = [b for b in existing.get("bindings", []) if b.get("signal") not in pinned]
    return {"adapter": "renode", "machine": generated.get("machine") or existing.get("machine"),
            "bindings": [{"signal": b["signal"], "endpoint": b["endpoint"]} for b in generated["bindings"]] + kept}


def to_yaml(manifest: Mapping[str, Any], source: str = "") -> str:
    header = "# Generated from a CodeLayer Forge pin plan" + (f" ({source})" if source else "") + \
             " by embeddyn.mechatronics.pinplan.\n"
    body = {k: v for k, v in manifest.items() if k in {"adapter", "machine", "bindings"} and v is not None}
    body["bindings"] = [{"signal": b["signal"], "endpoint": b["endpoint"]} for b in body.get("bindings", [])]
    return header + yaml.safe_dump(body, sort_keys=False, width=120)


def load_json_or_yaml(path: Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text)                    # JSON is YAML
