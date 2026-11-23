"""Project document <-> schematic graph.

The graph is what the Lab edits:

```text
{name, path, root, step_seconds, duration_seconds, description,
 nodes:   [{id, kind, part, label, icon, x, y, domain, section, spec, ports, settings}],
 wires:   [{id, kind: net|coupling|probe, source: {node, port}, target: {node, port},
            transform, delay_steps, spec}],
 signals: {name: signal spec as in the project file},
 extra:   every other top-level section of the project file, kept verbatim}
```

* ``net``: an output signal read directly by a python/FMU input mapping
  (both ports carry the same signal).
* ``coupling``: an entry of ``couplings:`` (source signal -> target signal,
  with its transform).
* ``probe``: a signal shown by an instrument or 3-D body; no effect on the run.

Ports of instances are derived from the instance itself: plant inputs and
outputs, replay columns, binding files (Renode, Webots, ngspice) or, for
legacy mock domains, the domain's signals.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from ..model import MechatronicProject

_NODE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_COUPLING_KEYS = ("source", "target")
DOMAIN_COLUMNS = {"environment": 0, "firmware": 1, "electrical": 2, "mechanical": 3, "thermal": 4}


def valid_node_id(name: str) -> bool:
    return bool(_NODE_ID.match(name or ""))


def _unit(signals: dict[str, Any], name: str | None) -> str:
    spec = signals.get(name or "") or {}
    return str(spec.get("unit", "")) if isinstance(spec, dict) else ""


def _binding_ports(spec: dict[str, Any], root: Path, signals: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ports from a binding manifest (+ inline bindings), or None without one."""
    options = spec.get("options") or {}
    backend = str(options.get("backend", ""))
    path = options.get("bindings")
    inline = list(options.get("inline_bindings") or [])
    if not path and not inline:
        return None
    entries: list[tuple[str, str, str]] = []      # (signal, direction, label)
    file = (root / str(path)).resolve() if path else None
    if backend == "ngspice-shared":
        from ..adapters.ngspice_shared import load_ngspice_bindings
        try:
            bindings = load_ngspice_bindings(file if file and file.exists() else None, inline)
        except (OSError, ValueError):
            return None
        entries += [(b.signal, "in", f"{b.kind} {b.target}") for b in bindings.inputs]
        entries += [(b.signal, "out", b.vector) for b in bindings.outputs]
    else:
        raw_entries = []
        if file is not None and file.exists():
            raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            raw_entries += list(raw.get("bindings") or [])
        raw_entries += inline
        for item in raw_entries:
            if not isinstance(item, dict) or "signal" not in item:
                continue
            endpoint = item.get("endpoint") or item.get("target") or item.get("source") or item
            direction = str(endpoint.get("direction", item.get("direction", "input")))
            obj = str(endpoint.get("object", endpoint.get("node", endpoint.get("device", ""))))
            prop = str(endpoint.get("property", endpoint.get("field", "")))
            detail = next((f"{k} {endpoint[k]}" for k in ("channel", "pin", "address") if k in endpoint), "")
            label = " ".join(filter(None, [obj.removeprefix("sysbus.") if obj != "sysbus" else "sysbus", prop, detail]))
            for side in (("in", "out") if direction == "bidirectional" else
                         (("out",) if direction == "output" else ("in",))):
                entries.append((str(item["signal"]), side, label))
    unique = list(dict.fromkeys((signal, side) for signal, side, _ in entries))
    labels = {(signal, side): label for signal, side, label in reversed(entries)}
    both = {signal for signal, side in unique if side == "out"} & {signal for signal, side in unique if side == "in"}
    ports = [{"name": f"{signal}:in" if side == "in" and signal in both else signal, "direction": side,
              "signal": signal, "label": labels[(signal, side)], "unit": _unit(signals, signal), "mappable": False}
             for signal, side in unique]
    return ports


def instance_ports(spec: dict[str, Any], root: Path, signals: dict[str, Any],
                   couplings: list[dict[str, Any]], domain_fallback: str | None = None) -> list[dict[str, Any]]:
    """Ports of one simulator instance (see module docstring)."""
    options = spec.get("options") or {}
    backend = str(options.get("backend", ""))
    if backend in {"python", "fmu"}:
        inputs, outputs = dict(options.get("inputs") or {}), dict(options.get("outputs") or {})
        names_in, names_out = list(inputs), list(outputs)
        units: dict[str, str] = {}
        if backend == "python" and spec.get("model"):
            try:
                from ..adapters.python_model import load_plant_class
                from .parts import PORT_UNITS
                cls = load_plant_class(str(spec["model"]), root)
                names_in = [*getattr(cls, "inputs", ()), *[n for n in inputs if n not in getattr(cls, "inputs", ())]]
                names_out = [*getattr(cls, "outputs", ()), *[n for n in outputs if n not in getattr(cls, "outputs", ())]]
                units = PORT_UNITS.get(cls.__name__, {})
            except Exception:            # noqa: BLE001 - an unloadable model still shows its mappings
                pass
        ports = [{"name": n, "direction": "in", "signal": inputs.get(n), "mappable": True,
                  "unit": _unit(signals, inputs.get(n)) or units.get(n, "")} for n in names_in]
        ports += [{"name": n, "direction": "out", "signal": outputs.get(n), "mappable": True,
                   "unit": _unit(signals, outputs.get(n)) or units.get(n, "")} for n in names_out]
        return ports
    if backend == "mujoco":
        return _mujoco_ports(spec, root, signals)
    if backend == "replay":
        columns = dict(options.get("columns") or {})
        return [{"name": column, "direction": "out", "signal": signal, "mappable": False,
                 "unit": _unit(signals, signal)} for column, signal in columns.items()]
    bound = _binding_ports(spec, root, signals)
    uarts = [{"name": f"uart:{u.get('name')}", "direction": "out", "signal": u.get("signal") or f"uart.{u.get('name')}",
              "label": f"{str(u.get('peripheral', '')).removeprefix('sysbus.')} console", "unit": "", "mappable": False,
              "kind": "event"} for u in (options.get("uarts") or []) if isinstance(u, dict)]
    if bound is not None or uarts:
        return (bound or []) + uarts
    domain = str(spec.get("domain") or domain_fallback or "")
    targets = {c.get("target") for c in couplings}
    ports = []
    for name, sig in signals.items():
        if (sig or {}).get("domain", "environment") != domain:
            continue
        ports.append({"name": name, "direction": "in" if name in targets else "out", "signal": name,
                      "unit": _unit(signals, name), "mappable": False})
    return ports


def mujoco_addresses(model_file: Path) -> tuple[list[str], list[str]]:
    """Input and output addresses a MuJoCo model offers (see adapters/mujoco_adapter.py)."""
    import mujoco
    model = mujoco.MjModel.from_xml_path(str(model_file))
    obj = mujoco.mjtObj

    def names(kind, count):
        return [mujoco.mj_id2name(model, kind, i) for i in range(count)]

    scalar_joints = [j for i, j in enumerate(names(obj.mjOBJ_JOINT, model.njnt))
                     if j and model.jnt_type[i] in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)]
    actuators = [a for a in names(obj.mjOBJ_ACTUATOR, model.nu) if a]
    inputs = [f"ctrl:{a}" for a in actuators] + [f"qfrc:{j}" for j in scalar_joints]
    outputs = [f"{kind}:{j}" for j in scalar_joints for kind in ("qpos", "qvel", "qfrc_act")]
    for i, sensor in enumerate(names(obj.mjOBJ_SENSOR, model.nsensor)):
        if sensor:
            dim = int(model.sensor_dim[i])
            outputs += [f"sensor:{sensor}"] if dim == 1 else [f"sensor:{sensor}[{k}]" for k in range(dim)]
    outputs += [f"act:{a}" for a in actuators]
    return inputs, outputs


def _mujoco_ports(spec: dict[str, Any], root: Path, signals: dict[str, Any]) -> list[dict[str, Any]]:
    options = spec.get("options") or {}
    inputs, outputs = dict(options.get("inputs") or {}), dict(options.get("outputs") or {})
    names_in, names_out = list(inputs), list(outputs)
    if spec.get("model"):
        try:
            offered_in, offered_out = mujoco_addresses((root / str(spec["model"])).resolve())
            names_in = offered_in + [n for n in inputs if n not in offered_in]
            names_out = offered_out + [n for n in outputs if n not in offered_out]
        except Exception:            # noqa: BLE001 - mujoco missing or model invalid: show the mappings
            pass
    ports = [{"name": n, "direction": "in", "signal": inputs.get(n), "mappable": True,
              "unit": _unit(signals, inputs.get(n))} for n in names_in]
    ports += [{"name": n, "direction": "out", "signal": outputs.get(n), "mappable": True,
               "unit": _unit(signals, outputs.get(n))} for n in names_out]
    return ports


def _instances(raw: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(name, section, spec) of every declared instance, legacy domains included."""
    result = []
    for domain, spec in (raw.get("simulators") or {}).items():
        spec = dict(spec or {})
        spec.setdefault("domain", domain)
        result.append((str(domain), "simulators", spec))
    for name, spec in (raw.get("instances") or {}).items():
        result.append((str(name), "instances", dict(spec or {})))
    return result


def graph_from_raw(raw: dict[str, Any], root: Path, path: Path | None = None) -> dict[str, Any]:
    raw = copy.deepcopy(raw or {})
    root = Path(root).resolve()
    signals: dict[str, Any] = {name: dict(spec or {}) for name, spec in (raw.get("signals") or {}).items()}
    couplings: list[dict[str, Any]] = [dict(c) for c in (raw.get("couplings") or [])]
    metadata = dict(raw.get("metadata") or {})
    gui = dict(metadata.pop("gui", None) or {})
    layout: dict[str, Any] = dict(gui.get("layout") or {})
    nodes: list[dict[str, Any]] = []
    for name, section, spec in _instances(raw):
        backend = str((spec.get("options") or {}).get("backend", ""))
        part = {"python": "python", "renode": "renode_mcu", "replay": "replay_csv", "fmu": "fmu",
                "webots": "webots_world", "ngspice-shared": "ngspice_circuit"}.get(backend, backend or "simulator")
        icon = {"firmware": "mcu", "electrical": "circuit", "mechanical": "gear",
                "environment": "table", "thermal": "thermo"}.get(str(spec.get("domain")), "box")
        nodes.append({"id": name, "kind": "instance", "part": part, "label": name, "icon": icon,
                      "domain": str(spec.get("domain")), "section": section, "spec": spec,
                      "ports": instance_ports(spec, root, signals, couplings)})
    for item in gui.get("nodes") or []:              # sources, instruments, 3-D bodies
        item = dict(item)
        channels = dict(item.pop("channels", {}) or {})
        ports = [dict(port) for port in item.get("ports", [])]
        for port in ports:
            if port["name"] in channels:
                port["signal"] = channels[port["name"]]
        item["ports"] = ports
        nodes.append(item)
    # Couplings or probes may name signals no node exposes: give them terminals.
    exposed = {(port["signal"], port["direction"]) for node in nodes for port in node["ports"] if port.get("signal")}
    wanted = [(c["source"], "out") for c in couplings] + [(c["target"], "in") for c in couplings]
    for signal, side in wanted:
        if (signal, side) not in exposed:
            nodes.append({"id": f"net_{len(nodes)}", "kind": "terminal", "part": "terminal", "label": signal,
                          "icon": "net", "ports": [{"name": signal, "direction": side, "signal": signal,
                                                    "unit": _unit(signals, signal), "mappable": False}]})
            exposed.add((signal, side))
    _place(nodes, layout)
    wires = _wires(nodes, couplings)
    extra = {k: v for k, v in raw.items()
             if k not in {"name", "step_seconds", "duration_seconds", "signals", "couplings", "instances", "simulators"}}
    extra["metadata"] = metadata
    description = metadata.get("description", "")
    return {
        "name": str(raw.get("name", path.stem if path else "untitled")),
        "path": str(path) if path else None,
        "root": str(root),
        "step_seconds": float(raw.get("step_seconds", 0.001)),
        "duration_seconds": float(raw.get("duration_seconds", 1.0)),
        "description": str(description).strip(),
        "nodes": nodes, "wires": wires, "signals": signals, "extra": extra,
        "view": dict(gui.get("view") or {}),
    }


def _place(nodes: list[dict[str, Any]], layout: dict[str, Any]) -> None:
    """Saved positions, else columns by role: sources | firmware | electrical | mechanical | observers."""
    rows: dict[int, int] = {}
    for node in nodes:
        saved = layout.get(node["id"])
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            node["x"], node["y"] = float(saved[0]), float(saved[1])
            continue
        if "x" in node and "y" in node:
            continue
        if node["kind"] in {"source", "terminal"}:
            column = 0
        elif node["kind"] in {"instrument", "scene"}:
            column = 5
        else:
            column = DOMAIN_COLUMNS.get(str(node.get("domain")), 2)
        row = rows.get(column, 0)
        height = 60 + 22 * max(sum(p["direction"] == "in" for p in node["ports"]),
                               sum(p["direction"] == "out" for p in node["ports"]))
        node["x"], node["y"] = 40 + column * 300, 40 + row
        rows[column] = row + height + 40


def _wires(nodes: list[dict[str, Any]], couplings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    producers: dict[str, tuple[str, str]] = {}
    consumers: dict[str, list[tuple[str, str]]] = {}
    for node in nodes:
        for port in node["ports"]:
            if not port.get("signal"):
                continue
            if port["direction"] == "out":
                producers.setdefault(port["signal"], (node["id"], port["name"]))
            else:
                consumers.setdefault(port["signal"], []).append((node["id"], port["name"]))
    wires: list[dict[str, Any]] = []
    kinds = {node["id"]: node["kind"] for node in nodes}
    for signal, readers in consumers.items():
        if signal not in producers:
            continue
        source = producers[signal]
        for reader in readers:
            if reader[0] == source[0]:
                continue
            kind = "probe" if kinds[reader[0]] in {"instrument", "scene"} else "net"
            wires.append({"id": f"w{len(wires)}", "kind": kind, "signal": signal,
                          "source": {"node": source[0], "port": source[1]},
                          "target": {"node": reader[0], "port": reader[1]}})
    for coupling in couplings:
        source = producers.get(coupling["source"])
        target = next(iter(consumers.get(coupling["target"], [])), None)
        if source is None or target is None:
            continue
        spec = {k: v for k, v in coupling.items() if k not in _COUPLING_KEYS}
        wires.append({"id": f"w{len(wires)}", "kind": "coupling", "signal": coupling["source"],
                      "target_signal": coupling["target"],
                      "source": {"node": source[0], "port": source[1]},
                      "target": {"node": target[0], "port": target[1]}, "spec": spec})
    return wires


def raw_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """The project document for a graph (inverse of `graph_from_raw`)."""
    extra = copy.deepcopy(graph.get("extra") or {})
    metadata = dict(extra.pop("metadata", {}) or {})
    if graph.get("description"):
        metadata["description"] = graph["description"]
    raw: dict[str, Any] = {"name": graph.get("name") or "untitled",
                           "step_seconds": float(graph.get("step_seconds", 0.001)),
                           "duration_seconds": float(graph.get("duration_seconds", 1.0))}
    nodes = list(graph.get("nodes") or [])
    simulators = {n["id"]: copy.deepcopy(n["spec"]) for n in nodes
                  if n.get("kind") == "instance" and n.get("section") == "simulators"}
    for spec in simulators.values():
        spec.pop("domain", None)
    instances = {n["id"]: copy.deepcopy(n["spec"]) for n in nodes
                 if n.get("kind") == "instance" and n.get("section") != "simulators"}
    if simulators:
        raw["simulators"] = simulators
    if instances:
        raw["instances"] = instances
    raw["signals"] = copy.deepcopy(graph.get("signals") or {})
    couplings = []
    for wire in graph.get("wires") or []:
        if wire.get("kind") != "coupling":
            continue
        couplings.append({"source": wire["signal"], "target": wire["target_signal"], **(wire.get("spec") or {})})
    if couplings:
        raw["couplings"] = couplings
    raw.update(extra)
    gui_nodes = []
    for node in nodes:
        if node.get("kind") not in {"source", "instrument", "scene"}:
            continue
        gui_nodes.append({
            "id": node["id"], "kind": node["kind"], "part": node.get("part"), "label": node.get("label", node["id"]),
            "icon": node.get("icon"), "settings": node.get("settings", {}),
            "ports": [{k: v for k, v in port.items() if k != "signal"} for port in node["ports"]],
            "channels": {port["name"]: port["signal"] for port in node["ports"] if port.get("signal")},
        })
    metadata["gui"] = {
        "layout": {node["id"]: [round(float(node.get("x", 0)), 1), round(float(node.get("y", 0)), 1)]
                   for node in nodes if node.get("kind") != "terminal"},
        "nodes": gui_nodes,
        **({"view": graph["view"]} if graph.get("view") else {}),
    }
    raw["metadata"] = metadata
    return raw


def to_yaml(raw: dict[str, Any]) -> str:
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110)


def load_graph(path: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return graph_from_raw(raw, path.parent, path)


def project_from_graph(graph: dict[str, Any]) -> MechatronicProject:
    root = Path(graph.get("root") or ".")
    return MechatronicProject.from_mapping(raw_from_graph(graph), root, graph.get("name") or "untitled")


def controls(graph: dict[str, Any]) -> dict[str, float]:
    """Live control values (sources) keyed by signal."""
    result = {}
    for node in graph.get("nodes") or []:
        if node.get("kind") == "source" and node.get("ports"):
            signal = node["ports"][0].get("signal")
            if signal:
                result[signal] = float((node.get("settings") or {}).get("value", 0.0))
    return result
