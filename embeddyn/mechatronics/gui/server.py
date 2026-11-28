"""HTTP server of the Lab (standard library only).

JSON API under ``/api``, live data as Server-Sent Events, the single-page app
from ``static/``. Binds to 127.0.0.1 by default: the API reads and writes
project files and launches simulators, so it is a local tool.
"""
from __future__ import annotations

import copy
import json
import mimetypes
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from . import breadboard
from .document import graph_from_raw, instance_ports, load_graph, project_from_graph, raw_from_graph, to_yaml
from .parts import EXAMPLES, instantiate_part, part_library
from .session import CaptureSession, RunSession

STATIC = Path(__file__).resolve().parent / "static"
_PATH_KEYS = ("script", "firmware", "bindings", "log")
_NETLIST_SUFFIXES = {".cir", ".sp", ".spice", ".net", ".ckt"}


def _netlist_path(body: dict[str, Any], key: str = "path") -> Path:
    path = Path(str(body.get(key) or "")).expanduser()
    if not str(body.get(key) or "") or path.suffix.lower() not in _NETLIST_SUFFIXES:
        raise ValueError(f"a SPICE netlist ({', '.join(sorted(_NETLIST_SUFFIXES))}) is needed")
    if not path.is_absolute():
        path = Path(str(body.get("root") or os.getcwd())) / path
    return path.resolve()


def _examples() -> list[dict[str, Any]]:
    result = []
    for path in sorted(EXAMPLES.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        description = str((raw.get("metadata") or {}).get("description", "")).strip()
        result.append({"name": str(raw.get("name", path.stem)), "file": path.name, "path": str(path),
                       "description": " ".join(description.split())[:240]})
    return result


def _rebase(graph: dict[str, Any], new_root: Path) -> None:
    """Keep relative model/script/firmware/bindings paths valid after Save As elsewhere."""
    old_root = Path(graph.get("root") or new_root)
    if old_root.resolve() == new_root.resolve():
        return

    def move(value: Any) -> Any:
        if not isinstance(value, str) or not value or os.path.isabs(value):
            return value
        try:
            return os.path.relpath((old_root / value).resolve(), new_root)
        except ValueError:                       # another drive on Windows
            return str((old_root / value).resolve())

    for node in graph.get("nodes") or []:
        spec = node.get("spec")
        if not spec:
            continue
        model = spec.get("model")
        if isinstance(model, str) and model and ":" not in model:
            spec["model"] = move(model)
        elif isinstance(model, str) and model.split(":")[0].endswith(".py"):
            location, cls = model.rsplit(":", 1)
            spec["model"] = f"{move(location)}:{cls}"
        options = spec.get("options") or {}
        for key in _PATH_KEYS:
            if key in options:
                options[key] = move(options[key])
    graph["root"] = str(new_root.resolve())


class LabState:
    def __init__(self, startup: Path | None = None) -> None:
        self.startup = startup
        self.sessions: dict[str, RunSession] = {}
        self.lock = threading.Lock()
        self.scan_images: set[str] = set()          # overlay pictures the Scan view may fetch

    def session(self, session_id: str) -> RunSession:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise LookupError(f"unknown run session {session_id}") from None

    def new_session(self, session: RunSession) -> RunSession:
        with self.lock:
            for old in list(self.sessions.values()):     # one live run at a time: simulators share ports
                if not old.finished:
                    old.stop()
            finished = [k for k, s in self.sessions.items() if s.finished]
            for key in finished[:-4]:
                self.sessions.pop(key, None)
            self.sessions[session.id] = session
        return session.start()

    def stop_all(self) -> None:
        for session in self.sessions.values():
            session.stop()
        for session in self.sessions.values():
            session.join(10)


def make_handler(state: LabState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EmbeddynLab/1.0"

        def log_message(self, format: str, *args: Any) -> None:        # quiet by default
            if os.environ.get("EMBEDDYN_LAB_VERBOSE"):
                super().log_message(format, *args)

        # helpers -------------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, allow_nan=False, default=str).encode(), "application/json")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 32 * 1024 * 1024:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _same_origin(self) -> bool:
            """Reject cross-site requests: another web page must not drive the local API."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return urlparse(origin).netloc == host

        # routing -------------------------------------------------------------
        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/stream":
                    return self._stream(state.session(query.get("session", "")), int(query.get("from", 0)))
                if url.path == "/api/state":
                    return self._json({"examples": _examples(), "parts": part_library(), "cwd": os.getcwd(),
                                       "startup": str(state.startup) if state.startup else None,
                                       "renode": bool(os.environ.get("RENODE_BIN"))})
                if url.path == "/api/session":
                    return self._json(state.session(query.get("session", "")).info())
                if url.path == "/api/analyzer":
                    return self._json(_analyzer_rows(state.session(query.get("session", "")), int(query.get("from", 0))))
                if url.path == "/api/buses":
                    return self._json(_bus_activity(state.session(query.get("session", ""))))
                if url.path == "/api/pcap":
                    session = state.session(query.get("session", ""))
                    return self._send(200, _pcap(session, query.get("proto", "can")), "application/vnd.tcpdump.pcap",
                                      {"Content-Disposition": f'attachment; filename="run-{session.id}-'
                                                              f'{query.get("proto", "can")}.pcap"'})
                if url.path == "/api/scan/image":
                    path = str(Path(query.get("path", "")).resolve())
                    if path not in state.scan_images or not Path(path).is_file():
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, Path(path).read_bytes(), "image/png")
                if url.path == "/api/csv":
                    session = state.session(query.get("session", ""))
                    return self._send(200, session.csv().encode(), "text/csv",
                                      {"Content-Disposition": f'attachment; filename="run-{session.id}.csv"'})
                return self._static(url.path)
            except LookupError as error:
                self._json({"error": str(error)}, 404)
            except Exception as error:                 # noqa: BLE001
                self._json({"error": f"{type(error).__name__}: {error}"}, 500)

        def do_POST(self) -> None:
            if not self._same_origin():
                return self._json({"error": "cross-origin request refused"}, 403)
            url = urlparse(self.path)
            try:
                body = self._body()
                handler = getattr(self, "_post_" + url.path.removeprefix("/api/").replace("/", "_"), None)
                if not url.path.startswith("/api/") or handler is None:
                    return self._json({"error": "not found"}, 404)
                return self._json(handler(body))
            except LookupError as error:
                self._json({"error": str(error)}, 404)
            except (ValueError, KeyError, TypeError, FileNotFoundError, yaml.YAMLError) as error:
                self._json({"error": f"{type(error).__name__}: {error}"}, 400)
            except Exception as error:                 # noqa: BLE001
                self._json({"error": f"{type(error).__name__}: {error}"}, 500)

        # API -----------------------------------------------------------------
        def _post_open(self, body: dict[str, Any]) -> dict[str, Any]:
            path = Path(str(body["path"])).expanduser()
            if not path.is_absolute():
                path = (Path(os.getcwd()) / path)
            if path.suffix not in {".yaml", ".yml"}:
                raise ValueError("open a .yaml project file")
            return load_graph(path)

        def _post_new(self, body: dict[str, Any]) -> dict[str, Any]:
            root = Path(str(body.get("root") or os.getcwd())).expanduser().resolve()
            graph = graph_from_raw({"name": body.get("name") or "untitled", "step_seconds": 0.001,
                                    "duration_seconds": 2.0, "signals": {}}, root)
            return graph

        def _post_instantiate(self, body: dict[str, Any]) -> dict[str, Any]:
            return instantiate_part(str(body["part"]), str(body["id"]), Path(str(body.get("root") or os.getcwd())),
                                    dict(body.get("signals") or {}))

        def _post_ports(self, body: dict[str, Any]) -> dict[str, Any]:
            spec = dict(body["spec"])
            return {"ports": instance_ports(spec, Path(str(body.get("root") or os.getcwd())),
                                            dict(body.get("signals") or {}), list(body.get("couplings") or []))}

        def _post_regraph(self, body: dict[str, Any]) -> dict[str, Any]:
            """The graph re-derived from its project document (after bus edits): bus nodes and wires follow
            the boards' event options; layout, view and the path are kept."""
            graph = body["graph"]
            path = Path(graph["path"]) if graph.get("path") else None
            fresh = graph_from_raw(raw_from_graph(graph), Path(str(graph.get("root") or os.getcwd())), path)
            fresh["path"] = graph.get("path")
            return {"graph": fresh}

        def _post_wiring(self, body: dict[str, Any]) -> dict[str, Any]:
            from .document import wiring_issues
            return {"issues": wiring_issues(body["graph"])}

        def _post_yaml(self, body: dict[str, Any]) -> dict[str, Any]:
            return {"yaml": to_yaml(raw_from_graph(body["graph"]))}

        def _post_yaml_parse(self, body: dict[str, Any]) -> dict[str, Any]:
            value = yaml.safe_load(str(body.get("text") or "")) or {}
            if not isinstance(value, dict):
                raise ValueError("expected a mapping")
            return {"value": value}

        def _post_yaml_dump(self, body: dict[str, Any]) -> dict[str, Any]:
            return {"text": to_yaml(body.get("value") or {})}

        def _post_validate(self, body: dict[str, Any]) -> dict[str, Any]:
            graph = body["graph"]
            errors: list[str] = []
            warnings: list[str] = []
            try:
                project = project_from_graph(graph)
                from ..bindings import validate_project_bindings
                errors += validate_project_bindings(project)["errors"]
            except Exception as error:                 # noqa: BLE001
                errors.append(f"{type(error).__name__}: {error}")
            for node in graph.get("nodes") or []:
                for port in node.get("ports") or []:
                    if node.get("kind") == "instance" and port["direction"] == "in" and not port.get("signal") \
                            and port.get("mappable"):
                        warnings.append(f"{node['id']}.{port['name']} is unconnected (plant default applies)")
                    if node.get("kind") in {"instrument", "scene"} and not port.get("signal") and port["name"] == "A":
                        warnings.append(f"{node['id']} has no signal on channel A")
            from .document import wiring_issues
            for issue in wiring_issues(graph):
                (errors if issue["severity"] == "error" else warnings).append(issue["text"])
            return {"ok": not errors, "errors": errors, "warnings": warnings}

        def _post_save(self, body: dict[str, Any]) -> dict[str, Any]:
            graph = body["graph"]
            path = Path(str(body.get("path") or graph.get("path") or "")).expanduser()
            if not str(path) or path.suffix not in {".yaml", ".yml"}:
                raise ValueError("save as a .yaml file")
            if not path.is_absolute():
                path = Path(graph.get("root") or os.getcwd()) / path
            path = path.resolve()
            _rebase(graph, path.parent)
            text = to_yaml(raw_from_graph(graph))
            project_from_graph(graph)                     # refuse to write an invalid project
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Saved by embeddyn Lab. Layout and instruments live in metadata.gui.\n" + text,
                            encoding="utf-8")
            return {"path": str(path), "graph": load_graph(path)}

        # breadboard view -----------------------------------------------------
        def _post_breadboard_circuits(self, body: dict[str, Any]) -> dict[str, Any]:
            return {"circuits": breadboard.circuit_instances(body["graph"]), "palette": breadboard.palette()}

        def _post_breadboard_open(self, body: dict[str, Any]) -> dict[str, Any]:
            return breadboard.open_board(_netlist_path(body))

        def _post_breadboard_layout(self, body: dict[str, Any]) -> dict[str, Any]:
            path = _netlist_path(body)
            result = breadboard.open_board(path)
            board = breadboard.auto_layout(breadboard.parse_netlist(result["netlist"]), name=path.stem)
            board["saved"] = False
            return {**result, "board": board, "check": breadboard.check_board(board)}

        def _post_breadboard_check(self, body: dict[str, Any]) -> dict[str, Any]:
            return breadboard.check_board(body["board"])

        def _post_breadboard_save(self, body: dict[str, Any]) -> dict[str, Any]:
            path = _netlist_path(body)
            if not path.exists():
                raise FileNotFoundError(str(path))
            return {"sidecar": str(breadboard.save_board(path, body["board"]))}

        def _post_breadboard_netlist(self, body: dict[str, Any]) -> dict[str, Any]:
            path = _netlist_path(body)
            original = path.read_text(encoding="utf-8") if path.exists() else None
            return {"text": breadboard.export_netlist(body["board"], original)}

        def _post_breadboard_export(self, body: dict[str, Any]) -> dict[str, Any]:
            path = _netlist_path(body)
            target = _netlist_path({"path": body.get("target") or str(path), "root": str(path.parent)})
            original = path.read_text(encoding="utf-8") if path.exists() else None
            target.write_text(breadboard.export_netlist(body["board"], original), encoding="utf-8")
            return {"path": str(target)}

        # scan view: printed schematics (PDF, pictures) to a netlist and an imported board -----------------
        def _post_scan_run(self, body: dict[str, Any]) -> dict[str, Any]:
            from .scanview import run_scan
            result = run_scan(body)
            with state.lock:
                state.scan_images.update(str(Path(p["overlay"]).resolve()) for p in result["pages"] if p["overlay"])
            return result

        def _post_scan_import(self, body: dict[str, Any]) -> dict[str, Any]:
            from .scanview import import_board
            return import_board(body)

        # analyzer: frame sender, captures, decoder settings ------------------------------------------
        def _post_analyzer_inject(self, body: dict[str, Any]) -> dict[str, Any]:
            session = state.session(str(body["session"]))
            payload = body.get("payload")
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError("payload must be an object with hex `data` (and `id` for CAN)")
            bytes.fromhex(str(payload["data"]))                      # refuse malformed hex here
            if payload.get("proto", "can") == "can":
                payload = {**payload, "proto": "can", "id": int(str(payload.get("id", 0)), 0)
                           if isinstance(payload.get("id"), str) else int(payload.get("id", 0))}
                if not 0 <= payload["id"] <= 0x1FFFFFFF or len(bytes.fromhex(payload["data"])) > 8:
                    raise ValueError("CAN id must be 0..0x1FFFFFFF and data at most 8 bytes")
            if not hasattr(session, "inject"):
                raise LookupError("captures cannot take frames: run the project")
            return session.inject(str(body.get("topic") or ""), payload, float(body.get("period_ms") or 0) / 1000.0,
                                  int(body.get("count") or 0))

        def _post_analyzer_inject_stop(self, body: dict[str, Any]) -> dict[str, Any]:
            session = state.session(str(body["session"]))
            if not hasattr(session, "stop_injection"):
                return {"injections": []}
            return session.stop_injection(body.get("id"))

        def _post_analyzer_medium(self, body: dict[str, Any]) -> dict[str, Any]:
            """Change a topic's impairments in the running simulation."""
            session = state.session(str(body["session"]))
            orchestrator = getattr(session, "_orchestrator", None)
            if orchestrator is None or session.finished:
                raise LookupError("the run is not active")
            spec = body.get("spec")
            if spec is not None and not isinstance(spec, dict):
                raise ValueError("spec must be an object or null")
            orchestrator.media.update(str(body.get("topic") or ""), spec)
            return {"media": orchestrator.media.specs()}

        def _post_analyzer_medium_check(self, body: dict[str, Any]) -> dict[str, Any]:
            """Link budget, cable and I2C checks for medium settings, without a run."""
            from ..comms.medium import Medium
            from ..comms.physical import RADIO, radio_link
            topic = str(body.get("topic") or "")
            spec = dict(body.get("spec") or {})
            medium = Medium(topic, spec)
            derived = medium.derived()
            link = spec.get("link")
            if link:
                proto = topic.split(".")[0]
                kinds = {"rfid": [("em4100", {"proto": "rfid", "air": "em4100"}), ("gen2 tag", {"proto": "rfid", "air": "gen2", "dir": "tag"})],
                         "lora": [(f"SF{sf}", {"proto": "lora", "sf": sf, "freq": 868.1}) for sf in (7, 9, 12)]}.get(
                    proto, [(proto, {"proto": proto if proto in RADIO else "ble", "channel": 6})])
                links = {}
                for name, sample in kinds:
                    for source, extra in [("", {}), *[(s, v) for s, v in (link.get("sources") or {}).items()]]:
                        budget = radio_link({**sample, "src": source}, link)
                        label = f"{source} {name}".strip() if source else name
                        links[label] = {k: budget[k] for k in ("distance_m", "rssi", "margin_db", "tag_power_dbm", "ber", "lost", "why")
                                        if k in budget}
                derived["links"] = links
            return {"derived": derived}

        def _post_analyzer_import(self, body: dict[str, Any]) -> dict[str, Any]:
            from ..comms.captures import read_capture
            path = Path(str(body.get("path") or "")).expanduser()
            if not str(body.get("path") or ""):
                raise ValueError("a capture path is needed")
            if not path.is_absolute():
                path = Path(str(body.get("root") or os.getcwd())) / path
            path = path.resolve()
            if path.suffix.lower() not in {".pcap", ".pcapng", ".cap", ".log", ".txt", ".candump", ".asc"}:
                raise ValueError("open a .pcap, .pcapng or candump .log capture")
            events = read_capture(path)
            graph = copy.deepcopy(dict(body.get("graph") or {}))
            if not body.get("canopen", True):                    # plain CAN: drop the CANopen decoder settings
                analyzer = ((graph.get("extra") or {}).get("metadata") or {}).get("analyzer")
                if isinstance(analyzer, dict):
                    analyzer.pop("canopen", None)
                    analyzer.pop("topics", None)
            else:
                analyzer = ((graph.get("extra") or {}).get("metadata") or {}).get("analyzer")
                if isinstance(analyzer, dict):
                    analyzer.pop("topics", None)                 # a capture has its own topics
            session = CaptureSession(graph, events, str(path))
            with state.lock:
                state.sessions[session.id] = session
            topics: dict[str, int] = {}
            for event in events:
                topics[event["topic"]] = topics.get(event["topic"], 0) + 1
            return {**session.info(), "topics": topics}

        def _post_analyzer_config(self, body: dict[str, Any]) -> dict[str, Any]:
            """New decoder settings for a run's analyzer (named fields...): decode it again."""
            session = state.session(str(body["session"]))
            config = dict(body.get("config") or {})
            graph = session.graph
            extra = graph.setdefault("extra", {})
            metadata = extra.setdefault("metadata", {})
            metadata["analyzer"] = config
            session.__dict__.pop("_analyzer", None)
            return {"ok": True}

        def _post_analyzer_dbc(self, body: dict[str, Any]) -> dict[str, Any]:
            from ..comms.analyzer import dbc
            return {"text": dbc(dict(body.get("config") or {}))}

        def _post_run(self, body: dict[str, Any]) -> dict[str, Any]:
            session = RunSession(body["graph"], mock=bool(body.get("mock")), realtime=bool(body.get("realtime")),
                                 speed=float(body.get("speed") or 1.0), duration=body.get("duration"))
            state.new_session(session)
            return session.info()

        def _post_control(self, body: dict[str, Any]) -> dict[str, Any]:
            session = state.session(str(body["session"]))
            action = body.get("action")
            if action == "pause":
                session.pause()
            elif action == "resume":
                session.resume()
            elif action == "stop":
                session.stop()
            elif action == "speed":
                session.set_speed(float(body["value"]), body.get("realtime"))
            else:
                raise ValueError(f"unknown action {action!r}")
            return session.info()

        def _post_uart(self, body: dict[str, Any]) -> dict[str, Any]:
            session = state.session(str(body["session"]))
            text = str(body.get("text", ""))
            if len(text) > 4096:
                raise ValueError("text too long")
            return session.uart_write(str(body["signal"]), text)

        def _post_force(self, body: dict[str, Any]) -> dict[str, Any]:
            session = state.session(str(body["session"]))
            value = body.get("value")
            session.force(str(body["signal"]), None if value is None else float(value))
            return {"forced": session.forced}

        # streaming and files -----------------------------------------------------
        def _stream(self, session: RunSession, cursor: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_status = None
            event_cursor = 0
            try:
                while True:
                    events, event_cursor = session.events_since(event_cursor)
                    if events:
                        self._event("events", {"events": events})
                    rows, cursor_next = session.snapshot(cursor)
                    if session.status != last_status or (rows and cursor == 0):
                        last_status = session.status
                        self._event("status", session.info())
                    for start in range(0, len(rows), 2000):
                        self._event("rows", {"from": cursor + start, "rows": rows[start:start + 2000]})
                    cursor = cursor_next
                    if session.finished and cursor >= len(session.rows):
                        events, event_cursor = session.events_since(event_cursor)
                        if events:
                            self._event("events", {"events": events})
                        self._event("status", session.info())
                        self._event("end", {"rows": cursor})
                        return
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _event(self, name: str, payload: Any) -> None:
            data = json.dumps(payload, allow_nan=False, default=str)
            self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
            self.wfile.flush()

        def _static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            file = (STATIC / relative).resolve()
            if STATIC not in file.parents or not file.is_file():
                return self._json({"error": "not found"}, 404)
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if file.suffix == ".js":
                content_type = "text/javascript"
            self._send(200, file.read_bytes(), content_type + ("; charset=utf-8" if content_type.startswith("text") else ""))

    return Handler


def _analyzer_config(session: RunSession) -> dict[str, Any]:
    metadata = ((session.graph.get("extra") or {}).get("metadata") or {})
    return dict(metadata.get("analyzer") or {})


def _analyzer_rows(session: RunSession, start: int) -> dict[str, Any]:
    """Decoded bus and radio packets of a run, from packet `start` on (decoded once, cached on the session)."""
    from ..comms.analyzer import decode_event
    config = _analyzer_config(session)
    topics = set(config.get("topics") or [])
    cache = session.__dict__.setdefault("_analyzer", {"rows": [], "cursor": 0, "lock": threading.Lock()})
    with cache["lock"]:
        return _decode_new(session, cache, config, topics, start, decode_event)


def _decode_new(session: RunSession, cache: dict[str, Any], config: dict[str, Any], topics: set[str], start: int,
                decode_event: Any) -> dict[str, Any]:
    status = session.info().get("status")                # before reading: a finished run has no more events
    events, cursor = session.events_since(cache["cursor"])
    # Keep the list in time order: hold back events that are newer than what the run has finished.
    events = sorted(cache.pop("held", []) + list(events), key=lambda event: float(event.get("time_seconds", 0.0)))
    if status not in {"done", "stopped", "error"} and events:
        horizon = float(events[-1].get("time_seconds", 0.0)) - 0.002
        cache["held"] = [e for e in events if float(e.get("time_seconds", 0.0)) > horizon]
        events = [e for e in events if float(e.get("time_seconds", 0.0)) <= horizon]
    for event in events:
        if topics and event.get("topic") not in topics:
            continue
        row = decode_event(event, config, cache.setdefault("context", {}))
        if row is not None:
            row["no"] = len(cache["rows"]) + 1
            row["source_instance"] = event.get("source")
            cache["rows"].append(row)
    cache["cursor"] = cursor
    return {"packets": cache["rows"][start:], "total": len(cache["rows"]), "status": status,
            "pending": len(cache.get("held", [])), "injections": session.injection_list(),
            "media": (session._orchestrator.media.specs() if getattr(session, "_orchestrator", None) is not None else None),
            "capture": getattr(session, "path", None)}


def _bus_activity(session: RunSession) -> dict[str, Any]:
    """Traffic per event topic of a run, for the schematic's bus nodes: frames, frames per second over the
    last 0.1 s of simulated time, frames the medium lost, error frames and frames it corrupted."""
    cache = session.__dict__.setdefault("_buses", {"cursor": 0, "topics": {}, "lock": threading.Lock()})
    with cache["lock"]:
        events, cursor = session.events_since(cache["cursor"])
        cache["cursor"] = cursor
        for event in events:
            topic = str(event.get("topic") or "")
            payload = event.get("payload")
            if not topic or not isinstance(payload, dict):
                continue
            t = float(event.get("time_seconds", 0.0))
            item = cache["topics"].setdefault(topic, {"frames": 0, "lost": 0, "errors": 0, "impaired": 0, "recent": [], "last": 0.0})
            item["frames"] += 1
            item["last"] = max(item["last"], t)
            medium = payload.get("medium") or {}
            item["lost"] += bool(medium.get("lost"))
            item["errors"] += bool(payload.get("error"))
            item["impaired"] += bool(medium) and not medium.get("lost") and not payload.get("error")
            item["recent"].append(t)
        window = 0.1
        out = {}
        last = max((i["last"] for i in cache["topics"].values()), default=0.0)
        for topic, item in cache["topics"].items():
            item["recent"] = [t for t in item["recent"] if t > last - window]
            out[topic] = {k: item[k] for k in ("frames", "lost", "errors", "impaired", "last")}
            out[topic]["rate_hz"] = round(len(item["recent"]) / window, 1)
    return {"status": session.info().get("status"), "topics": out}


def _pcap(session: RunSession, proto: str) -> bytes:
    import io
    from ..comms.analyzer import write_pcap
    config = _analyzer_config(session)
    topics = set(config.get("topics") or [])
    events = [e for e in session.events_since(0)[0] if not topics or e.get("topic") in topics]
    buffer = io.BytesIO()
    write_pcap(events, buffer, proto)
    return buffer.getvalue()


def serve(project: Path | None = None, host: str = "127.0.0.1", port: int = 8710) -> ThreadingHTTPServer:
    """Create the server (not yet serving). `serve_forever()` runs it."""
    state = LabState(project)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    server.lab_state = state                     # type: ignore[attr-defined]
    return server
