# Integration roadmap

1. **Current baseline** — deterministic master, mock plant, Webots JSON bridge, LTspice/ngspice batch adapters, Renode socket adapter, KiCad CLI export and IPC plugin skeleton.
2. **Studio UX** — add domain-specific nodes, coupling editor, unit validation, run controls and synchronized 3D/electrical/firmware timelines.
3. **Persistent simulators** — use ngspice shared library and persistent LTspice result bridge to avoid process-per-step overhead.
4. **Renode production bridge** — ship a Renode external peripheral exposing PWM, ADC, encoder, GPIO, CAN and fault signals over the existing protocol.
5. **Model exchange** — add FMI 2/3 co-simulation import/export and iterative algebraic-loop solving.
6. **CAD pipeline** — map KiCad symbols/nets and Webots PROTO fields into Embeddyn nodes, preserve stable IDs and validate signal units.
7. **Verification** — golden waveforms, energy-balance checks, step-size convergence, model-to-measurement calibration and CI scenario matrices.
8. **HIL** — optional real-time scheduler, deadline statistics, isolated hardware I/O and fail-safe shutdown policies.
