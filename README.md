# Embeddyn

Copyright (c) 2023 [Antmicro](https://www.antmicro.com)

> Embeddyn is the unified multi-domain mechatronic emulation and simulation platform developed from this codebase. It combines embedded firmware emulation, electrical and mechanical co-simulation, KiCad/Webots/SPICE/Renode integration, distributed experiments, HIL safety, and digital-twin workflows. The original upstream copyright and licensing notices are retained.

The Embeddyn app is a local multi-tool which incorporates the diagramming capabilities of the online [Embeddyn](https://designer.antmicro.com/) which can be used for building block design of embedded systems in a diagramming workflow.
For more background on Embeddyn and its goals, please read the [introductory blog note on Antmicro's blog](https://antmicro.com/blog/2023/09/build-embedded-systems-with-embeddyn/).

The tool can also be used to generate and build [Zephyr RTOS](https://zephyrproject.org/)-based firmware and simulate it using [Renode](https://www.renode.io), Antmicro's open source simulation framework, visualizing the state of the simulation.

## Documentation

The documentation is an [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) site built from [`docs/`](docs/index.md):

```bash
pip install -e ".[docs]"
mkdocs serve        # http://127.0.0.1:8000
```

| Section | Start here |
|---|---|
| Getting started | [installation, first simulation, core concepts](docs/getting-started/index.md) |
| Tutorials | [eleven step-by-step lessons](docs/tutorials/index.md), from a DC motor loop to STM32 firmware with SPI/I²C sensors, tuning and tolerance analysis |
| Theory of operation | [how the master algorithm, adapters and analysis methods work](docs/theory/index.md) |
| How-to guides | [plants, SPI sensor models, firmware bindings, parallel runs, Renode builds](docs/guides/index.md) |
| Reference | [project file, CLI, every adapter](docs/reference/index.md) |
| Platform | [Studio, Zephyr builds, RF/EMC, HIL, digital twin](docs/user/README.md) |
| Contributing | [contributor guide](docs/contributing/README.md) and [C4 architecture](docs/c4/README.md) |

Every plot in the docs comes from a real run of `examples/mechatronics/` (`tools/docs/make_figures.py`), and the Lab screenshots from the running Lab (`tools/docs/capture_lab.py`).

The root [`CONTRIBUTING.md`](CONTRIBUTING.md) is a short discoverable entry point for contributors.


## Mechatronic co-simulation

embeddyn couples real firmware in Renode, circuits in ngspice, multibody mechanics in MuJoCo, Python plants and FMI 2.0/3.0 FMUs in one deterministic co-simulation, then measures, tunes (Nelder–Mead, CMA-ES), analyses tolerances (Monte Carlo, Sobol), estimates parameters from bench data and verifies models. Start with:

```bash
embeddyn mechatronics run examples/mechatronics/dc-motor.yaml --mock
embeddyn mechatronics gui examples/mechatronics/rc-servo-lab.yaml --open
```

and [Your first simulation](docs/getting-started/first-simulation.md). The plan toward full firmware + electronics + mechanics emulation and tuning is in the [roadmap](docs/project/roadmap.md).


## RF / EMC simulation perspective

Embeddyn Local Studio now includes an **RF / EMC** perspective for antenna tuning and generated-noise analysis. It can calculate a microstrip patch starting geometry from the PCB stack-up, generate a standalone openEMS full-wave model, inspect Touchstone S-parameters (S11, VSWR, impedance, return-loss bandwidth and first-order tuning guidance), close the geometry-tuning loop from the simulated/measured resonance, FFT simulated/session waveforms for switching harmonics, and apply custom engineering limit masks. The Studio view is an engineering cockpit with a scale-proportional antenna/stack-up drawing, workflow/status cards, project-level likely-noise-source hints, linked S11 and Smith-chart views, a log-frequency EMI spectrum with harmonic/mask markers, and stale-result warnings when analysis inputs change.

```bash
python bootstrap.py bootstrap --profile rf
embeddyn rf status
embeddyn rf patch-seed --target-hz 2.45e9 --epsilon-r 4.4 --substrate-mm 1.6
embeddyn rf patch-openems-script patch_2450.py --target-hz 2.45e9 --epsilon-r 4.4 --substrate-mm 1.6
embeddyn rf touchstone examples/rf/antenna_fixture.s1p --target-hz 2.45e9
```

See [RF / EMC simulation perspective](docs/RF_EMC_PERSPECTIVE.md) for the Studio workflow, openEMS integration, RF-noise limitations and recommended simulation-to-VNA/pre-compliance loop.

## Demo

https://github.com/antmicro/visual-system-designer-app/assets/114056459/9262c9db-82ad-4abf-ac39-a331427065c2

## Prerequisites

The Embeddyn application currently depends on other projects: kenning-pipeline-manager, Zephyr and Renode, therefore their dependencies must be installed first.
Make sure that you have installed all the programs mentioned below.
Any other dependencies (e.g. Python requirements or Zephyr workspace) will be downloaded later by the setup script.

(the following package names are for Debian based systems)

* [Pipeline Manager dependencies](https://github.com/antmicro/kenning-pipeline-manager#prerequisites)

  ```
  npm python3 python3-pip
  ```
* [Zephyr dependencies](https://docs.zephyrproject.org/latest/develop/getting_started/index.html#install-dependencies)

  ```
  git cmake ninja-build gperf ccache dfu-util device-tree-compiler wget python3-dev python3-pip python3-setuptools \
  python3-tk python3-wheel xz-utils file make gcc gcc-multilib g++-multilib libsdl2-dev libmagic1
  ```
* [Renode dependencies](https://github.com/renode/renode#installing-dependencies)

  ```
  mono-complete
  ```

NOTE: On Arch based systems additionally the `gtk-sharp` package must be installed to successfully run Renode.

## Setup

Embeddyn includes a west-like bootstrap/dependency manager. For the full Python and source dependency set:

```bash
python3 bootstrap.py bootstrap --profile full
. ./.embeddyn/env.sh
```

To also install Debian/Ubuntu host packages and initialize the complete Zephyr + SDK + Renode workspace:

```bash
python3 bootstrap.py bootstrap --profile full --install-system --workspace
. ./.embeddyn/env.sh
```

Dependency revisions are declared in `embeddyn.yml` and frozen in `embeddyn.lock.json`. Normal `sync` reproduces the lock; explicit `update` advances manifest revisions. See [Bootstrap and dependency versioning](docs/BOOTSTRAP_AND_DEPENDENCY_VERSIONING.md) for profiles, Windows launchers, source-archive handling and Python locking.

## Starting the Embeddyn application

The most convenient way to run Embeddyn is to use it interactively:

```
embeddyn run
```

After running this command the Embeddyn server will start and the application can be used in a web browser (by default at http://localhost:9000).
It can be used to design a graph of the platform, build an example Zephyr application on designed platform and run it in simulation using Renode.

To adjust the options used to start the Embeddyn application use the following options (those can be listed with `--help`):

```
Usage: embeddyn run [OPTIONS]

Options:
  --application PATH          [default: demo/blinky-temperature]
  --website-host TEXT         [default: 127.0.0.1]
  --website-port INTEGER      [default: 9000]
  --embeddyn-backend-host TEXT     [default: 127.0.0.1]
  --embeddyn-backend-port INTEGER  [default: 5000]
  --remote / --no-remote      [default: no-remote]
  --spec-mod PATH
  --verbosity TEXT            [default: INFO]
```

## Using the Embeddyn application

After the Embeddyn application is launched it can be used to design graphs.
Graphs can either be designed from scratch or from another graph imported using the "Load graph file" option.

Embeddyn is also capable of running a Zephyr demo application based on the created graphs.

To build Zephyr on the current graph use the "Build" button.
After the build has succeeded, simulation may be run using the "Run simulation" button.
The build logs and Zephyr console output are available in dedicated terminals on the bottom of the screen.

### Modifying specification

If you want to add some new nodes to the specification or modify existing ones you have to define a JSON file with such modifications.
An example file (with modifications needed while running Embeddyn interactively) is present in [embeddyn/spec_mods/interactive.json](./embeddyn/spec_mods/interactive.json).
After defining such files you must specify their paths while starting Embeddyn app (they will be applied in the order they were specified):

```
embeddyn run --spec-mod mod1.json --spec-mod mod2.json
# or
embeddyn prepare-zephyr-board board-graph.json --spec-mod mod1.json --spec-mod mod2.json
```

To modify specification without running Embeddyn app you may use [tools/modify_specification.py](./tools/modify_specification.py) script.

#### Specification modifications file format

On the high level, the JSON file, which contains the description of modifications, has three keys:

* `"metadata"` -- each field specified here will replace the one in metadata of original specification
* `"add_nodes"` -- new nodes will be directly added to the original specification
* `"mods"` -- each entry in this section describes how to modify a group of nodes specified in `"names"` list:
  - `"add_property"` -- properties specified here will be added to all specified nodes
  - `"add_interface"` -- interfaces specified here will be added to all specified nodes

Example file:
```JSON
{
    "metadata": {
        "notifyWhenChanged": true
    },
    "add_nodes": [
        {
            "abstract": false,
            "category": "Category/SomeNode",
            "name": "SomeNode",
            "properties": [
                {
                    "default": "",
                    "name": "property1",
                    "type": "text"
                }
            ],
            "interfaces": [
                {
                    "direction": "inout",
                    "maxConnectionsCount": -1,
                    "name": "interface1",
                    "side": "left",
                    "type": "interface1"
                }
            ]
        }
    ],
    "mods": [
        {
            "names": ["LED"],
            "add_properties": [
                    {
                        "default": false,
                        "name": "active",
                        "type": "bool"
                    }
            ]
        },
        {
            "names": [
                "bme280",
                "sht4xd",
                "tmp108",
                "si7210"
            ],
            "add_properties": [
                {
                    "default": 20.0,
                    "name": "temperature",
                    "type": "number"
                }
            ]
        }
    ]
}
```

## Using Embeddyn from command line

Embeddyn can also be used as a command line utility to prepare and simulate a demo on graph created with Embeddyn.
There are two available commands: `prepare-zephyr-app` and `simulate`.
These commands process graph obtained earlier (e.g. from [designer.antmicro.com](https://designer.antmicro.com) or using `embeddyn run` command).

### `prepare-zephyr-app` command

This command is used to prepare and build Zephyr application for given board graph.

```
usage: embeddyn prepare-zephyr-app graph_file source_dir [--from-template template_dir] [--force]
```

It requires providing:

* `graph_file` - file defining the Embeddyn graph representing the design
* `source_dir` - a directory where the Zephyr project is placed (or where the generated project from template should be placed)

There are two possible options to provide application sources for this command:

- `--from-template` - specify the directory which contains a template for the project
- `--force` - specify the application template (by name or directory), which will be used to generate the application sources. Currently there is only one template available to use specifying its name: `blinky-temperature`.

#### Example

Few basic usage examples:

- Building demo from sources:
  ```
  embeddyn prepare-zephyr-app demo/stm32-led-thermometer.json demo/blinky-temperature
  ```
- Building demo from template:
  ```
  embeddyn prepare-zephyr-app demo/stm32-led-thermometer.json ./my-project --from-template demo/templates/blinky-temperature/
  ```

### `simulate` command

This command is used to start Renode simulation of the demo build in the previous step.
The `board_name`, which has to be specified as an argument, is obtained from the graph name by substituting all white and special characters with underscore.
The board name is also printed in the previous step.

```
usage: embeddyn simulate board_name
```

#### Example

Firstly, building demo, e.g. from template as demonstrated in `prepare-zephyr-app`:

```
embeddyn prepare-zephyr-app demo/stm32-led-thermometer.json ./my-blinky --from-template demo/templates/blinky-temperature/
```

Secondly, run `embeddyn simulate` with board name, here:

```
embeddyn simulate demo_blinky_temp
```

## Example application

The Embeddyn app comes with its own Zephyr demo ([demo/blinky-temperature](./demo/blinky-temperature/)) which can be used on a predefined graph ([stm32-led-thermometer.json](./demo/stm32-led-thermometer.json)).
To run that demo interactively you can start the Embeddyn app, import the graph and run the application using "Run" button.

To prepare and run the demo in the shell execute following commands:

```
embeddyn prepare-zephyr-app demo/stm32-led-thermometer.json demo/blinky-temperature
embeddyn simulate demo_blinky_temp
```

## Demo with frontend hosted on a remote server

### Building and hosting Pipeline Manager

In order to build Pipeline Manager frontend, create the `venv` environment and install [Pipeline Manager](https://github.com/antmicro/kenning-pipeline-manager):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/antmicro/kenning-pipeline-manager.git
```

The frontend requires additional assets (icons, graphics, ...) to render properly - they can be obtained from [upstream Visual System Designer resources repository](https://github.com/antmicro/visual-system-designer-resources):

```sh
git clone https://github.com/antmicro/visual-system-designer-resources.git
```

After obtaining all requirements, the frontend can be built with:

```sh
pipeline_manager build server-app --communication-server-host localhost --communication-server-port 9000 --output-directory website --workspace-directory pm-workspace --assets-directory embeddyn-resources/assets/
```

The `--communication-server-host` and `--communication-server-port` specify the address from which the `embeddyn run` command will connect (from the user desktop perspective, hence `localhost` is sufficient).

The `website` directory can now be served using any HTTP server (e.g. the one included in Python3 distribution):

```sh
python3 -m http.server -d ./website
```

### Running the demo

Assuming the commands are executed in the root directory for this project:

1. Prepare the workspace as described in [Setup](#setup).
1. Start Embeddyn app (the `--application <dir>` is the path to the sources for the Zephyr application)
    ```sh
    embeddyn run --app demo/blinky-temperature
    ```
2. Go to address hosting Pipeline Manager (using above Python server go to http://localhost:8000).
3. Use Embeddyn as usual (e.g. load [`embeddyn/demo/stm32-led-thermometer.json`](demo/stm32-led-thermometer.json) and click "Run").

## License

This project is published under the [Apache-2.0](LICENSE) license.

### Phase 4 engineering integration

```bash
embeddyn mechatronics integration-validate examples/mechatronics/dc-motor.yaml
embeddyn mechatronics catalog-list --root components/mechatronics
embeddyn mechatronics kicad-inspect examples/mechatronics/motor.cir
```

The integration layer validates Webots/Renode signal bindings and SPICE artifacts even when external engineering tools are not installed. External executable availability is reported separately for deployment qualification.


## Digital twin and evidence workflows

Phase 6 adds measurement synchronization, simulation-to-real comparison, calibration fitting, uncertainty sampling, requirements verification, and evidence-report generation. See `docs/project/history/phase6-completion.md`.
