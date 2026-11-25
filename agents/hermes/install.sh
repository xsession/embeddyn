#!/bin/sh
# Make the embedded-emulation skills visible to Hermes Agent and print the MCP config to add.
# Usage: agents/hermes/install.sh [hermes-home]   (default ~/.hermes)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
HERMES_HOME=${1:-$HOME/.hermes}
mkdir -p "$HERMES_HOME/skills"
if [ -e "$HERMES_HOME/skills/embedded-emulation" ] && [ ! -L "$HERMES_HOME/skills/embedded-emulation" ]; then
    echo "$HERMES_HOME/skills/embedded-emulation exists and is not a link; leaving it alone" >&2
else
    ln -sfn "$HERE/skills/embedded-emulation" "$HERMES_HOME/skills/embedded-emulation"
    echo "skills linked: $HERMES_HOME/skills/embedded-emulation -> $HERE/skills/embedded-emulation"
fi
mkdir -p "$HOME/.config/mcu-emu/profiles"
echo
echo "Add the MCP server to $HERMES_HOME/config.yaml (see $HERE/config.example.yaml), with EMBEDDYN_HOME=$REPO,"
echo "then run:  hermes mcp test mcu_emu"
echo "Check the server alone:  PYTHONPATH=$REPO python3 -m embeddyn.agents.mcu_emu < /dev/null"
