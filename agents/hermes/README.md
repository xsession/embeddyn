# Hermes Agent kit: full MCU emulation

Skills and an MCP server that let an agent, running a 27B-class open model in
[Hermes Agent](https://hermes-agent.nousresearch.com/docs), repeat the workflow that brought the
dsPIC30F5011 into Renode:
- CPU core;
- differential tests;
- tlib integration;
- every peripheral;
- firmware tests;
- real product firmware.

## Contents

| Path | What |
|---|---|
| `skills/embedded-emulation/` | nine skills (agentskills.io format): the workflow and one skill per phase, with templates and worked examples |
| `../../embeddyn/agents/mcu_emu/` | the `mcu-emu` MCP server: 17 deterministic tools, standard library only |
| `../../embeddyn/agents/mcu_emu/profiles/` | MCU profiles (dsPIC30F5011; an STM32L476/SVD example) |
| `config.example.yaml` | the Hermes `config.yaml` fragment |
| `install.sh` | links the skills into `~/.hermes/skills/` |

## Skills

| Skill | Phase |
|---|---|
| `mcu-emulation-workflow` | the plan: phases, done-gates, tool map; read it first |
| `mcu-facts-extraction` | profile, SFR map, vectors, pin and clock facts |
| `mcu-cpu-core` | the core as a standalone C interpreter, plus a native runner |
| `mcu-differential-testing` | step-by-step comparison with the vendor simulator, random generators |
| `renode-tlib-core-integration` | the interpreter inside tlib and the C# CPU class |
| `renode-peripheral-models` | C# peripheral models, patterns, per-peripheral checklists, detail pass |
| `renode-firmware-tests` | test firmware with precomputed expectations, run through `renode_run` |
| `real-firmware-bringup` | product firmware on a board file, gaps, speed |
| `emulation-privacy-hygiene` | private names out of repositories; `privacy_scan` before commits |

## MCP tools

All tools return JSON with `ok`. Large data goes to files, and results are capped at 12,000
characters.

| Group | Tools |
|---|---|
| profiles | `mcu_profile_list`, `mcu_profile_show` |
| register map | `extract_sfr_map` (C header / linker script / CMSIS-SVD), `sfr_lookup`, `extract_vectors`, `sfr_coverage` |
| firmware | `build_firmware`, `hex_to_images`, `disassemble` |
| Renode | `renode_build`, `renode_check_platform`, `renode_run` (loads, stimuli, reads, UART capture, monitor commands), `pc_hotspots` |
| core testing | `trace_from_columns`, `compare_traces` |
| code generation | `peripheral_skeleton` |
| hygiene | `privacy_scan` |

## Install

```bash
agents/hermes/install.sh
```

Then:
1. Merge `config.example.yaml` into `~/.hermes/config.yaml`, setting `EMBEDDYN_HOME`, the
   Renode/dotnet paths and the toolchain variables.
2. Run `hermes mcp test mcu_emu`.
3. Start a session with `/mcu-emulation-workflow`.

## Working with a 27B-class model

- **One phase per session.** Load `/mcu-emulation-workflow` plus the phase skill, and end each
  session by writing the status (done / verified / open) into the project's notes. Long sessions
  degrade small models more than large ones.
- **Let the tools do the parsing.** The skills tell the model to call a tool rather than read
  vendor headers, Renode logs or traces itself. Keep the MCP tool list complete (no `include`
  filter), so the model never improvises a shell parser.
- **Gates.** Every procedure step that changes code is followed by a check tool (`renode_build`,
  `renode_check_platform`, `renode_run` with expected reads, `compare_traces`). A step counts as
  done only when its check passed.
- **Context.** Use at least 32k context. Tool results are capped; files hold the bulk (SFR maps,
  traces).
- **Sampling.** Use low temperature (0.2–0.4) for code edits. A tool-call parser that handles the
  model's tool format reliably matters more than raw model size.
- **Review.** A larger model, or a human, should review the core and anything marked unverified
  before it is trusted.

## Verifying the kit

`tests/agents/test_mcu_emu.py` runs in the normal test suite: protocol, extraction (with synthetic
header/linker/SVD fixtures), images, traces, privacy scan, skeleton. With `XC16_BIN`, `RENODE_BIN`
and `RENODE_ROOT` set, it also builds and runs the dsPIC30 smoke firmware in Renode.
