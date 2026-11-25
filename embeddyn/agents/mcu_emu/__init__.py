"""mcu-emu: an MCP server with the deterministic steps of full MCU emulation.

The tools extract register maps from vendor files, build firmware, run Renode
headless, compare execution traces, profile hot spots, generate peripheral
skeletons and scan for private names. They return short JSON so that smaller
models (27B class) can drive the workflow described in the skills under
agents/hermes/skills/embedded-emulation.

Run: python -m embeddyn.agents.mcu_emu   (stdio MCP server, no dependencies)
"""
