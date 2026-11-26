"""Tests of the mcu-emu MCP server (embeddyn/agents/mcu_emu)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from embeddyn.agents.mcu_emu import server, tools

HEADER = """
extern volatile uint16_t U1STA __attribute__((__sfr__));
typedef struct tagU1STABITS {
  uint16_t URXDA:1;
  uint16_t OERR:1;
  uint16_t :6;
  uint16_t TRMT:1;
  uint16_t UTXBF:1;
} U1STABITS;
#define PORTA (*(volatile uint8_t *)(0x0022))
"""

GLD = """
 U1MODE       = 0x20C;
_U1MODE       = 0x20C;
 U1STA        = 0x20E;
 TMR1         = 0x100;
** Interrupt Vector Table
    LONG(DEFINED(__ReservedTrap0) ? ABSOLUTE(__ReservedTrap0) : 0);
    LONG(DEFINED(__OscillatorFail) ? ABSOLUTE(__OscillatorFail) : 0);
    LONG(DEFINED(__INT0Interrupt) ? ABSOLUTE(__INT0Interrupt) : 0);
    LONG(DEFINED(__T1Interrupt) ? ABSOLUTE(__T1Interrupt) : 0);
** Alternate
"""

SVD = """<?xml version="1.0"?>
<device><peripherals>
 <peripheral><name>TIM2</name><baseAddress>0x40000000</baseAddress>
  <interrupt><name>TIM2</name><value>28</value></interrupt>
  <registers><register><name>CR1</name><addressOffset>0x0</addressOffset><size>32</size><resetValue>0</resetValue>
   <fields><field><name>CEN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field></fields></register>
  <register><name>CNT</name><addressOffset>0x24</addressOffset></register></registers></peripheral>
 <peripheral derivedFrom="TIM2"><name>TIM3</name><baseAddress>0x40000400</baseAddress></peripheral>
</peripherals></device>
"""


@pytest.fixture
def files(tmp_path):
    (tmp_path / "dev.h").write_text(HEADER)
    (tmp_path / "dev.gld").write_text(GLD)
    (tmp_path / "dev.svd").write_text(SVD)
    return tmp_path


def test_protocol_lists_and_calls_tools(files):
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "mcu-emu"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert {"extract_sfr_map", "renode_run", "compare_traces", "peripheral_skeleton"} <= set(names)
    bad = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sfr_lookup", "arguments": {"x": 1}}})
    assert bad["result"]["isError"] and "expected" in json.loads(bad["result"]["content"][0]["text"])
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_stdio_round_trip():
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    out = subprocess.run([sys.executable, "-m", "embeddyn.agents.mcu_emu"], input="\n".join(map(json.dumps, msgs)) + "\n",
                         capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2])})
    replies = [json.loads(l) for l in out.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]


def test_sfr_map_from_header_and_linker_script(files):
    r = tools.extract_sfr_map(header=str(files / "dev.h"), linker_script=str(files / "dev.gld"), out=str(files / "map.json"))
    assert r["ok"] and r["registers"] == 4
    hit = tools.sfr_lookup(str(files / "map.json"), name="^U1STA$")["registers"][0]
    assert hit["address"] == "0x020E" and "TRMT:8" in hit["fields"] and "UTXBF:9" in hit["fields"]
    assert tools.sfr_lookup(str(files / "map.json"), name="PORTA")["registers"][0]["address"] == "0x0022"


def test_sfr_map_and_vectors_from_svd(files):
    r = tools.extract_sfr_map(svd=str(files / "dev.svd"), out=str(files / "svd.json"))
    assert r["registers"] == 4 and r["interrupts"] == 1
    regs = {x["name"]: x for x in json.loads((files / "svd.json").read_text())["registers"]}
    assert regs["TIM3_CNT"]["address"] == 0x40000424 and regs["TIM2_CR1"]["fields"][0]["name"] == "CEN"
    assert tools.extract_vectors(svd=str(files / "dev.svd"))["vectors"] == [{"index": 28, "name": "TIM2"}]


def test_vectors_from_linker_script(files):
    v = tools.extract_vectors(linker_script=str(files / "dev.gld"), skip_traps=2)
    assert v["vectors"] == [{"index": 0, "name": "INT0Interrupt"}, {"index": 1, "name": "T1Interrupt"}]


def test_coverage(files):
    tools.extract_sfr_map(header=str(files / "dev.h"), linker_script=str(files / "dev.gld"), out=str(files / "map.json"))
    (files / "p.repl").write_text("uart1: X.Uart @ sysbus 0x020C\n    size: 0x0A\n\ntimer1: X.T @ sysbus <0x100, +0x6>\n")
    c = tools.sfr_coverage(str(files / "map.json"), str(files / "p.repl"))
    assert c["uncovered"] == ["PORTA@0x0022"]
    c = tools.sfr_coverage(str(files / "map.json"), str(files / "p.repl"), cpu_ranges=[["0x0", "0x54"]])
    assert c["uncovered_count"] == 0


def test_hex_images(tmp_path):
    hexfile = tmp_path / "a.hex"
    hexfile.write_text(":020000040000FA\n:0800000004010000000000F3\n:020000040001F9\n:04000000AABBCCDDEE\n:00000001FF\n")
    r = tools.hex_to_images(str(hexfile), out_dir=str(tmp_path / "out"), base="0x01000000", phantom_lane=3)
    assert [i["address"] for i in r["images"]] == ["0x1000000", "0x1010000"]
    assert Path(r["images"][0]["file"]).read_bytes()[:4] == bytes([4, 1, 0, 0])


def test_traces(tmp_path):
    (tmp_path / "t.txt").write_text("000100 040104 0000 0001\n000104 20C00F 0C00 0001\nRAM 0000\n")
    assert tools.trace_from_columns(str(tmp_path / "t.txt"), str(tmp_path / "a.jsonl"), ["pc", "op", "W0", "sr"])["steps"] == 2
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    step = json.loads(lines[1]); step["sr"] ^= 0x100
    (tmp_path / "b.jsonl").write_text(lines[0] + "\n" + json.dumps(step) + "\n")
    assert tools.compare_traces(str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl"), masks={"sr": "0x100"})["tolerated"] == {"sr": 1}
    bad = tools.compare_traces(str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl"))
    assert not bad["ok"] and bad["step"] == 1 and "sr" in bad["differences"]


def test_privacy_scan(tmp_path):
    (tmp_path / "doc.md").write_text("board ACME-1234 here\nnothing\n")
    (tmp_path / "rules.tsv").write_text("private\tacme-[0-9]+\n")
    r = tools.privacy_scan([str(tmp_path / "doc.md")], rules_file=str(tmp_path / "rules.tsv"))
    assert not r["ok"] and r["match_count"] == 1 and "ACME" not in r["matches"][0]


def test_peripheral_skeleton(files):
    tools.extract_sfr_map(header=str(files / "dev.h"), linker_script=str(files / "dev.gld"), out=str(files / "map.json"))
    r = tools.peripheral_skeleton("Dev_UART", str(files / "Uart.cs"), sfr_map=str(files / "map.json"), start="0x20C", end="0x216", irqs=["RxIRQ"])
    code = (files / "Uart.cs").read_text()
    assert r["registers"] == 2 and "case 0x2:" in code and "TRMT:8" in code and "public GPIO RxIRQ" in code


def test_profiles_expand_variables(monkeypatch):
    monkeypatch.setenv("XC16_BIN", "/x/bin")
    p = tools.load_profile("dspic30f5011")
    assert p["toolchain"]["cc"] == "/x/bin/xc16-gcc" and p["image"]["phantom_lane"] == 3
    assert "stm32l476" in [x["name"] for x in tools.mcu_profile_list()["profiles"]]


def test_ti_profile(monkeypatch):
    monkeypatch.setenv("C2000_CGT", "/cgt")
    monkeypatch.setenv("RENODE_ROOT", "/r")
    p = tools.load_profile("tms320f28069")
    assert p["toolchain"]["style"] == "ti" and p["toolchain"]["cc"] == "/cgt/bin/cl2000"
    assert p["toolchain"]["link_files"] == ["/r/tools/c2000/tests/f28069/ram.cmd"]
    assert tools._load_lines([{"file": "a.out", "kind": "ti"}, "cpu LoadTIProgram @b.out"]) == \
        ["cpu LoadTIProgram @a.out", "cpu LoadTIProgram @b.out"]


needs_ti = pytest.mark.skipif(not (os.environ.get("C2000_CGT") and os.environ.get("C2000WARE") and os.environ.get("RENODE_BIN")
                                   and os.environ.get("RENODE_ROOT") and shutil.which("dotnet")),
                              reason="C2000_CGT, C2000WARE, RENODE_BIN, RENODE_ROOT and dotnet needed")


@needs_ti
def test_build_and_run_f28069(tmp_path):
    src = tmp_path / "t.c"
    src.write_text('#include "F2806x_Device.h"\nvolatile Uint32 r[2];\nvoid main(void)\n{\n    EALLOW;\n'
                   '    SysCtrlRegs.WDCR = 0x0068;\n    EDIS;\n    r[0] = 0x12345678UL;\n    r[1] = SysCtrlRegs.PLLSTS.all;\n'
                   '    __asm(" ESTOP0");\n    for(;;) {\n    }\n}\n')
    b = tools.build_firmware("tms320f28069", str(src), out_dir=str(tmp_path / "b"))
    assert b["ok"], b
    word = int(re.search(r"^\S+\s+([0-9a-f]{8})\s+r\s*$", Path(b["map"]).read_text(), re.M).group(1), 16)
    r = tools.renode_run("platforms/cpus/ti/tms320f28069.repl", b["load_commands"], run_for="0.001",
                         reads=[f"0x{2 * word:X}:32", f"0x{2 * word + 4:X}:32"])
    assert list(r["reads"].values()) == ["0x12345678", "0x00000001"], r


def test_mspm0_profiles(monkeypatch):
    monkeypatch.setenv("MSPM0_SDK", "/sdk")
    monkeypatch.setenv("ARM_BIN", "/arm")
    for name, dev, lib in (("mspm0g3507", "MSPM0G3507", "mspm0g1x0x_g3x0x"), ("mspm0c1103", "MSPM0C1103", "mspm0c110x")):
        tc = tools.load_profile(name)["toolchain"]
        assert tc["cc"] == "/arm/arm-none-eabi-gcc"
        assert f"-D__{dev}__" in tc["cflags"]
        assert f"-T/sdk/source/ti/devices/msp/m0p/linker_files/gcc/{dev.lower()}.lds" in tc["cflags"]
        assert f"/sdk/source/ti/driverlib/lib/gcc/m0p/{lib}/driverlib.a" in tc["cflags_post"]


needs_mspm0 = pytest.mark.skipif(not (os.environ.get("MSPM0_SDK") and os.environ.get("ARM_BIN") and os.environ.get("RENODE_BIN")
                                      and os.environ.get("RENODE_ROOT") and shutil.which("dotnet")),
                                 reason="MSPM0_SDK, ARM_BIN (arm-none-eabi with newlib), RENODE_BIN, RENODE_ROOT and dotnet needed")


@needs_mspm0
@pytest.mark.parametrize("profile,platform,sram", [("mspm0g3507", "platforms/cpus/ti/mspm0g3507.repl", 0x20200000),
                                                   ("mspm0c1103", "platforms/cpus/ti/mspm0c1103.repl", 0x20000000)])
def test_build_and_run_mspm0(tmp_path, profile, platform, sram):
    src = tmp_path / "t.c"
    src.write_text("#include <ti/devices/msp/msp.h>\n#include <ti/driverlib/driverlib.h>\nvolatile uint32_t r[2];\n"
                   "int main(void)\n{\n    DL_GPIO_reset(GPIOA);\n    DL_GPIO_enablePower(GPIOA);\n"
                   "    DL_GPIO_initDigitalOutput(IOMUX_PINCM1);\n    DL_GPIO_enableOutput(GPIOA, DL_GPIO_PIN_0);\n"
                   "    DL_GPIO_setPins(GPIOA, DL_GPIO_PIN_0);\n    r[0] = 0x12345678;\n    r[1] = GPIOA->DOUT31_0;\n"
                   "    for(;;) {\n    }\n}\n")
    b = tools.build_firmware(profile, str(src), out_dir=str(tmp_path / "b"))
    assert b["ok"], b
    r = tools.renode_run(platform, [{"file": b["elf"], "kind": "elf"}], run_for="0.001",
                         reads=[f"0x{sram:X}:32", f"0x{sram + 4:X}:32"], after=["iomux PinLevel 0"])
    assert list(r["reads"].values()) == ["0x12345678", "0x00000001"], r
    assert r["after"]["iomux PinLevel 0"] == "True"


def test_dspic33fj128mc804_profile(monkeypatch):
    monkeypatch.setenv("XC16_BIN", "/xc")
    p = tools.load_profile("dspic33fj128mc804")
    assert "-mcpu=33FJ128MC804" in p["toolchain"]["cflags"]
    assert p["renode"]["platform"] == "platforms/cpus/microchip/dspic33fj128mc804.repl"
    assert p["image"]["base"] == "0x01000000" and p["image"]["phantom_lane"] == 3


def test_dspic33ep512gp806_profile(monkeypatch):
    monkeypatch.setenv("XC16_BIN", "/xc")
    p = tools.load_profile("dspic33ep512gp806")
    assert "-mcpu=33EP512GP806" in p["toolchain"]["cflags"]
    assert p["toolchain"]["cflags_post"] == ["-Wl,--script=/xc/../support/dsPIC33E/gld/p33EP512GP806.gld"]
    assert p["renode"]["platform"] == "platforms/cpus/microchip/dspic33ep512gp806.repl"
    assert p["memory"]["ram"] == ["0x1000", "0xDFFF"]


def test_dspic33ep128gm604_profile(monkeypatch):
    monkeypatch.setenv("XC16_BIN", "/xc")
    p = tools.load_profile("dspic33ep128gm604")
    assert "-mcpu=33EP128GM604" in p["toolchain"]["cflags"]
    assert p["renode"]["platform"] == "platforms/cpus/microchip/dspic33ep128gm604.repl"
    assert p["memory"]["ram"] == ["0x1000", "0x4FFF"]


def test_rp2040_profile(monkeypatch):
    monkeypatch.setenv("RENODE_ROOT", "/r")
    monkeypatch.setenv("ARM_BIN", "/arm")
    p = tools.load_profile("rp2040")
    assert p["toolchain"]["cc"] == "/arm/arm-none-eabi-gcc"
    assert "-T/r/tools/rp2040/tests/rp2040.ld" in p["toolchain"]["cflags"]
    assert p["toolchain"]["cflags_post"][0] == "/r/tools/rp2040/tests/crt0.S"
    assert p["renode"]["platform"] == "platforms/boards/raspberrypi/raspberry_pi_pico.repl"
    assert p["memory"]["sram"] == ["0x20000000", "0x20041FFF"]


needs_rp2040 = pytest.mark.skipif(not (os.environ.get("ARM_BIN") and os.environ.get("RENODE_BIN") and os.environ.get("RENODE_ROOT")
                                       and shutil.which("dotnet")),
                                  reason="ARM_BIN (arm-none-eabi with newlib), RENODE_BIN, RENODE_ROOT and dotnet needed")


@needs_rp2040
def test_build_and_run_rp2040_periph(tmp_path):
    root = Path(os.environ["RENODE_ROOT"])
    b = tools.build_firmware("rp2040", str(root / "tools/rp2040/tests/periph.c"), out_dir=str(tmp_path))
    assert b["ok"], b
    nm = subprocess.run([str(Path(os.environ["ARM_BIN"]) / "arm-none-eabi-nm"), b["elf"]], capture_output=True, text=True).stdout
    r = int(re.search(r"^([0-9a-f]{8}) [bB] r$", nm, re.M).group(1), 16)
    rom = str(root / "tools/rp2040/bootrom/rp2040_bootrom.elf")
    res = tools.renode_run("platforms/boards/raspberrypi/raspberry_pi_pico.repl",
                           [{"file": rom, "kind": "elf"}, {"file": b["elf"], "kind": "elf"}], run_for="0.01",
                           before=["adc SetVoltage 0 1.65"], uart=["uart0"],
                           reads=[f"0x{r + 4 * i:X}:32" for i in (0, 1, 4, 7, 8, 9, 10)])
    # all 10 steps; divider 142 r 6; interpolator 0x18D; DMA sum 36; PIO ~x; ADC 1.65 V; core 1 ran
    assert list(res["reads"].values()) == ["0x0000000A", "0x008E0006", "0x0000018D", "0x00000024", "0xF0F0F0F0",
                                           "0x00000800", "0xC0DE0001"], res
    assert "RP2040 OK" in res["uart"]["uart0"], res


def test_concerto_profiles(monkeypatch):
    monkeypatch.setenv("RENODE_ROOT", "/r")
    monkeypatch.setenv("C2000_CGT", "/cgt")
    m3 = tools.load_profile("f28m35h52c1_m3")
    c28 = tools.load_profile("f28m35h52c1_c28")
    assert "-T/r/tools/concerto/tests/m3.ld" in m3["toolchain"]["cflags"]
    assert c28["toolchain"]["style"] == "ti" and "--retain=*(codestart)" in c28["toolchain"]["ldflags"]
    assert m3["renode"]["platform"] == c28["renode"]["platform"] == "platforms/cpus/ti/f28m35h52c1.repl"
    # per-CPU load commands pass through unchanged
    assert tools._load_lines(["c28 LoadTIProgram @a.out false", "m3 VectorTableOffset 0", "b.bin 0x0"]) == \
        ["c28 LoadTIProgram @a.out false", "m3 VectorTableOffset 0", "sysbus LoadBinary @b.bin 0x0"]


needs_concerto = pytest.mark.skipif(not (os.environ.get("ARM_BIN") and os.environ.get("C2000_CGT") and os.environ.get("RENODE_BIN")
                                         and os.environ.get("RENODE_ROOT") and shutil.which("dotnet")),
                                    reason="ARM_BIN, C2000_CGT, RENODE_BIN, RENODE_ROOT and dotnet needed")


@needs_concerto
def test_build_and_run_concerto_ipc(tmp_path):
    tests = Path(os.environ["RENODE_ROOT"]) / "tools" / "concerto" / "tests"
    m3 = tools.build_firmware("f28m35h52c1_m3", str(tests / "m3_ipc.c"), out_dir=str(tmp_path / "m3"))
    c28 = tools.build_firmware("f28m35h52c1_c28", str(tests / "c28_ipc.c"), out_dir=str(tmp_path / "c28"))
    assert m3["ok"] and c28["ok"], (m3, c28)
    r = tools.renode_run("platforms/cpus/ti/f28m35h52c1.repl", m3["load_commands"] + c28["load_commands"], run_for="0.01",
                         uart=["uart0"], cpu="m3")
    assert "M3: C28 says 00001235 00C28C28" in r["uart"]["uart0"], r


needs_tools = pytest.mark.skipif(not (os.environ.get("XC16_BIN") and os.environ.get("RENODE_BIN") and os.environ.get("RENODE_ROOT")
                                      and shutil.which("dotnet")), reason="XC16_BIN, RENODE_BIN, RENODE_ROOT and dotnet needed")


@needs_tools
def test_build_and_run_dspic30_smoke(tmp_path):
    src = Path(os.environ["RENODE_ROOT"]) / "tools/dspic30/tests/renode/smoke.c"
    b = tools.build_firmware("dspic30f5011", str(src), out_dir=str(tmp_path))
    assert b["ok"], b
    # 0.2 s: blank configuration fuses mean the 64 ms power-up timer before the first instruction
    r = tools.renode_run("platforms/cpus/microchip/dspic30f5011.repl", b["load_commands"], run_for="0.2", reads=["0x900", "0x906"])
    assert r["reads"] == {"0x900": "0x7360", "0x906": "0xC0DE"}, r


@needs_tools
def test_build_and_run_dspic33e_cpu(tmp_path):
    src = Path(os.environ["RENODE_ROOT"]) / "tools/dspic30/tests/dspic33e/cpu33e.c"
    b = tools.build_firmware("dspic33ep512gp806", str(src), out_dir=str(tmp_path))
    assert b["ok"], b
    r = tools.renode_run("platforms/cpus/microchip/dspic33ep512gp806.repl", b["load_commands"], run_for="0.05",
                         reads=["0x1000", "0x1002", "0x1014", "0x101E"])
    # 12 steps done; four nested DO loops 36; PSV const 0xC0DE; REPEAT #20000 -> 20001
    assert r["reads"] == {"0x1000": "0x000C", "0x1002": "0x0024", "0x1014": "0xC0DE", "0x101E": "0x4E21"}, r


@needs_tools
def test_build_and_run_dspic33ep128gm604(tmp_path):
    src = Path(os.environ["RENODE_ROOT"]) / "tools/dspic30/tests/dspic33e/periph604.c"
    b = tools.build_firmware("dspic33ep128gm604", str(src), out_dir=str(tmp_path))
    assert b["ok"], b
    # the configuration words are in flash: build_firmware returns a load command per HEX segment
    r = tools.renode_run("platforms/cpus/microchip/dspic33ep128gm604.repl", b["load_commands"], run_for="0.05",
                         before=["adc1 SetVoltage 1000000 4", "adc1 SetVoltage 2000000 6"],
                         reads=["0x1000", "0x1008", "0x100A", "0x100C", "0x1012"], after=["pwm Frequency 1"])
    # all nine steps; override 0x4000; op amp follower 1.0 V on AN3; C4OUT; PTG 3 interrupts / 3 conversions
    assert r["reads"] == {"0x1000": "0x0009", "0x1008": "0x4000", "0x100A": "0x04D9", "0x100C": "0x0008",
                          "0x1012": "0x0303"}, r
    assert abs(float(r["after"]["pwm Frequency 1"]) - 20000) < 5, r
