#!/usr/bin/env bash
# Run the examples whose results the documentation figures show.
# Usage: tools/docs/make_figure_data.sh DATA_DIR   (from the embeddyn root)
# Needs RENODE_BIN (Renode >= 1.17) for the firmware runs and NGSPICE_LIBRARY
# for the ngspice run; missing tools make those runs fail, the rest still run.
# Takes about 10 minutes with 4 workers (tolerance is the longest).
set -u
DATA=${1:?usage: make_figure_data.sh DATA_DIR}
PY=${PYTHON:-python}
mkdir -p "$DATA"
run() { echo "== $*"; "$PY" -m embeddyn mechatronics "$@" > "$DATA/last.log" 2>&1 || { echo "   failed, see $DATA/last.log"; cp "$DATA/last.log" "$DATA/failed-$2.log"; }; }

for example in dc-motor servo-drivetrain mujoco-arm two-wheel-drive dc-motor-ngspice rc-servo-l476 mujoco-arm-firmware; do
  run run "examples/mechatronics/$example.yaml" -o "$DATA/$example.csv" --output-root "$DATA/runs"
done
for method in nelder-mead cmaes; do
  run tune examples/mechatronics/tuning/servo-pi.yaml --method "$method" --workers 4 --max-evaluations 60 -o "$DATA/tune-$method.json"
done
run tolerance examples/mechatronics/tolerance/servo.yaml --workers 4 -o "$DATA/tol.json"
run estimate examples/mechatronics/estimation/dc-motor.yaml -o "$DATA/est.json"
echo "data in $DATA; now: $PY tools/docs/make_figures.py $DATA"
