"""Turn a netlist (+ CubeMX project) into an emulation plan: which MCU, which devices on which buses, chip
selects, interrupt lines, LEDs, supply rails, the HSE crystal and USB, with findings for the import report."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .ioc import IocProject
from .kicad_netlist import Component, Net, Netlist

SUPPLY_RE = re.compile(r"^(\+?\d+V\d*|\+?\d+V|VCC.*|VDD.*|VBUS|VIN|\+[0-9.]+V.*|3V3|5V)$", re.I)
GROUND_RE = re.compile(r"^(GND.*|VSS.*|AGND|DGND|PGND|0)$", re.I)
PASSIVE_REF_RE = re.compile(r"^(R|FB|L|JP)\d")
MCU_PIN_RE = re.compile(r"^P([A-K])(\d{1,2})(?:[-_/].*)?$")


@dataclass
class Finding:
    level: str      # info | warning | error
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "message": self.message}


@dataclass
class McuPin:
    name: str        # "PA4"
    port: str        # "A"
    number: int
    through: list[str] = field(default_factory=list)   # series parts between the device and the MCU

    @property
    def gpio(self) -> str:
        return f"gpioPort{self.port}"


@dataclass
class Device:
    ref: str
    part_id: str
    name: str                       # library device name (accel, gyro, baro, flash)
    model: str
    bus_kind: str                   # spi | i2c
    bus: str                        # spi1, i2c2
    cs: McuPin | None = None
    i2c_address: int | None = None
    outputs: dict[str, McuPin] = field(default_factory=dict)   # model GPIO property -> MCU pin
    quantities: dict[str, dict[str, Any]] = field(default_factory=dict)
    title: str = ""

    @property
    def instance(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", f"{self.ref}{self.name.capitalize()}")


@dataclass
class Led:
    ref: str
    color: str
    pin: McuPin
    active_low: bool
    series_ohm: float | None = None

    @property
    def instance(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", f"led{self.ref}{self.color.capitalize()}")


@dataclass
class Rail:
    ref: str
    net: str
    volts: float | None
    nominal: float | None
    detail: str = ""


@dataclass
class Plan:
    mcu_ref: str = ""
    mcu_part: str = ""
    platform: str = ""
    family: str = ""
    hse_hz: int | None = None
    devices: list[Device] = field(default_factory=list)
    leds: list[Led] = field(default_factory=list)
    rails: list[Rail] = field(default_factory=list)
    usb: bool = False
    usb_peripheral: str = "usbOtgFs"
    unmatched: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    firmware_drivers: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.findings.append(Finding("warning", message))

    def info(self, message: str) -> None:
        self.findings.append(Finding("info", message))


def load_library(extra: list[Path] | None = None) -> list[dict[str, Any]]:
    text = resources.files("embeddyn.mechatronics.importer").joinpath("parts.yaml").read_text(encoding="utf-8")
    parts = list(yaml.safe_load(text)["parts"])
    for path in extra or []:
        parts = list(yaml.safe_load(path.read_text(encoding="utf-8")).get("parts", [])) + parts
    return parts


def match_part(component: Component, library: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in library:
        for key, value in (("match", component.mpn), ("match", component.value), ("symbol", component.part),
                           ("footprint", component.footprint)):
            for pattern in entry.get(key, []):
                if value and re.search(pattern, value, re.I):
                    return entry
    return None


def _parse_value(text: str) -> float | None:
    """'10k' -> 10000, '4u7' -> 4.7e-6, '16MHz' -> 16e6, '2k2' -> 2200."""
    text = text.strip().replace("Ω", "").replace("ohm", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([pnuµmkKMG]?)(\d*)\s*(?:Hz|H|F|R)?$", text)
    if not m:
        m2 = re.match(r"^(\d+)([RkKM])(\d+)$", text)
        if not m2:
            return None
        whole, unit, frac = m2.groups()
        number = float(f"{whole}.{frac}")
        return number * {"R": 1, "k": 1e3, "K": 1e3, "M": 1e6}[unit]
    number, unit, frac = m.groups()
    value = float(number + (("." + frac) if frac else ""))
    return value * {"": 1, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6,
                    "G": 1e9}[unit]


def _nominal_volts(net: str) -> float | None:
    m = re.match(r"^\+?(\d+)V(\d*)$", net.rsplit("/", 1)[-1])
    if m:
        return float(f"{m.group(1)}.{m.group(2) or 0}")
    return None


class Resolver:
    def __init__(self, netlist: Netlist, ioc: IocProject | None, library: list[dict[str, Any]],
                 substitutions: dict[str, str] | None = None) -> None:
        self.netlist = netlist
        self.ioc = ioc
        self.library = library
        self.substitutions = substitutions or {}
        self.plan = Plan()
        self.pin_index: dict[str, dict[str, Net]] = {}

    # --- netlist helpers ---------------------------------------------------------------------------------------
    def pins_by_name(self, ref: str) -> dict[str, Net]:
        if ref not in self.pin_index:
            names = self.netlist.pin_names(ref)
            nets = self.netlist.nets_of(ref)
            index: dict[str, Net] = {}
            for number, net in nets.items():
                index[names.get(number, number)] = net
                index.setdefault(number, net)
            self.pin_index[ref] = index
        return self.pin_index[ref]

    def mcu_pin(self, net: Net | None, depth: int = 2, through: list[str] | None = None) -> McuPin | None:
        """The MCU GPIO a net reaches, directly or through up to `depth` series passives (R, FB, L, JP)."""
        if net is None:
            return None
        through = through or []
        for node in net.nodes:
            if node.ref == self.plan.mcu_ref:
                m = MCU_PIN_RE.match(node.pin_name or "")
                if m:
                    return McuPin(name=f"P{m.group(1)}{int(m.group(2))}", port=m.group(1), number=int(m.group(2)),
                                  through=list(through))
        if depth == 0 or self.is_supply(net) or self.is_ground(net):
            return None
        for node in net.nodes:
            if PASSIVE_REF_RE.match(node.ref) and node.ref not in through:
                other = [n for pin, n in self.netlist.nets_of(node.ref).items() if n is not net]
                if len(other) == 1:
                    found = self.mcu_pin(other[0], depth - 1, through + [node.ref])
                    if found:
                        return found
        return None

    @staticmethod
    def is_supply(net: Net | None) -> bool:
        return bool(net) and bool(SUPPLY_RE.match(net.short_name))

    @staticmethod
    def is_ground(net: Net | None) -> bool:
        return bool(net) and bool(GROUND_RE.match(net.short_name))

    def bus_of(self, pin: McuPin | None, kind: str, role: str, net: Net | None) -> str | None:
        """The peripheral instance (spi1, i2c2) a bus line uses: the CubeMX pin signal, else the net name."""
        if pin and self.ioc:
            signal = self.ioc.signal_of(pin.name)
            m = re.match(rf"({kind.upper()})(\d)_", signal)
            if m:
                return f"{kind}{m.group(2)}"
        if net:
            m = re.search(rf"{kind.upper()}(\d)", net.short_name.upper())
            if m:
                return f"{kind}{m.group(1)}"
        return None

    # --- resolution --------------------------------------------------------------------------------------------
    def resolve(self) -> Plan:
        plan = self.plan
        matches: dict[str, dict[str, Any]] = {}
        by_id = {e["id"]: e for e in self.library}
        for ref, component in sorted(self.netlist.components.items()):
            entry = match_part(component, self.library)
            if ref in self.substitutions:
                wanted = self.substitutions[ref]
                replacement = by_id.get(wanted) or next(
                    (e for e in self.library if any(re.search(p, wanted, re.I) for p in e.get("match", []))), None)
                if replacement is None:
                    plan.warn(f"substitution {ref}={wanted}: no such part in the library")
                else:
                    plan.warn(f"{ref} ({component.value}) is emulated as {replacement.get('title', replacement['id'])} "
                              f"(substitution)")
                    entry = replacement
            if entry:
                matches[ref] = entry
            elif not re.match(r"^(R|C|FB|L|TP|H|MH|J|SW|D|Q|F|JP|NT|LOGO|REF)\d", ref):
                plan.unmatched.append(f"{ref} ({component.value})")
        mcus = [ref for ref, e in matches.items() if e["kind"] == "mcu"]
        if not mcus:
            plan.findings.append(Finding("error", "no supported microcontroller in the netlist"))
            return plan
        if len(mcus) > 1:
            plan.warn(f"several MCUs ({', '.join(mcus)}): using {mcus[0]}")
        plan.mcu_ref = mcus[0]
        plan.mcu_part = self.netlist.components[plan.mcu_ref].value
        plan.platform = matches[plan.mcu_ref]["platform"]
        plan.family = matches[plan.mcu_ref]["family"]
        if self.ioc and self.ioc.mcu and not plan.mcu_part.upper().startswith(self.ioc.mcu[:9].upper()):
            plan.warn(f"the CubeMX project is for {self.ioc.mcu}, the schematic has {plan.mcu_part}")
        for ref, entry in matches.items():
            kind = entry["kind"]
            if kind == "device":
                self._device(ref, entry)
            elif kind in ("led", "rgb_led"):
                self._led(ref, entry)
            elif kind == "regulator":
                self._regulator(ref, entry)
            elif kind == "crystal":
                self._crystal(ref)
            elif kind == "usb":
                self._usb(ref)
        if self.ioc and self.ioc.hse_frequency:
            if plan.hse_hz and plan.hse_hz != self.ioc.hse_frequency:
                plan.warn(f"the crystal is {plan.hse_hz / 1e6:g} MHz but CubeMX sets HSE_VALUE = "
                          f"{self.ioc.hse_frequency / 1e6:g} MHz")
            plan.hse_hz = plan.hse_hz or self.ioc.hse_frequency
        return plan

    def _device(self, ref: str, entry: dict[str, Any]) -> None:
        pins = self.pins_by_name(ref)
        for spec in entry.get("devices", []):
            bus_kind = "spi" if "spi" in spec else "i2c"
            mode = spec.get("mode")
            if mode and "spi" in spec and "i2c" in spec:
                net = pins.get(mode["pin"])
                if mode.get("spi_when") == "low":
                    bus_kind = "spi" if self.is_ground(net) else "i2c"
                elif mode.get("spi_when") == "high":
                    bus_kind = "spi" if self.is_supply(net) else "i2c"
                else:  # "driven": SPI when the pin goes to an MCU GPIO (a chip select), I2C when tied to a rail
                    bus_kind = "spi" if self.mcu_pin(net) else "i2c"
            roles = spec[bus_kind]
            clock = roles["sck"] if bus_kind == "spi" else roles["scl"]
            clock_net = pins.get(clock)
            clock_pin = self.mcu_pin(clock_net)
            bus = self.bus_of(clock_pin, bus_kind, "sck", clock_net)
            if not bus:
                self.plan.warn(f"{ref} {spec['name']}: cannot tell which {bus_kind.upper()} its {clock} pin uses "
                               f"(net {clock_net.name if clock_net else '?'})")
                continue
            device = Device(ref=ref, part_id=entry["id"], name=spec["name"], model=spec["model"], bus_kind=bus_kind,
                            bus=bus, title=entry.get("title", ""), quantities=dict(spec.get("quantities", {})))
            if bus_kind == "spi":
                cs_net = pins.get(roles["cs"])
                device.cs = self.mcu_pin(cs_net)
                if device.cs is None:
                    self.plan.warn(f"{ref} {spec['name']}: chip select {roles['cs']} reaches no MCU GPIO "
                                   f"(net {cs_net.name if cs_net else '?'})")
                for role in ("mosi", "miso"):
                    other = self.bus_of(self.mcu_pin(pins.get(roles[role])), "spi", role, pins.get(roles[role]))
                    if other and other != bus:
                        self.plan.warn(f"{ref} {spec['name']}: {roles[role]} is on {other}, SCK on {bus}")
            else:
                address = roles.get("address", {})
                net = pins.get(address.get("pin", ""))
                device.i2c_address = address.get("high") if self.is_supply(net) else address.get("low")
            for pin_name, prop in (spec.get("outputs") or {}).items():
                target = self.mcu_pin(pins.get(pin_name))
                if target:
                    device.outputs[prop] = target
            if self.ioc and device.bus.upper() not in self.ioc.ips:
                self.plan.info(f"{ref} ({entry.get('title', entry['id'])}) is on {device.bus.upper()}, which the "
                               f"CubeMX project does not enable: the firmware does not use it")
            self.plan.devices.append(device)

    def _led(self, ref: str, entry: dict[str, Any]) -> None:
        pins = self.pins_by_name(ref)
        component = self.netlist.components[ref]
        if entry["kind"] == "rgb_led":
            common = pins.get(entry.get("common", "A"))
            common_is_anode = entry.get("common", "A").upper().startswith("A")
            for color, pin_name in entry["channels"].items():
                self._led_channel(ref, color, pins.get(pin_name), common, common_is_anode)
        else:
            anode, cathode = pins.get(entry.get("anode", "A")), pins.get(entry.get("cathode", "K"))
            color = re.sub(r"[^a-z]", "", component.value.lower()) or "led"
            if self.is_supply(anode):
                self._led_channel(ref, color, cathode, anode, True)
            elif self.is_ground(cathode):
                self._led_channel(ref, color, anode, cathode, False)
            else:
                pin = self.mcu_pin(cathode) or self.mcu_pin(anode)
                if pin is None:
                    self.plan.info(f"{ref} ({component.value} LED) is not driven by the MCU")

    def _led_channel(self, ref: str, color: str, driven: Net | None, common: Net | None, common_is_anode: bool) -> None:
        pin = self.mcu_pin(driven)
        if pin is None:
            self.plan.info(f"{ref} {color} LED is not driven by an MCU pin")
            return
        series = None
        for part in pin.through:
            if part.startswith("R"):
                series = _parse_value(self.netlist.components[part].value)
        active_low = common_is_anode and self.is_supply(common)
        self.plan.leds.append(Led(ref=ref, color=color, pin=pin, active_low=active_low, series_ohm=series))

    def _leg(self, resistor: str, start: Net) -> tuple[float | None, Net | None, list[str]]:
        """Follow a chain of series resistors from `start`; total resistance, the net it ends on, the parts."""
        total, refs, net, current = 0.0, [], start, resistor
        while True:
            value = _parse_value(self.netlist.components[current].value)
            if value is None:
                return None, None, refs
            total += value
            refs.append(current)
            others = [n for n in self.netlist.nets_of(current).values() if n is not net]
            if not others:
                return None, None, refs
            net = others[0]
            if self.is_ground(net) or self.is_supply(net) or len(net.nodes) != 2:
                return total, net, refs
            nxt = [x.ref for x in net.nodes if x.ref != current]
            if not nxt or not nxt[0].startswith("R"):
                return total, net, refs
            current = nxt[0]

    def _regulator(self, ref: str, entry: dict[str, Any]) -> None:
        pins = self.pins_by_name(ref)
        feedback = pins.get(entry["pins"]["feedback"])
        if feedback is None:
            return
        top = bottom = None
        for node in feedback.nodes:
            if not node.ref.startswith("R"):
                continue
            ohm, end, refs = self._leg(node.ref, feedback)
            if ohm is None or end is None:
                continue
            if self.is_ground(end):
                bottom = (ohm, refs)
            else:
                top = (ohm, refs, end)
        vref = float(entry.get("vref", 0.8))
        if not (top and bottom):
            self.plan.info(f"{ref}: feedback divider not found on {feedback.name}")
            return
        volts = vref * (1 + top[0] / bottom[0])
        rail = top[2]
        nominal = _nominal_volts(rail.short_name)
        detail = (f"{entry.get('topology', 'regulator')}, Vref {vref} V, top {'+'.join(top[1])} {top[0]:g} ohm, "
                  f"bottom {'+'.join(bottom[1])} {bottom[0]:g} ohm")
        self.plan.rails.append(Rail(ref=ref, net=rail.name, volts=volts, nominal=nominal, detail=detail))
        if nominal and abs(volts - nominal) / nominal > 0.05:
            self.plan.warn(f"{ref} sets {volts:.3f} V on {rail.name} (nominal {nominal} V): check the feedback divider")

    def _crystal(self, ref: str) -> None:
        component = self.netlist.components[ref]
        frequency = _parse_value(component.value)
        if not frequency:
            return
        for net in self.netlist.nets_of(ref).values():
            for node in net.nodes:
                if node.ref == self.plan.mcu_ref and re.search(r"OSC_IN|PH0|OSC_OUT|PH1", node.pin_name or ""):
                    self.plan.hse_hz = int(frequency)
                    return

    def _usb(self, ref: str) -> None:
        for net in self.netlist.nets_of(ref).values():
            pin = self.mcu_pin(net)
            if pin and pin.name in ("PA11", "PA12"):
                self.plan.usb = True
                return


def scan_firmware_drivers(directories: list[Path], library: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Library part id -> firmware files that mention its driver identifiers (outside vendor Drivers/Middlewares)."""
    found: dict[str, list[str]] = {}
    for directory in directories:
        for path in directory.rglob("*"):
            if path.suffix not in (".c", ".h", ".cpp", ".hpp") or not path.is_file():
                continue
            parts = path.relative_to(directory).parts
            if parts and parts[0] in ("Drivers", "Middlewares", "Debug", "Release"):
                continue
            text = path.name + "\n" + path.read_text(encoding="utf-8", errors="replace")[:200000]
            for entry in library:
                for ident in entry.get("firmware", []):
                    if re.search(rf"\b{re.escape(ident)}", text, re.I):
                        found.setdefault(entry["id"], []).append(str(path.relative_to(directory)))
                        break
    return found


def check_firmware_against_board(plan: Plan, drivers: dict[str, list[str]], library: list[dict[str, Any]]) -> None:
    by_id = {e["id"]: e for e in library}
    on_board = {d.part_id for d in plan.devices}
    plan.firmware_drivers = sorted(drivers)
    for part_id, files in sorted(drivers.items()):
        if part_id not in on_board:
            entry = by_id[part_id]
            plan.warn(f"the firmware has a driver for {entry.get('title', part_id)} ({', '.join(sorted(set(files))[:3])}) "
                      f"but the schematic has no such part")
    for part_id in sorted(on_board):
        if part_id not in drivers:
            entry = by_id[part_id]
            plan.info(f"no firmware driver found for {entry.get('title', part_id)}")
