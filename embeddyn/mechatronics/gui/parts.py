"""Parts library of the Lab: simulator instances, live controls, instruments and 3-D bodies.

A part is a template. `instantiate_part` turns it into a schematic node with
ports and the project signals it owns. Four kinds exist:

* ``instance``: a co-simulation instance (python plant, Renode MCU, ngspice
  circuit, Webots world, FMU, CSV replay). Saved under ``instances:``.
* ``source``: a live operator control (slider, switch). Its signal is held at
  the control's value during a run (`CoSimulationOrchestrator.forced`).
* ``instrument``: an observer wired to signals (oscilloscope, meter, logic
  analyser, X-Y plotter, gauge, LED). It never changes the simulation.
* ``scene``: a body in the 3-D view whose pose follows signals.

Only ``instance`` parts reach the simulator; the other kinds, and the layout,
live in ``metadata.gui`` of the project file.
"""
from __future__ import annotations

import copy
import inspect
import os
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "mechatronics"

# Units of the python plants' ports (plants don't declare them).
PORT_UNITS: dict[str, dict[str, str]] = {
    "RCServo": {"pulse_width": "s", "load_torque": "N*m", "supply": "V", "angle": "rad", "angle_deg": "deg",
                "speed": "rad/s", "target_angle": "rad", "current": "A", "motor_voltage": "V", "power": "W",
                "feedback_voltage": "V", "output_torque": "N*m"},
    "DCMotorRL": {"supply": "V", "speed": "rad/s", "current": "A", "voltage": "V", "power": "W",
                  "torque": "N*m", "back_emf": "V"},
    "GearedDrivetrain": {"motor_torque": "N*m", "load_torque": "N*m", "motor_angle": "rad", "motor_speed": "rad/s",
                         "motor_acceleration": "rad/s^2", "load_angle": "rad", "load_speed": "rad/s",
                         "shaft_torque": "N*m", "friction_torque": "N*m", "dissipated_power": "W", "stored_energy": "J"},
    "ThermalRC": {"current": "A", "ambient": "degC", "temperature": "degC", "resistance": "ohm", "copper_loss": "W"},
    "ServoPulseGenerator": {"angle_deg": "deg", "pulse_width": "s"},
    "HBridgePWM": {"in1": "1", "in2": "1", "duty": "1"},
    "PIController": {},
    "FunctionGenerator": {"gate": "1"},
    "I2cMaster": {"temperature": "degC"}, "Tmp102": {"temperature": "degC"},
    "ModbusMaster": {"setpoint": "degC", "temperature": "degC", "pressure": "bar"},
    "ModbusSlave": {"temperature": "degC", "pressure": "bar", "setpoint": "degC"},
    "NmeaReader": {"lat": "deg", "lon": "deg", "speed_kn": "kn"},
    "SpiMaster": {"ax": "g", "ay": "g", "az": "g"}, "Lis3dh": {"ax": "g", "ay": "g", "az": "g"},
    "EthIo": {"value": "bar"}, "EthPlc": {"value": "bar"},
}

# Comms boards: (class, part id, name, category, icon, description, {event port: default topic}). Event
# outputs publish on, event inputs subscribe to the topic, so boards dropped together share a bus at once.
_C = "embeddyn.mechatronics.comms."
_BOARDS = [
    (_C + "can:CanBus", "can_bus", "CAN bus (arbitration)", "Buses", "bus",
     "Arbitrates the boards' frames (exact bit timing) and puts them on the wire topic.", {"tx": "can.tx", "wire": "can"}),
    (_C + "can:CanRestbus", "can_restbus", "CAN restbus", "Buses", "bus",
     "Sends the frames of absent nodes on a schedule.", {"tx": "can.tx", "rx": "can"}),
    (_C + "boards:CanopenMaster", "canopen_master", "CANopen master", "Wired boards", "chip",
     "NMT master, SYNC, SDO client, heartbeat watch.", {"can_tx": "can.tx", "can_rx": "can"}),
    (_C + "boards:AxisNode", "canopen_axis", "CANopen axis node", "Wired boards", "chip",
     "Follows the RPDO set-point, reports status and position on SYNC.", {"can_tx": "can.tx", "can_rx": "can"}),
    (_C + "boards:SensorNode", "canopen_sensor", "CANopen sensor node", "Wired boards", "chip",
     "Distance and temperature TPDOs, EMCY on over-temperature.", {"can_tx": "can.tx", "can_rx": "can"}),
    (_C + "boards:BleGateway", "ble_gateway", "CANopen → BLE gateway", "Radio boards", "radio",
     "Listens to the CANopen network and advertises a status record over BLE.",
     {"can_tx": "can.tx", "can_rx": "can", "ble": "ble"}),
    (_C + "boards:WifiBridge", "wifi_bridge", "BLE → Wi-Fi bridge", "Radio boards", "radio", "",
     {"ble": "ble", "wifi_tx": "wifi", "wifi_rx": "wifi"}),
    (_C + "boards:AccessPoint", "wifi_ap", "Wi-Fi access point (open)", "Radio boards", "radio", "",
     {"wifi_tx": "wifi", "wifi_rx": "wifi"}),
    (_C + "rf_boards:WpaAccessPoint", "wpa_ap", "Wi-Fi access point (WPA2)", "Radio boards", "radio",
     "WPA2-PSK access point with the 4-way handshake and CCMP data.", {"wifi_tx": "wifi", "wifi_rx": "wifi"}),
    (_C + "rf_boards:WpaStation", "wpa_station", "Wi-Fi station (WPA2)", "Radio boards", "radio",
     "Joins a WPA2 network and sends encrypted data.", {"wifi_tx": "wifi", "wifi_rx": "wifi"}),
    (_C + "rf_boards:Deauther", "deauther", "Wi-Fi deauther", "Radio boards", "radio", "", {"wifi_tx": "wifi"}),
    (_C + "rf_boards:BleBeacon", "ble_beacon", "BLE beacon", "Radio boards", "radio",
     "iBeacon / Eddystone advertiser.", {"ble": "ble"}),
    (_C + "rf_boards:BleSensor", "ble_sensor", "BLE sensor", "Radio boards", "radio",
     "Advertises readings, answers scan requests.", {"ble": "ble", "ble_rx": "ble"}),
    (_C + "rf_boards:BlePhone", "ble_phone", "BLE phone (scanner)", "Radio boards", "radio",
     "Scans and connects (CONNECT_IND).", {"ble": "ble", "ble_rx": "ble"}),
    (_C + "rf_boards:NfcReader", "nfc_reader", "NFC reader (ISO 14443-A)", "Radio boards", "radio",
     "REQA, anticollision, SELECT, READ of a Type 2 tag.", {"tx": "nfc", "rx": "nfc"}),
    (_C + "rf_boards:Type2Tag", "nfc_tag", "NFC tag (NTAG213)", "Radio boards", "radio",
     "Type 2 tag with an NDEF record.", {"tx": "nfc", "rx": "nfc"}),
    (_C + "rf_boards:Em4100Card", "em4100", "125 kHz card (EM4100)", "Radio boards", "radio", "", {"tx": "rfid"}),
    (_C + "rf_boards:Gen2Reader", "gen2_reader", "UHF RFID reader (Gen2)", "Radio boards", "radio",
     "EPC Gen2 inventory: Query, RN16, ACK, EPC.", {"tx": "rfid", "rx": "rfid"}),
    (_C + "rf_boards:Gen2Tags", "gen2_tags", "UHF RFID tags (Gen2)", "Radio boards", "radio", "", {"tx": "rfid", "rx": "rfid"}),
    (_C + "rf_boards:LoraNode", "lora_node", "LoRaWAN node", "Radio boards", "radio",
     "Class A end device: encrypted uplinks, MIC.", {"tx": "lora", "rx": "lora"}),
    (_C + "rf_boards:LoraGateway", "lora_gateway", "LoRaWAN gateway", "Radio boards", "radio", "", {"tx": "lora", "rx": "lora"}),
    (_C + "i2c:I2cMaster", "i2c_master", "I2C master (MCU)", "Wired boards", "chip", "", {"tx": "i2c", "rx": "i2c"}),
    (_C + "i2c:Tmp102", "tmp102", "TMP102 sensor (I2C)", "Wired boards", "thermo", "", {"tx": "i2c", "rx": "i2c"}),
    (_C + "spi:SpiMaster", "spi_master", "SPI master (MCU)", "Wired boards", "chip", "", {"tx": "spi", "rx": "spi"}),
    (_C + "spi:Lis3dh", "lis3dh", "LIS3DH accelerometer (SPI)", "Wired boards", "chip", "", {"tx": "spi", "rx": "spi"}),
    (_C + "serial:GpsReceiver", "gps", "GPS module (UART NMEA)", "Wired boards", "radio", "", {"tx": "uart"}),
    (_C + "serial:NmeaReader", "nmea_reader", "NMEA reader (MCU UART)", "Wired boards", "chip", "", {"rx": "uart"}),
    (_C + "serial:ModbusMaster", "modbus_master", "Modbus RTU master (PLC)", "Wired boards", "chip", "",
     {"tx": "rs485", "rx": "rs485"}),
    (_C + "serial:ModbusSlave", "modbus_slave", "Modbus RTU slave (drive)", "Wired boards", "chip", "",
     {"tx": "rs485", "rx": "rs485"}),
    (_C + "ethernet:EthPlc", "eth_plc", "Ethernet PLC (Modbus/TCP)", "Wired boards", "chip", "", {"tx": "eth", "rx": "eth"}),
    (_C + "ethernet:EthIo", "eth_io", "Ethernet remote I/O", "Wired boards", "chip", "", {"tx": "eth", "rx": "eth"}),
]

_PLANTS = [
    # (class reference, part id, name, category, domain, icon, description)
    ("embeddyn.mechatronics.plants:RCServo", "rc_servo", "RC servo", "Mechanical", "mechanical", "servo",
     "Hobby servo: pulse decoder, amplifier, DC motor, 200:1 gearbox, horn, feedback pot."),
    ("embeddyn.mechatronics.plants:GearedDrivetrain", "drivetrain", "Geared drivetrain", "Mechanical", "mechanical",
     "gear", "Rotor, gearbox with backlash, compliant shaft, load with friction, encoder."),
    ("embeddyn.mechatronics.plants:DCMotorRL", "dc_motor_rl", "DC motor (armature)", "Electrical", "electrical",
     "motor", "PWM-driven armature circuit L di/dt = duty*V - R i - Ke w."),
    ("embeddyn.mechatronics.plants:HBridgePWM", "h_bridge", "H-bridge (2 PWM inputs)", "Electrical", "electrical",
     "circuit", "Sign-magnitude bridge: duty = IN1 - IN2, for firmware driving two PWM channels."),
    ("embeddyn.mechatronics.plants:ThermalRC", "thermal_rc", "Winding thermal RC", "Thermal", "thermal", "thermo",
     "First-order copper heating with R(T) feedback."),
    ("embeddyn.mechatronics.plants:PIController", "pi_controller", "PI controller", "Controllers", "firmware", "chip",
     "Continuous PI with clamp and anti-windup (software controller)."),
    ("embeddyn.mechatronics.gui.sources:ServoPulseGenerator", "servo_pulse_gen", "Servo pulse generator",
     "Controllers", "firmware", "chip", "Angle command (deg) to RC pulse width, a software servo controller."),
    ("embeddyn.mechatronics.gui.sources:FunctionGenerator", "function_generator", "Function generator", "Sources",
     "environment", "wave", "Sine, square, triangle, sawtooth, step, ramp, pulse or DC, computed in the simulation."),
]

_INSTRUMENTS = [
    ("oscilloscope", "Oscilloscope", "scope", 4, "Four channels; roll or sweep timebase; stacked or overlaid traces.",
     {"timebase": 0.5, "mode": "roll", "layout": "stack"}),
    ("multimeter", "Multimeter", "meter", 1, "Instantaneous, mean, min, max and RMS of one signal.",
     {"mode": "value"}),
    ("logic_analyser", "Logic analyser", "logic", 8, "Eight digital channels with a threshold.",
     {"threshold": 0.5, "timebase": 0.5}),
    ("xy_plotter", "X-Y plotter", "xy", 2, "Plots channel B against channel A (phase portraits, loci).", {}),
    ("gauge", "Gauge", "gauge", 1, "Analog dial with a configurable range.", {"minimum": -1.0, "maximum": 1.0}),
    ("led", "LED", "led", 1, "Lights when the signal exceeds the threshold.", {"threshold": 0.5, "color": "#ff3b30"}),
    ("data_table", "Watch table", "table", 8, "Live values of up to eight signals.", {}),
    ("serial_monitor", "Serial monitor", "terminal", 1, "UART console of a Renode MCU: received lines and a send box.",
     {"line_end": "CRLF"}),
]

_SCENE = [
    ("scene_servo", "Servo + arm", "servo", ["angle"], "Servo body with a horn and arm that turn with the angle.",
     {"length": 0.12, "unit": "auto", "color": "#2f6fdf"}),
    ("scene_motor", "Motor shaft", "motor", ["angle"], "Motor can with a marked shaft and flywheel.",
     {"radius": 0.05, "unit": "auto", "color": "#9a9a9a"}),
    ("scene_pendulum", "Pendulum / arm link", "pendulum", ["angle"],
     "Rod and bob hinged on a horizontal axis; offset 90 deg makes angle 0 horizontal (robot-arm joint).",
     {"length": 0.25, "unit": "auto", "color": "#e0a020", "offset": 0.0}),
    ("scene_cart", "Wheeled cart", "cart", ["position", "heading"], "Two-wheel cart that drives along the floor.",
     {"wheel_radius": 0.04, "unit": "auto", "color": "#3aa860"}),
    ("scene_slider", "Linear slide", "linear", ["position"], "Carriage on a rail (m).",
     {"stroke": 0.3, "color": "#c04040"}),
    ("scene_lamp", "Lamp", "lamp", ["value"], "Glows with the signal (0..1 or over threshold).",
     {"threshold": 0.5, "color": "#ffd60a"}),
    ("scene_gauge", "3-D bar", "bar", ["value"], "Bar whose height follows a signal.",
     {"minimum": 0.0, "maximum": 1.0, "color": "#7d5fff"}),
]


def _plant_part(reference: str, part_id: str, name: str, category: str, domain: str, icon: str,
                description: str, topics: dict[str, str] | None = None) -> dict[str, Any]:
    from ..adapters.python_model import load_plant_class
    cls = load_plant_class(reference)
    units = PORT_UNITS.get(cls.__name__, {})
    parameters = {}
    for pname, param in inspect.signature(cls.__init__).parameters.items():
        if pname == "self" or param.default is inspect.Parameter.empty:
            continue
        value = param.default
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            parameters[pname] = value
        elif isinstance(value, (tuple, list)):
            parameters[pname] = list(value)
    ports = [{"name": p, "direction": "in", "unit": units.get(p, "")} for p in getattr(cls, "inputs", ())]
    ports += [{"name": p, "direction": "out", "unit": units.get(p, "")} for p in getattr(cls, "outputs", ())]
    topics = topics or {}
    events = {p: topics[p] for p in getattr(cls, "event_outputs", ()) if p in topics}
    subscribe = {p: topics[p] for p in getattr(cls, "event_inputs", ()) if p in topics}
    ports += [{"name": f"event:{p}", "direction": "in", "unit": "", "kind": "event"} for p in getattr(cls, "event_inputs", ())]
    ports += [{"name": f"event:{p}", "direction": "out", "unit": "", "kind": "event"} for p in getattr(cls, "event_outputs", ())]
    options: dict[str, Any] = {"backend": "python", "max_step": 1.0e-4, "parameters": {}, "inputs": {}, "outputs": {}}
    if events:
        options["events"] = events
    if subscribe:
        options["subscribe"] = subscribe
    return {
        "id": part_id, "name": name, "category": category, "kind": "instance", "domain": domain, "icon": icon,
        "description": (description or ((cls.__doc__ or "").strip().splitlines() or [name])[0]),
        "template": {"domain": domain, "model": reference, "options": options},
        "parameters": parameters, "ports": ports,
    }


def part_library() -> list[dict[str, Any]]:
    parts = [_plant_part(*spec) for spec in _PLANTS]
    parts += [_plant_part(ref, pid, name, category, "firmware" if category != "Buses" else "electrical", icon, text, topics)
              for ref, pid, name, category, icon, text, topics in _BOARDS]
    parts.append({"id": "bus", "name": "Bus / radio channel", "category": "Buses", "kind": "bus", "icon": "bus",
                  "description": "An event topic boards publish on and subscribe to (CAN, UART, I2C, SPI, RS-485, "
                                 "Ethernet, BLE, Wi-Fi…). Wire ⇄ pins to it.", "ports": []})
    servo_fw = EXAMPLES / "firmware" / "rc-servo-l476.elf"
    parts += [
        {"id": "stm32l476_servo_fw", "name": "STM32L476 servo firmware", "category": "Controllers", "kind": "instance",
         "domain": "firmware", "icon": "mcu",
         "description": "Renode STM32L476 running the RC-servo firmware (ADC in, TIM2 pulse and fault LED out)."
                        + ("" if servo_fw.exists() else " Build it first: make -C examples/mechatronics/firmware/rc-servo-l476"),
         "template": {"domain": "firmware", "options": {
             "backend": "renode", "launch": True, "machine": "l476", "port": 21240,
             "script": str(EXAMPLES / "renode" / "stm32l476-servo.resc"),
             "firmware": str(servo_fw),
             "bindings": str(EXAMPLES / "bindings" / "renode-rc-servo.yaml"),
             "variables": {"quantum": "0.00001"}}}},
        {"id": "renode_mcu", "name": "Renode MCU (generic)", "category": "Controllers", "kind": "instance",
         "domain": "firmware", "icon": "mcu",
         "description": "Any Renode platform: set script (.resc), firmware (.elf), machine and a bindings file.",
         "template": {"domain": "firmware", "options": {"backend": "renode", "launch": True, "port": 21250,
                                                        "script": "", "firmware": "", "bindings": ""}}},
        {"id": "ngspice_circuit", "name": "SPICE circuit (ngspice)", "category": "Electrical", "kind": "instance",
         "domain": "electrical", "icon": "circuit",
         "description": "Shared-library ngspice netlist; ports come from its bindings file.",
         "template": {"domain": "electrical", "model": "", "options": {"backend": "ngspice-shared", "bindings": ""}}},
        {"id": "webots_world", "name": "Webots world", "category": "Mechanical", "kind": "instance",
         "domain": "mechanical", "icon": "robot",
         "description": "Webots robot world (.wbt) driven through its bindings file.",
         "template": {"domain": "mechanical", "model": "", "options": {"backend": "webots", "headless": True,
                                                                       "port": 8765, "bindings": ""}}},
        {"id": "mujoco_model", "name": "MuJoCo model (MJCF)", "category": "Mechanical", "kind": "instance",
         "domain": "mechanical", "icon": "robot",
         "description": "Multibody physics with contacts and checkpoints; set model to an MJCF .xml and its "
                        "actuators, joints and sensors appear as pins.",
         "template": {"domain": "mechanical", "model": str(EXAMPLES / "mujoco" / "arm-joint.xml"),
                      "options": {"backend": "mujoco", "inputs": {}, "outputs": {}}}},
        {"id": "fmu", "name": "FMU (FMI 2/3)", "category": "Mechanical", "kind": "instance", "domain": "mechanical",
         "icon": "box", "description": "Functional Mock-up Unit; map its inputs and outputs in the inspector.",
         "template": {"domain": "mechanical", "model": "", "options": {"backend": "fmu", "inputs": {}, "outputs": {}}}},
        {"id": "replay_csv", "name": "CSV replay", "category": "Sources", "kind": "instance", "domain": "environment",
         "icon": "table", "description": "Plays recorded columns back as signals (time column first).",
         "template": {"domain": "environment", "model": "", "options": {"backend": "replay", "columns": {}}}},
    ]
    parts += [
        {"id": "slider", "name": "Slider / potentiometer", "category": "Sources", "kind": "source", "icon": "slider",
         "description": "Live control: drag it while the simulation runs.",
         "ports": [{"name": "value", "direction": "out", "unit": ""}],
         "settings": {"minimum": 0.0, "maximum": 1.0, "value": 0.0, "unit": "", "step": 0.0}},
        {"id": "switch", "name": "Switch / push button", "category": "Sources", "kind": "source", "icon": "switch",
         "description": "Live two-state control (off value / on value).",
         "ports": [{"name": "value", "direction": "out", "unit": ""}],
         "settings": {"off": 0.0, "on": 1.0, "value": 0.0, "momentary": False, "unit": ""}},
    ]
    for part_id, name, icon, channels, description, settings in _INSTRUMENTS:
        letters = "ABCDEFGH"
        parts.append({"id": part_id, "name": name, "category": "Instruments", "kind": "instrument", "icon": icon,
                      "description": description, "settings": settings,
                      "ports": [{"name": letters[i], "direction": "in", "unit": ""} for i in range(channels)]})
    for part_id, name, icon, inputs, description, settings in _SCENE:
        parts.append({"id": part_id, "name": name, "category": "3-D bodies", "kind": "scene", "icon": icon,
                      "description": description, "settings": {"x": 0.0, "z": 0.0, "yaw": 0.0, **settings},
                      "ports": [{"name": p, "direction": "in", "unit": ""} for p in inputs]})
    return parts


def _relative(value: str, root: Path) -> str:
    """Absolute library paths become relative to the project directory."""
    if not value or not os.path.isabs(value):
        return value
    try:
        return os.path.relpath(value, root)
    except ValueError:                       # another drive on Windows
        return value


def instantiate_part(part_id: str, node_id: str, root: Path, signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """A schematic node for `part_id` named `node_id`, plus the signals it creates.

    Python plant outputs get signals ``<node>.<output>`` right away; inputs stay
    unmapped (the plant default applies) until something is wired to them.
    """
    from .document import instance_ports, valid_node_id
    if not valid_node_id(node_id):
        raise ValueError(f"invalid part name {node_id!r}: use letters, digits and _ (start with a letter)")
    parts = {part["id"]: part for part in part_library()}
    if part_id not in parts:
        raise KeyError(part_id)
    part = parts[part_id]
    new_signals: dict[str, Any] = {}
    node: dict[str, Any] = {"id": node_id, "kind": part["kind"], "part": part_id, "label": node_id,
                            "icon": part.get("icon", "box"), "x": 0, "y": 0}
    if part["kind"] == "bus":
        raise ValueError("a bus is added by the Lab (its topic is its name)")
    if part["kind"] == "instance":
        spec = copy.deepcopy(part["template"])
        options = spec.setdefault("options", {})
        for key in ("script", "firmware", "bindings"):
            if isinstance(options.get(key), str):
                options[key] = _relative(options[key], root)
        if spec.get("model") and os.path.isabs(str(spec["model"])):
            spec["model"] = _relative(str(spec["model"]), root)
        if options.get("backend") == "python":
            for port in part["ports"]:
                if port["direction"] == "out" and port.get("kind") != "event":
                    name = f"{node_id}.{port['name']}"
                    options["outputs"][port["name"]] = name
                    new_signals[name] = {"domain": spec["domain"], "unit": port["unit"], "initial": 0.0}
            if part_id == "rc_servo":           # the pot reads mid-rail at 0 deg
                new_signals[f"{node_id}.feedback_voltage"]["initial"] = 1.65
        node["section"] = "instances"
        node["spec"] = spec
        node["domain"] = spec["domain"]
        known = {**(signals or {}), **new_signals}
        node["ports"] = instance_ports(spec, root, known, [])
    else:
        node["settings"] = copy.deepcopy(part.get("settings", {}))
        node["ports"] = [dict(port, signal=None) for port in part["ports"]]
        if part["kind"] == "source":
            name = f"{node_id}.value"
            node["ports"][0]["signal"] = name
            settings = node["settings"]
            new_signals[name] = {"domain": "environment", "unit": settings.get("unit", ""),
                                 "initial": float(settings.get("value", 0.0))}
    return {"node": node, "signals": new_signals}


