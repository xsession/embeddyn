from __future__ import annotations

import csv
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .adapters import SimulatorAdapter
from .model import Coupling, Domain, MechatronicProject, SignalKind
from .runtime import AdapterManager, CheckpointManager, EventQueue, InstanceCounter, InstanceMap, ScheduleEntry, build_scheduler
from .storage.telemetry import StructuredRecordWriter, TelemetryWriter


class CoSimulationOrchestrator:
    """Deterministic co-simulation coordinator with pluggable scheduling.

    Phase 3 adds a real multi-rate activation plan, typed event collection,
    adapter lifecycle management, and JSON checkpoints while preserving the
    Phase 1/2 API.
    """

    def __init__(self, project: MechatronicProject, adapters: Mapping[Any, SimulatorAdapter]) -> None:
        self.project = project
        self.adapters = InstanceMap(adapters)
        self.entries = self._schedule_entries()
        self.instance_domains = {entry.name: entry.domain for entry in self.entries}
        self.manager = AdapterManager(self.adapters, self.instance_domains)
        self.scheduler = build_scheduler(project.scheduler, self.entries)
        self.events = EventQueue()
        self.values: dict[str, Any] = {name: signal.initial for name, signal in project.signals.items()}
        self.history: dict[str, deque[float]] = {
            name: deque([float(signal.initial)], maxlen=self._max_delay_for(name) + 2)
            for name, signal in project.signals.items()
            if signal.kind in {SignalKind.CONTINUOUS, SignalKind.DISCRETE}
        }
        self.by_target_domain: dict[Domain, list[Coupling]] = defaultdict(list)
        for coupling in project.couplings:
            self.by_target_domain[project.signals[coupling.target].domain].append(coupling)
        self.current_time = 0.0
        self.activation_count: InstanceCounter = InstanceCounter()
        # Signals held at a fixed value (live operator controls in the GUI).
        self.forced: dict[str, float] = {}

    def _schedule_entries(self) -> list[ScheduleEntry]:
        """Configured instances, adapters given without configuration (named
        after their domain), and a coupling-only slot for every domain that has
        no instance, so couplings into it are still applied each step."""
        entries: dict[str, ScheduleEntry] = {}
        for name, config in self.project.simulator_instances().items():
            if config.enabled and config.domain is not None:
                entries[name] = ScheduleEntry(name, config.domain, config.step_seconds)
        for name in self.adapters:
            if name not in entries:
                try:
                    entries[name] = ScheduleEntry(name, Domain(name))
                except ValueError as error:
                    raise ValueError(f"adapter {name!r} has no simulator instance in the project") from error
        covered = {entry.domain for entry in entries.values()}
        for domain in Domain:
            if domain not in covered and domain.value not in entries:
                entries[domain.value] = ScheduleEntry(domain.value, domain)
        return list(entries.values())

    def run(
        self,
        output_csv: Path | None = None,
        telemetry_writer: TelemetryWriter | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        checkpoint_interval_seconds: float | None = None,
        resume_from: Path | None = None,
        event_writer: StructuredRecordWriter | None = None,
        bus_writer: StructuredRecordWriter | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.iterate(telemetry_writer=telemetry_writer, checkpoint_manager=checkpoint_manager,
                                 checkpoint_interval_seconds=checkpoint_interval_seconds, resume_from=resume_from,
                                 event_writer=event_writer, bus_writer=bus_writer))
        if output_csv:
            self.write_csv(output_csv, rows)
        return rows

    def iterate(
        self,
        telemetry_writer: TelemetryWriter | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        checkpoint_interval_seconds: float | None = None,
        resume_from: Path | None = None,
        event_writer: StructuredRecordWriter | None = None,
        bus_writer: StructuredRecordWriter | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run the co-simulation one macro step at a time, yielding the initial
        row and then one row per macro step. Closing the generator early stops
        every adapter, so an interactive front end can stop a run at any step.
        Signals in `self.forced` are held at their forced value throughout."""
        self._event_writer = event_writer
        self._bus_writer = bus_writer
        self.manager.start_all(self._adapter_inputs())
        try:
            if resume_from is not None:
                self.restore(CheckpointManager.load(resume_from))
            macro = self.project.step_seconds
            # Integer step index: the time of step k is k * macro, so it never accumulates rounding error
            # and a run resumed from a checkpoint lands on exactly the same times as an uninterrupted one.
            step_index = int(round(self.current_time / macro))
            macro_count = max(0, int(math.ceil((self.project.duration_seconds - self.current_time) / macro - 1e-9)))
            next_checkpoint = (
                self.current_time + checkpoint_interval_seconds
                if checkpoint_manager is not None and checkpoint_interval_seconds and checkpoint_interval_seconds > 0
                else None
            )
            self._apply_forced()
            if telemetry_writer is not None:
                telemetry_writer.append(self._telemetry_row(self.current_time))
            yield self._row(self.current_time)
            for _ in range(macro_count):
                macro_start = self.current_time
                step_index += 1
                macro_end = min(step_index * macro, self.project.duration_seconds)
                self._apply_forced()
                for activation in self.scheduler.activations(macro_start, macro_end):
                    self._activate(activation.instance or activation.domain.value, activation.domain,
                                   activation.time_seconds, activation.step_seconds)
                self.current_time = macro_end
                self._commit_history()
                if telemetry_writer is not None:
                    telemetry_writer.append(self._telemetry_row(self.current_time))
                if next_checkpoint is not None and self.current_time + 1e-15 >= next_checkpoint:
                    checkpoint_manager.save(self.current_time, self.snapshot())
                    next_checkpoint += checkpoint_interval_seconds
                yield self._row(self.current_time)
                if self.current_time >= self.project.duration_seconds - 1e-15:
                    break
        finally:
            self.manager.stop_all()
            if telemetry_writer is not None:
                telemetry_writer.close()
            if event_writer is not None:
                event_writer.close()
            if bus_writer is not None:
                bus_writer.close()

    def _apply_forced(self) -> None:
        for name, value in list(self.forced.items()):
            signal = self.project.signals.get(name)
            if signal is not None:
                self.values[name] = signal.clamp(float(value))

    def _activate(self, instance: str, domain: Domain, now: float, step_seconds: float) -> None:
        self._apply_couplings_iterative(domain)
        if self.forced:
            self._apply_forced()
        adapter = self.adapters.get(instance)
        if adapter is None:
            return
        result = adapter.step(now, step_seconds, self._adapter_inputs())
        self.activation_count[instance] += 1
        for name, value in result.outputs.items():
            signal = self.project.signals.get(name)
            if signal is None:
                self.values[name] = value
            elif signal.kind is SignalKind.EVENT:
                if value is not None:
                    event = self.events.publish(now, name, value, adapter.name)
                    writer = getattr(self, "_event_writer", None)
                    if writer is not None:
                        writer.append({"time_seconds": event.time_seconds, "topic": event.topic, "payload": event.payload, "source": event.source})
            elif signal.kind is SignalKind.BUS:
                self.values[name] = value
                event = self.events.publish(now, name, value, adapter.name)
                writer = getattr(self, "_bus_writer", None)
                if writer is not None:
                    writer.append({"time_seconds": event.time_seconds, "topic": event.topic, "payload": event.payload, "source": event.source})
            else:
                self.values[name] = signal.clamp(float(value))
        if self.forced:
            self._apply_forced()
        diagnostics = result.diagnostics or {}
        for event in diagnostics.get("events", []):
            published = self.events.publish(float(event.get("time_seconds", now)), str(event["topic"]), event.get("payload"), adapter.name)
            writer = getattr(self, "_event_writer", None)
            if writer is not None:
                writer.append({"time_seconds": published.time_seconds, "topic": published.topic, "payload": published.payload, "source": published.source})

    def _adapter_inputs(self) -> dict[str, Any]:
        parameters = {name: quantity.value for name, quantity in self.project.parameters.items()}
        return {**parameters, **self.values}

    def _telemetry_row(self, now: float) -> dict[str, float]:
        row: dict[str, float] = {"time": float(now)}
        for name, signal in self.project.signals.items():
            if signal.kind in {SignalKind.CONTINUOUS, SignalKind.DISCRETE}:
                row[name] = float(self.values[name])
        return row

    def _row(self, now: float) -> dict[str, Any]:
        row = {"time": now, **self.values}
        due = self.events.drain_until(now)
        if due:
            row["__events__"] = [
                {"time_seconds": e.time_seconds, "topic": e.topic, "payload": e.payload, "source": e.source}
                for e in due
            ]
        return row


    def _apply_couplings_iterative(self, target_domain: Domain) -> None:
        config = self.project.scheduler.convergence
        if not config.enabled:
            self._apply_couplings(target_domain)
            return
        couplings = self.by_target_domain.get(target_domain, [])
        if not couplings:
            return
        converged = False
        for iteration in range(1, config.max_iterations + 1):
            max_delta = 0.0
            for coupling in couplings:
                before = float(self.values[coupling.target])
                source = self._delayed(coupling.source, coupling.delay_steps)
                after = self.project.signals[coupling.target].clamp(coupling.transform.apply(source))
                self.values[coupling.target] = after
                max_delta = max(max_delta, abs(after - before))
            if max_delta <= config.tolerance:
                converged = True
                break
        self.last_convergence = {"domain": target_domain.value, "iterations": iteration,
                                 "converged": converged, "max_delta": max_delta}
        if not converged and config.failure_policy == "error":
            raise RuntimeError(f"coupling convergence failed for {target_domain.value}: delta={max_delta}")

    def _apply_couplings(self, target_domain: Domain) -> None:
        for coupling in self.by_target_domain.get(target_domain, []):
            source = self._delayed(coupling.source, coupling.delay_steps)
            value = coupling.transform.apply(source)
            signal = self.project.signals[coupling.target]
            self.values[coupling.target] = signal.clamp(value)

    def _delayed(self, signal: str, delay: int) -> float:
        if delay <= 0:
            return float(self.values[signal])
        history = self.history.get(signal)
        if history is None:
            raise ValueError(f"signal {signal} does not support delayed coupling history")
        if len(history) <= delay:
            return history[0]
        return list(history)[-1 - delay]

    def _commit_history(self) -> None:
        for name, history in self.history.items():
            history.append(float(self.values[name]))

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "time_seconds": self.current_time,
            "values": dict(self.values),
            "history": {name: list(values) for name, values in self.history.items()},
            "events": self.events.snapshot(),
            "adapters": self.manager.checkpoint(),
            "activation_count": dict(self.activation_count),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if int(state.get("version", 0)) != 1:
            raise ValueError("unsupported checkpoint version")
        self.current_time = float(state["time_seconds"])
        if not math.isfinite(self.current_time) or self.current_time < 0 or self.current_time > self.project.duration_seconds + 1e-12:
            raise ValueError("checkpoint time is outside the project duration")
        restored_values = dict(state["values"])
        unknown = set(restored_values) - set(self.values)
        if unknown:
            raise ValueError(f"checkpoint contains unknown signals: {', '.join(sorted(unknown))}")
        self.values = {name: restored_values.get(name, current) for name, current in self.values.items()}
        for name, items in dict(state.get("history", {})).items():
            if name in self.history:
                self.history[name].clear()
                self.history[name].extend(float(item) for item in items)
        self.events.restore(list(state.get("events", [])))
        self.manager.restore(dict(state.get("adapters", {})))
        self.activation_count = InstanceCounter({
            name: int(count) for name, count in dict(state.get("activation_count", {})).items()
        })

    def _max_delay_for(self, signal: str) -> int:
        return max((c.delay_steps for c in self.project.couplings if c.source == signal), default=0)

    @staticmethod
    def write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = list(rows)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        fields = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
