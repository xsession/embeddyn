# Outline of the peripheral reference document

One file next to the models (for example `tools/<mcu>/PERIPHERALS.md`):

1. Conventions:
   - register access;
   - clock source;
   - interrupt edges;
   - power states;
   - module disable;
   - time sync.
2. CPU-side registers (which data-space registers live in the core).
3. One section per model:
   - base address and registers;
   - behaviour as bullet rules;
   - timing formulas;
   - power behaviour;
   - pin routing;
   - monitor commands and properties for stimuli and fault injection.
4. Tests: a table of script and what it checks.
5. Known limits: what is not modelled, and why.
