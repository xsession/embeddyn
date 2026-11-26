# Agent kit: full MCU emulation with Hermes

The workflow in [Emulate a real product end to end](emulate-a-product.md) brought a dsPIC30F5011
into Renode in full:
- CPU core;
- differential tests against the vendor simulator;
- tlib integration;
- every peripheral;
- firmware tests;
- the real product firmware.

The agent kit packages that workflow so that an agent running a 27B-class open model in
[Hermes Agent](https://hermes-agent.nousresearch.com/docs) can repeat it for other MCUs.

* **Skills** (`agents/hermes/skills/embedded-emulation/`, agentskills.io format):
  - nine procedural skills, one per phase, each with *When to Use*, *Procedure*, *Pitfalls* and
    *Verification*;
  - templates (MCU profile, core header, native runner, C# SFR base, test firmware);
  - worked examples from the dsPIC30 work.
* **MCP server** `mcu-emu` (`embeddyn/agents/mcu_emu/`, standard library only): 17 deterministic
  tools.
  - Register maps from C headers, linker scripts or CMSIS-SVD; vector order; SFR coverage of a
    platform.
  - Firmware build and HEX images; disassembly.
  - Renode build, platform check, headless runs with stimuli, reads and UART capture; PC hot spots.
  - Trace conversion and step-by-step comparison.
  - Peripheral skeleton generation.
  - Privacy scan.

  Every tool returns short JSON with `ok`, so the model calls a tool instead of parsing vendor
  files or logs.
* **MCU profiles** (`embeddyn/agents/mcu_emu/profiles/`): toolchain, files, image layout and
  Renode platform per MCU. They cover the dsPIC30F5011, dsPIC33FJ128MC802, dsPIC33FJ128MC804, dsPIC33EP512GP806 and dsPIC33EP128GM604 (XC16), the
  TMS320F28069 (TI cl2000, `"style": "ti"`: one compile-and-link call, the ELF loaded with
  `cpu LoadTIProgram`, dis2000 for disassembly), the MSPM0G3507 and MSPM0C1103 (GNU Arm with
  the MSPM0 SDK headers, startup file, linker script and DriverLib), the F28M35H52C1 Concerto as two
  profiles on one dual-core platform (`f28m35h52c1_m3`, GNU Arm; `f28m35h52c1_c28`, TI cl2000; pass both
  load commands to `renode_run` and name the CPU with `cpu`), plus an STM32L476 example for
  SVD-described parts. Add your own in `$MCU_EMU_PROFILES`.

## Install

```bash
agents/hermes/install.sh
```

Then merge `agents/hermes/config.example.yaml` into `~/.hermes/config.yaml` and run
`hermes mcp test mcu_emu`. The kit was checked with Hermes Agent v0.23.2: the server connects and
its 17 tools are discovered, and the nine skills are listed and enabled.

## Using it

Start with `/mcu-emulation-workflow`, then load the skill of the current phase. Run one phase per
session, and end each session with a written status. Details and model advice are in
`agents/hermes/README.md`.

## Tests

`tests/agents/test_mcu_emu.py` covers:
- the MCP protocol over stdio;
- extraction from synthetic header, linker-script and SVD fixtures;
- HEX images;
- trace comparison;
- the privacy scan;
- skeleton generation.

With `XC16_BIN`, `RENODE_BIN` and `RENODE_ROOT` set, it also builds the dsPIC30 smoke firmware and
runs it in Renode.
