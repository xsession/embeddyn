# Getting started

This section takes you from a fresh checkout to a running co-simulation in about ten minutes, then introduces the handful of ideas every other page relies on.

1. **[Installation](installation.md)** — Python package, and the optional simulators (Renode, ngspice, MuJoCo, FMI tools) with how embeddyn finds them.
2. **[Your first simulation](first-simulation.md)** — validate, run and plot the DC motor example; read the run directory.
3. **[Core concepts](concepts.md)** — signals, couplings, instances, the macro step and the adapters.

After that, the [tutorials](../tutorials/index.md) build up one capability at a time, and the [theory of operation](../theory/index.md) explains what happens underneath.

!!! tip "No simulators installed? Use `--mock`."
    Every example runs with `--mock`, which swaps firmware, electrical and mechanical backends for built-in deterministic
    models. That is enough for the first two tutorials and for checking a project file before the real tools are set up.
