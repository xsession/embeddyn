"""`embeddyn mechatronics import`: KiCad netlist + CubeMX project -> emulated board, firmware, motion plant.

The unit tests use a small synthetic board (STM32F405, BMI088 on SPI1 with its two chip selects and data-ready
lines, an RGB LED through resistors, an MP2359 buck with its feedback divider, a 16 MHz crystal, USB). The
end-to-end test is opt-in: EMBEDDYN_IMPORT_ZIP (a KiCad + STM32CubeIDE project with that kind of board),
RENODE_BIN, RENODE_ROOT and GCC_ARM.
"""
from __future__ import annotations

import json
import math
import os
import socket
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.importer import import_project
from embeddyn.mechatronics.importer.analysis import Resolver, check_firmware_against_board, load_library, scan_firmware_drivers
from embeddyn.mechatronics.importer.emit import board_repl, bindings, feed_targets, project
from embeddyn.mechatronics.importer.firmware import find_firmware_projects, parse_cubeide
from embeddyn.mechatronics.importer.ioc import read_ioc
from embeddyn.mechatronics.importer.kicad_netlist import parse_sexpr, read_netlist
from embeddyn.mechatronics.indicators import LedBrightness
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.motion import PendulumMount, isa_pressure

MCU_PINS = {"5": "PH0-OSC_IN", "6": "PH1-OSC_OUT", "10": "PC2", "11": "PC3", "20": "PA4", "21": "PA5", "22": "PA6",
            "23": "PA7", "24": "PC4", "44": "PA11", "45": "PA12", "56": "PB4", "57": "PB5", "58": "PB6",
            "19": "VDD", "18": "VSS"}
IMU_PINS = {"1": "INT2", "2": "NC", "3": "VDD", "5": "CSB2", "7": "PS", "8": "SCK", "9": "SDI", "10": "SDO2",
            "12": "INT3", "14": "CSB1", "15": "SDO1", "16": "INT1"}
RGB_PINS = {"1": "RK", "2": "GK", "3": "BK", "4": "A"}
BUCK_PINS = {"2": "GND", "3": "FB", "5": "IN", "6": "SW"}
USB_PINS = {"2": "D-", "3": "D+"}

# net name -> list of (ref, pin)
NETS = {
    "+3V3": [("U1", "19"), ("U2", "3"), ("D1", "4"), ("R4", "1"), ("L1", "2")],
    "GND": [("U1", "18"), ("U2", "7"), ("R6", "2"), ("U3", "2")],
    "/SPI1_SCK": [("U1", "21"), ("U2", "8")],
    "/SPI1_MISO": [("U1", "22"), ("U2", "10"), ("U2", "15")],
    "/SPI1_MOSI": [("U1", "23"), ("U2", "9")],
    "/SPI1_!CS_ACC": [("U1", "20"), ("U2", "14")],
    "/SPI1_!CS_GYR": [("U1", "24"), ("U2", "5")],
    "/INT_ACC": [("U1", "10"), ("U2", "16")],
    "/INT_GYR": [("U1", "11"), ("U2", "12")],
    "Net-(D1-Pad1)": [("D1", "1"), ("R1", "1")],
    "Net-(D1-Pad2)": [("D1", "2"), ("R2", "1")],
    "Net-(D1-Pad3)": [("D1", "3"), ("R3", "1")],
    "/LED_RED": [("R1", "2"), ("U1", "56")],
    "/LED_GREEN": [("R2", "2"), ("U1", "57")],
    "/LED_BLUE": [("R3", "2"), ("U1", "58")],
    "Net-(U3-FB)": [("U3", "3"), ("R4", "2"), ("R5", "1")],
    "Net-(R5-Pad2)": [("R5", "2"), ("R6", "1")],
    "Net-(U3-SW)": [("U3", "6"), ("L1", "1")],
    "/OSC_IN": [("U1", "5"), ("Y1", "1")],
    "/OSC_OUT": [("U1", "6"), ("Y1", "2")],
    "/USB_DP": [("U1", "45"), ("J1", "3")],
    "/USB_DM": [("U1", "44"), ("J1", "2")],
}
COMPONENTS = [
    ("U1", "STM32F405RGT6", "MCU_ST_STM32F4", "STM32F405RGTx", MCU_PINS),
    ("U2", "BMI088", "BMI088", "BMI088", IMU_PINS),
    ("D1", "RGB", "Device", "LED_RGBA", RGB_PINS),
    ("U3", "MP2359DJ-LF-Z", "Regulator", "MP2359DJ-LF-Z", BUCK_PINS),
    ("Y1", "16MHz", "Device", "Crystal", {"1": "1", "2": "2"}),
    ("J1", "USB_B_Micro", "Connector", "USB_B_Micro", USB_PINS),
    ("R1", "1k", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("R2", "1k", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("R3", "1k", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("R4", "47k", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("R5", "15k", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("R6", "270", "Device", "R_Small", {"1": "~", "2": "~"}),
    ("L1", "10u", "Device", "L_Small", {"1": "~", "2": "~"}),
]

IOC = """#MicroXplorer Configuration settings - do not modify
Mcu.Family=STM32F4
Mcu.UserName=STM32F405RGTx
Mcu.IP0=SPI1
Mcu.IP1=USB_OTG_FS
Mcu.IP2=USB_DEVICE
Mcu.Pin0=PA4
Mcu.Pin1=PA5
Mcu.Pin2=PC2
PA4.GPIO_Label=SPI1_NCS_ACC
PA4.Signal=GPIO_Output
PA5.Signal=SPI1_SCK
PC2.GPIO_Label=INT_ACC
PC2.Signal=GPXTI2
RCC.HSE_VALUE=16000000
RCC.SYSCLKFreq_VALUE=168000000
USB_DEVICE.CLASS_NAME_FS=CDC
"""

CPROJECT = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject><storageModule moduleId="org.eclipse.cdt.core.settings"><cconfiguration id="debug.1">
<storageModule moduleId="cdtBuildSystem"><configuration name="Debug"><folderInfo><toolChain>
<option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.target_mcu" value="STM32F405RGTx"/>
<option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.fpu" value="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.fpu.value.fpv4-sp-d16"/>
<option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.floatabi" value="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.floatabi.value.hard"/>
<tool><option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.tool.c.compiler.option.definedsymbols">
<listOptionValue value="STM32F405xx"/><listOptionValue value="USE_HAL_DRIVER"/></option>
<option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.tool.c.compiler.option.includepaths">
<listOptionValue value="../Core/Inc"/><listOptionValue value="&quot;${workspace_loc:/${ProjName}/Core/Inc/Drivers}&quot;"/></option>
<option superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.tool.c.linker.option.script" value="C:\\Users\\someone\\proj\\STM32F405RGTX_FLASH.ld"/>
</tool></toolChain></folderInfo>
<sourceEntries><entry flags="VALUE_WORKSPACE_PATH|RESOLVED" kind="sourcePath" name="Core"/></sourceEntries>
</configuration></storageModule></cconfiguration></storageModule></cproject>
"""


def _netlist_text() -> str:
    libparts = "\n".join(
        f'    (libpart (lib {lib}) (part {part}) (pins ' + " ".join(f"(pin (num {n}) (name {name}) (type passive))"
                                                              for n, name in pins.items()) + "))"
        for _, _, lib, part, pins in COMPONENTS)
    comps = "\n".join(f"    (comp (ref {ref}) (value {value}) (footprint X:Y) (libsource (lib {lib}) (part {part})))"
                      for ref, value, lib, part, _ in COMPONENTS)
    nets = "\n".join(f'    (net (code {i}) (name "{name}") ' + " ".join(f"(node (ref {r}) (pin {p}))" for r, p in nodes) + ")"
                     for i, (name, nodes) in enumerate(NETS.items(), 1))
    return f"(export (version D)\n  (design (date \"11/11/2020 21:41:04\"))\n  (components\n{comps})\n" \
           f"  (libparts\n{libparts})\n  (nets\n{nets}))\n"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    root = tmp_path / "board"
    root.mkdir()
    (root / "board.net").write_text(_netlist_text())
    firmware = root / "Firmware"
    (firmware / "Core" / "Src" / "Drivers").mkdir(parents=True)
    (firmware / "Core" / "Inc").mkdir(parents=True)
    (firmware / "Firmware.ioc").write_text(IOC)
    (firmware / ".cproject").write_text(CPROJECT)
    (firmware / ".project").write_text("<projectDescription><name>BoardFW</name></projectDescription>")
    (firmware / "STM32F405RGTX_FLASH.ld").write_text("/* linker script */\n")
    (firmware / "Core" / "Src" / "main.c").write_text("#include \"BMI088.h\"\n#include \"SPL06.h\"\nint main(void){return 0;}\n")
    (firmware / "Core" / "Src" / "Drivers" / "SPL06.c").write_text("/* SPL06 barometer driver */\n")
    return root


def _plan(board: Path):
    library = load_library()
    plan = Resolver(read_netlist(board / "board.net"), read_ioc(board / "Firmware" / "Firmware.ioc"), library).resolve()
    check_firmware_against_board(plan, scan_firmware_drivers([board / "Firmware"], library), library)
    return plan


def test_sexpr_parser_handles_quotes_and_nesting():
    tree = parse_sexpr('(a (b "x y") (c (d 1)))')
    assert tree == ["a", ["b", "x y"], ["c", ["d", "1"]]]


def test_netlist_pin_names_come_from_libparts(board):
    netlist = read_netlist(board / "board.net")
    assert netlist.pin_names("U2")["14"] == "CSB1"
    assert netlist.net_of("U2", "8").short_name == "SPI1_SCK"


def test_resolver_finds_buses_chip_selects_and_interrupts(board):
    plan = _plan(board)
    assert plan.mcu_ref == "U1" and plan.platform.endswith("stm32f405.repl")
    devices = {d.name: d for d in plan.devices}
    accel, gyro = devices["accel"], devices["gyro"]
    assert (accel.bus, accel.cs.name, accel.outputs["Int1"].name) == ("spi1", "PA4", "PC2")
    assert (gyro.bus, gyro.cs.name, gyro.outputs["Int3"].name) == ("spi1", "PC4", "PC3")
    assert accel.bus_kind == "spi"           # PS tied to GND
    assert plan.hse_hz == 16_000_000 and plan.usb


def test_resolver_traces_leds_through_resistors_and_the_regulator_divider(board):
    plan = _plan(board)
    leds = {led.color: led for led in plan.leds}
    assert leds["red"].pin.name == "PB4" and leds["red"].active_low and leds["red"].series_ohm == 1000
    assert leds["blue"].pin.through == ["R3"]
    rail = plan.rails[0]
    assert rail.net == "+3V3" and rail.volts == pytest.approx(0.81 * (1 + 47e3 / 15.27e3))


def test_firmware_driver_without_a_board_part_is_reported(board):
    plan = _plan(board)
    messages = [f.message for f in plan.findings]
    assert any("SPL06" in m and "no such part" in m for m in messages)


def test_substitution_emulates_another_part(board):
    netlist = read_netlist(board / "board.net")
    netlist.components["U4"] = netlist.components["U2"].__class__(ref="U4", value="BMP280", part="BMP280")
    library = load_library()
    plan = Resolver(netlist, None, library, {"U4": "goertek-spl06-001"}).resolve()
    assert any("emulated as Goertek SPL06-001" in f.message for f in plan.findings)


def test_emitted_board_bindings_and_project(board, tmp_path):
    plan = _plan(board)
    repl = board_repl(plan, "test board")
    assert 'using "platforms/cpus/st/stm32f405.repl"' in repl
    assert "hseFrequency: 16000000" in repl
    assert "spi1Mux: SPI.SPIMultiplexer @ spi1" in repl and "multipleSelection: true" in repl
    assert "U2Accel: Sensors.BMI088_Accelerometer @ spi1Mux 0" in repl and "Int1 -> gpioPortC@2" in repl
    assert "U2Gyro: Sensors.BMI088_Gyroscope @ spi1Mux 1" in repl and "Int3 -> gpioPortC@3" in repl
    assert "usbSerial: USB.USBSerialHost @ sysbus" in repl
    # every feed slot targets its own property (the bridge caches per path and property)
    targets = feed_targets(plan)
    assert len({(path, prop) for path, prop, _, _ in targets.values()}) == len(targets)
    assert "resolved[(path, name)]" in repl
    signals = {b["signal"] for b in bindings(plan)["bindings"]}
    assert {"motion.acc_x", "motion.gyro_y", "motion.pressure", "board.ledD1Red"} - {"motion.pressure"} <= signals
    out = tmp_path / "out"
    out.mkdir()
    (out / "bindings.yaml").write_text(yaml.safe_dump(bindings(plan)))
    (out / "board.resc").write_text("")
    (out / "project.yaml").write_text(yaml.safe_dump(project(plan, "test", None)))
    loaded = MechatronicProject.load(out / "project.yaml")
    loaded.validate()
    assert {"board", "motion"} <= set(loaded.simulator_instances())


def test_cubeide_project_settings(board):
    project = parse_cubeide(board / "Firmware")
    assert project.name == "BoardFW" and project.target_mcu == "STM32F405RGTx"
    assert "-mcpu=cortex-m4" in project.cpu_flags and "-mfpu=fpv4-sp-d16" in project.cpu_flags
    assert "-mfloat-abi=hard" in project.cpu_flags
    assert project.defines == ["STM32F405xx", "USE_HAL_DRIVER"]
    assert project.includes[1] == board / "Firmware" / "Core" / "Inc" / "Drivers"
    # the author's absolute Windows path falls back to the script in the project
    assert project.linker_script == board / "Firmware" / "STM32F405RGTX_FLASH.ld"
    assert [p.name for p in find_firmware_projects(board)] == ["BoardFW"]


def test_import_without_building_writes_all_files(board, tmp_path):
    result = import_project(board, tmp_path / "out", title="test-board", build_firmware=False)
    names = set(result.files)
    assert {"board.repl", "board.resc", "bindings.yaml", "project.yaml", "report.md", "import.json"} <= names
    summary = json.loads((tmp_path / "out" / "import.json").read_text())
    assert summary["mcu"]["part"] == "STM32F405RGT6" and summary["usb"]
    assert "Import report: test-board" in (tmp_path / "out" / "report.md").read_text()


def test_pendulum_mount_reads_one_g_at_rest_and_swings_consistently():
    plant = PendulumMount(angle=0.0)
    out = plant.output(0.0, [0.0, 0.0], {})
    assert out["acc_z"] == pytest.approx(9.80665) and out["acc_x"] == pytest.approx(0.0)
    assert out["pressure"] == pytest.approx(isa_pressure(119.5))
    # undamped: the tangential specific force of a simple pendulum is zero, the radial one is g cos + L w^2
    swing = PendulumMount(damping=0.0, length=0.5)
    theta, omega = math.radians(20), 1.2
    out = swing.output(0.0, [theta, omega], {})
    assert out["acc_x"] == pytest.approx(0.0, abs=1e-9)
    assert out["acc_z"] == pytest.approx(9.80665 * math.cos(theta) + 0.5 * omega ** 2)
    assert out["gyro_y"] == pytest.approx(-omega)
    rotated = PendulumMount(damping=0.0, axes="z,y,-x").output(0.0, [theta, omega], {})
    assert rotated["acc_x"] == pytest.approx(out["acc_z"]) and rotated["acc_z"] == pytest.approx(-out["acc_x"])


def test_led_brightness_averages_pwm_and_inverts_active_low():
    led = LedBrightness(active_low=True, tau=0.02)
    x = led.initial_state()
    for _ in range(200):                       # 0.2 s of a 25 % high PWM (75 % on), explicit Euler at 1 ms
        x = [x[0] + 0.001 * led.derivatives(0.0, x, {"duty": 0.25})[0]]
    assert led.output(0.0, x, {})["brightness"] == pytest.approx(0.75, abs=1e-3)


# --- end to end (opt-in) ---------------------------------------------------------------------------------------

ZIP = os.environ.get("EMBEDDYN_IMPORT_ZIP")
NEEDS = [ZIP, os.environ.get("RENODE_BIN"), os.environ.get("RENODE_ROOT"), os.environ.get("GCC_ARM")]


@pytest.mark.skipif(not all(NEEDS), reason="set EMBEDDYN_IMPORT_ZIP, RENODE_BIN, RENODE_ROOT and GCC_ARM")
def test_import_build_and_run_end_to_end(tmp_path):
    import subprocess
    import sys

    result = import_project(Path(ZIP), tmp_path / "out")
    assert result.firmware and not result.build_errors
    usb_lines = []
    for name in sorted(n for n in result.files if n.startswith("project")):
        path = result.files[name]
        data = yaml.safe_load(path.read_text())
        data["duration_seconds"] = 1.2
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            data["instances"]["board"]["options"]["port"] = sock.getsockname()[1]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        runs = tmp_path / f"runs-{path.stem}"
        done = subprocess.run([sys.executable, "-m", "embeddyn", "mechatronics", "run", str(path), "--output-root", str(runs)],
                              capture_output=True, text=True, timeout=900)
        assert done.returncode == 0, done.stderr[-2000:]
        for events in runs.rglob("events.jsonl"):
            for line in events.read_text().splitlines():
                record = json.loads(line)
                if record.get("topic") == "board.usb":
                    usb_lines.append(record["payload"])
    # a firmware that streams IMU data over USB CDC shows gravity on one axis
    numeric = [[float(x) for x in line.split()] for line in usb_lines if line.split() and
               all(x.replace(".", "", 1).lstrip("-").isdigit() for x in line.split())]
    if numeric:
        assert any(any(8.5 < abs(v) < 11.5 for v in row) for row in numeric)
