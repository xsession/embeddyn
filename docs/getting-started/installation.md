# Installation

embeddyn is a Python package (3.11 or newer recommended; MuJoCo 4 needs it). The co-simulation core has no compiled dependencies: the simulators it talks to are optional and found at run time.

## The package

```bash
git clone https://github.com/xsession/embeddyn.git
cd embeddyn
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"            # core + pytest
embeddyn mechatronics --help
```

Optional extras:

| Extra | Installs | Needed for |
|---|---|---|
| `mujoco` | `mujoco` | `backend: mujoco` multibody instances |
| `fmi` | `fmpy`, `pythonfmu3` | `fmu-check`, `fmu-export` (loading FMUs needs nothing extra) |
| `studio` | FastAPI, numpy, … | the Studio web app; numpy is also used by CMA-ES |
| `docs` | MkDocs Material, matplotlib, Selenium | building this site and its figures |

The full platform (Zephyr workspace, Renode download, Studio) has its own bootstrapper; see [Installation and bootstrap](../user/INSTALLATION.md).

## External simulators

embeddyn starts or loads each simulator only when a project uses it.

| Simulator | How embeddyn finds it | Check |
|---|---|---|
| **Renode** ≥ 1.17 (or the custom-cores fork) | `RENODE_BIN`, else `renode` on `PATH` | `"$RENODE_BIN" --version` |
| **ngspice** shared library | `NGSPICE_LIBRARY`, the instance's `library:` option, else the system `libngspice` | `python -c "import ctypes,os; ctypes.CDLL(os.environ['NGSPICE_LIBRARY'])"` |
| **MuJoCo** | the `mujoco` Python package | `python -c "import mujoco; print(mujoco.__version__)"` |
| **Webots** | `webots` on `PATH` or the instance's `executable:` | `webots --version` |
| **KiCad** | `kicad-cli` on `PATH` | `kicad-cli version` |

`embeddyn mechatronics doctor` prints what it found on `PATH` (`null` = not found), for example:

```json
{
  "tools": {"webots": null, "ngspice": "/usr/bin/ngspice", "ltspice": null, "kicad-cli": null, "renode": "/opt/renode/renode"},
  "notes": {"ltspice": "Windows/macOS executable may need --executable in project YAML"}
}
```

!!! note "Renode version"
    The SPI, I²C-feed and CAN features need Renode 1.17's External Control message protocol. The adapter picks the
    protocol from `renode --version` (`api: auto`). Older custom-cores builds still work for ADC, GPIO, PWM and memory
    bindings through the legacy protocol. To build the fork yourself see [Build Renode with custom cores](../guides/build-renode.md).

## Firmware toolchain

The firmware examples ship as C sources with a Makefile (`examples/mechatronics/firmware/*/`). Building them needs `arm-none-eabi-gcc`:

```bash
make -C examples/mechatronics/firmware/arm-joint-l476
```

The resulting ELF is what the project file points at (`options.firmware`).

## Checking the installation

```bash
pytest tests/mechatronics -q
```

Tests that need a simulator skip themselves when it is missing; set `RENODE_BIN`, `NGSPICE_LIBRARY` and install `mujoco` to run them all.

!!! tip "Temporary files"
    Renode logs, checkpoints and tuning runs can produce a lot of temporary data. Point `TMPDIR` at a disk with room
    (and pass `--basetemp` to pytest) if your root partition is small.
