"""A co-simulation run driven from the Lab: background thread, pause/stop, live controls."""
from __future__ import annotations

import math
import threading
import time
import traceback
import uuid
from typing import Any

from .document import controls, project_from_graph


class RunSession:
    """Runs one project in a thread and keeps every row for streaming.

    `rows` holds ``[time, value...]`` in the order of `names` (numeric signals
    only). Readers poll `snapshot(cursor)`; the lock only guards list growth.
    """

    def __init__(self, graph: dict[str, Any], mock: bool = False, realtime: bool = False, speed: float = 1.0,
                 duration: float | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.graph = graph
        self.mock, self.realtime, self.speed = bool(mock), bool(realtime), max(float(speed or 1.0), 1e-3)
        self.duration = duration
        self.status = "starting"
        self.error: str | None = None
        self.log: list[str] = []
        self.names: list[str] = []
        self.units: dict[str, str] = {}
        self.rows: list[list[float | None]] = []
        self.dict_rows: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []        # UART lines and other adapter events, in order
        self.metrics: dict[str, Any] = {}
        self.wall_seconds = 0.0
        self.forced: dict[str, float] = controls(graph)
        self.injections: dict[str, dict[str, Any]] = {}  # frame sender: id -> {topic, payload, period, ...}
        self._lock = threading.Lock()
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._orchestrator = None
        self._thread = threading.Thread(target=self._run, name=f"embeddyn-lab-{self.id}", daemon=True)

    # control ---------------------------------------------------------------
    def start(self) -> "RunSession":
        self._thread.start()
        return self

    def pause(self) -> None:
        if self.status == "running":
            self._resume.clear()
            self.status = "paused"

    def resume(self) -> None:
        if self.status == "paused":
            self.status = "running"
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def set_speed(self, speed: float, realtime: bool | None = None) -> None:
        self.speed = max(float(speed), 1e-3)
        if realtime is not None:
            self.realtime = bool(realtime)

    def force(self, signal: str, value: float | None) -> None:
        if value is None:
            self.forced.pop(signal, None)
        else:
            if not math.isfinite(float(value)):
                raise ValueError("forced value must be finite")
            self.forced[signal] = float(value)

    @property
    def finished(self) -> bool:
        return self.status in {"done", "stopped", "error"}

    def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items = self.events[cursor:]
            return items, cursor + len(items)

    def uart_write(self, signal: str, text: str) -> dict[str, Any]:
        """Send text to the UART whose events go to `signal`. Logged as a `<signal>.tx` event."""
        orchestrator = self._orchestrator
        if orchestrator is None or self.finished:
            raise LookupError("the run is not active")
        for name, adapter in orchestrator.adapters.items():
            writer = getattr(adapter, "uart_write", None)
            if writer is None:
                continue
            try:
                count = writer(signal, text)
            except KeyError:
                continue
            with self._lock:
                self.events.append({"time_seconds": orchestrator.current_time, "topic": f"{signal}.tx",
                                    "payload": text, "source": "lab"})
            return {"instance": name, "bytes": count}
        raise LookupError(f"no UART publishes {signal}")

    def display_png(self, signal: str) -> bytes | None:
        """The latest frame of the display that publishes `signal` (a Renode `displays` binding), as PNG.

        Frames stay readable after the run ends; None until the firmware showed its first frame."""
        orchestrator = self._orchestrator
        if orchestrator is None:
            raise LookupError("the run has not started")
        for adapter in orchestrator.adapters.values():
            reader = getattr(adapter, "display_png", None)
            if reader is None:
                continue
            try:
                return reader(signal)
            except KeyError:
                continue
        raise LookupError(f"no display publishes {signal}")

    def inject(self, topic: str, payload: Any, period: float = 0.0, count: int = 0) -> dict[str, Any]:
        """Send an event into the running simulation (a CAN frame, a radio packet...).

        `period` > 0 repeats it every `period` seconds of simulated time (`count` times, 0 = until
        stopped); the returned id stops it (`stop_injection`)."""
        if self._orchestrator is None or self.finished:
            raise LookupError("the run is not active")
        if not topic:
            raise ValueError("a topic is needed")
        if period < 0 or (period and period < 1e-5):
            raise ValueError("period must be 0 (once) or at least 10 us")
        self._injection_count = getattr(self, "_injection_count", 0) + 1
        ident = f"i{self._injection_count}"
        with self._lock:
            self.injections[ident] = {"id": ident, "topic": topic, "payload": payload, "period": float(period),
                                      "left": int(count), "next": None, "sent": 0}
        return {"id": ident, "injections": self.injection_list()}

    def stop_injection(self, ident: str | None = None) -> dict[str, Any]:
        with self._lock:
            if ident:
                self.injections.pop(ident, None)
            else:
                self.injections.clear()
        return {"injections": self.injection_list()}

    def injection_list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in item.items() if k != "next"} for item in self.injections.values()]

    def _pump_injections(self, now: float) -> None:
        """Called in the run thread after each step: hand due injections to the orchestrator."""
        if not self.injections:
            return
        with self._lock:
            for ident, item in list(self.injections.items()):
                if item["next"] is None:
                    item["next"] = now
                if item["next"] > now + 1e-12:
                    continue
                payload = dict(item["payload"]) if isinstance(item["payload"], dict) else item["payload"]
                if isinstance(payload, dict):
                    payload.setdefault("src", "lab")
                self._orchestrator.inject(item["topic"], payload)
                item["sent"] += 1
                if item["period"] <= 0 or (item["left"] and item["sent"] >= item["left"]):
                    self.injections.pop(ident)
                else:
                    item["next"] += item["period"]

    def snapshot(self, cursor: int) -> tuple[list[list[float | None]], int]:
        with self._lock:
            rows = self.rows[cursor:]
            return rows, cursor + len(rows)

    def info(self) -> dict[str, Any]:
        return {"session": self.id, "status": self.status, "error": self.error, "names": self.names,
                "units": self.units, "rows": len(self.rows), "metrics": self.metrics,
                "wall_seconds": round(self.wall_seconds, 3), "forced": dict(self.forced),
                "speed": self.speed, "realtime": self.realtime, "log": self.log[-50:]}

    # worker ------------------------------------------------------------------
    def _emit(self, row: dict[str, Any]) -> None:
        values: list[float | None] = [float(row["time"])]
        for name in self.names:
            value = row.get(name)
            try:
                number = float(value)
                values.append(number if math.isfinite(number) else None)
            except (TypeError, ValueError):
                values.append(None)
        with self._lock:
            self.rows.append(values)
            self.dict_rows.append({k: v for k, v in row.items() if k != "__events__"})
            for event in row.get("__events__", []):
                self.events.append({k: event.get(k) for k in ("time_seconds", "topic", "payload", "source")})

    def _run(self) -> None:
        from ..factory import build_adapters
        from ..orchestrator import CoSimulationOrchestrator
        started = time.monotonic()
        generator = None
        try:
            project = project_from_graph(self.graph)
            if self.duration:
                project.duration_seconds = float(self.duration)
            from ..model import SignalKind
            self.names = [name for name, sig in project.signals.items()
                          if sig.kind in (SignalKind.CONTINUOUS, SignalKind.DISCRETE)]
            self.units = {name: signal.unit for name, signal in project.signals.items()}
            self.log.append(f"building adapters for {', '.join(project.simulator_instances()) or 'no instances'}"
                            + (" (mock hardware)" if self.mock else ""))
            adapters = build_adapters(project, mock=self.mock)
            orchestrator = CoSimulationOrchestrator(project, adapters)
            orchestrator.forced = self.forced            # shared dict: live controls apply at the next step
            self._orchestrator = orchestrator
            generator = orchestrator.iterate()
            self.log.append("starting simulators")
            t0 = None
            for row in generator:
                if t0 is None:
                    t0 = time.monotonic()
                    self.status = "running" if self._resume.is_set() else "paused"
                    self.log.append(f"running {project.duration_seconds:g} s at {project.step_seconds:g} s steps")
                self._emit(row)
                self._pump_injections(float(row["time"]))
                if self._stop.is_set():
                    break
                if not self._resume.is_set():
                    paused_at = time.monotonic()
                    self._resume.wait()
                    t0 += time.monotonic() - paused_at
                    if self._stop.is_set():
                        break
                if self.realtime:
                    ahead = float(row["time"]) / self.speed - (time.monotonic() - t0)
                    if ahead > 0:
                        time.sleep(min(ahead, 0.5))
            stopped = self._stop.is_set()
            generator.close()
            generator = None
            if project.metrics and not stopped:
                from ..metrics import compute_metrics, parse_metric_specs
                try:
                    self.metrics = compute_metrics(self.dict_rows, parse_metric_specs(project.metrics))
                except Exception as error:   # noqa: BLE001 - metrics are a bonus, keep the run
                    self.log.append(f"metrics: {error}")
            self.status = "stopped" if stopped else "done"
        except Exception as error:           # noqa: BLE001 - reported to the UI
            self.error = f"{type(error).__name__}: {error}"
            self.log.append(traceback.format_exc(limit=6))
            self.status = "error"
        finally:
            if generator is not None:
                try:
                    generator.close()
                except Exception:            # noqa: BLE001
                    pass
            self.wall_seconds = time.monotonic() - started
            self.log.append(f"{self.status} after {self.wall_seconds:.2f} s wall time")

    def csv(self) -> str:
        lines = [",".join(["time", *self.names])]
        with self._lock:
            for row in self.rows:
                lines.append(",".join("" if v is None else repr(v) for v in row))
        return "\n".join(lines) + "\n"


class CaptureSession:
    """A recorded capture (candump log, pcap, pcapng) opened in the analyzer, without a run.

    It answers what the analyzer endpoints ask of a `RunSession`: id, graph (for the
    decoder settings), events and status.
    """

    finished = True

    def __init__(self, graph: dict[str, Any], events: list[dict[str, Any]], path: str) -> None:
        self.id = "cap" + uuid.uuid4().hex[:9]
        self.graph = graph
        self.events = events
        self.path = path
        self.status = "done"
        self.injections: dict[str, Any] = {}

    def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        items = self.events[cursor:]
        return items, cursor + len(items)

    def info(self) -> dict[str, Any]:
        return {"session": self.id, "status": "done", "capture": self.path, "events": len(self.events)}

    def stop(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass

    def injection_list(self) -> list[dict[str, Any]]:
        return []
