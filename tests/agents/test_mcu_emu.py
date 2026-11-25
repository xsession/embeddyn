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
