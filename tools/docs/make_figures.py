"""Regenerate the documentation figures in docs/images/ from real embeddyn runs.

Usage (from the embeddyn root):

    tools/docs/make_figure_data.sh DATA_DIR     # runs the examples (Renode, ngspice, MuJoCo, tuning, tolerance ...)
    python tools/docs/make_figures.py DATA_DIR  # draws docs/images/*.png

The data script needs RENODE_BIN and NGSPICE_LIBRARY for the firmware and
ngspice figures; figures whose data is missing are skipped with a note.
A few figures (estimation fit, tuned step responses, timing diagram) are
computed here directly because they only need the Python plants.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))

# One palette for every figure (colour-blind safe, readable on light and dark pages).
C = {"blue": "#2563eb", "orange": "#ea580c", "green": "#16a34a", "purple": "#7c3aed",
     "red": "#dc2626", "grey": "#64748b", "teal": "#0d9488", "amber": "#d97706"}

plt.rcParams.update({
    "figure.dpi": 110, "savefig.dpi": 160, "savefig.bbox": "tight", "savefig.facecolor": "white",
    "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.labelsize": 10,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25,
    "legend.frameon": False, "legend.fontsize": 9, "lines.linewidth": 1.6,
})


def read_csv(path: Path) -> dict[str, list[float]]:
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    columns: dict[str, list[float]] = {}
    for key in rows[0]:
        try:
            columns[key] = [float(row[key]) for row in rows]
        except (TypeError, ValueError):
            continue
    return columns


def save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name)
    plt.close(fig)
    print("wrote", OUT / name)


def need(data: Path, *names: str) -> bool:
    missing = [name for name in names if not (data / name).exists()]
    if missing:
        print("skip: missing", ", ".join(missing))
    return not missing


# ---------------------------------------------------------------------------------------------- tutorials
def dc_motor(data: Path) -> None:
    if not need(data, "dc-motor.csv"):
        return
    d = read_csv(data / "dc-motor.csv")
    t = d["time"]
    fig, axes = plt.subplots(3, 1, figsize=(8, 6.2), sharex=True)
    axes[0].plot(t, d["firmware.target_velocity"], color=C["grey"], ls="--", label="target")
    axes[0].plot(t, d["mechanical.velocity"], color=C["blue"], label="shaft speed")
    axes[0].set_ylabel("speed [rad/s]")
    axes[0].set_title("DC motor speed loop: firmware → electrical → mechanical")
    axes[0].legend(loc="lower right")
    axes[1].plot(t, d["firmware.pwm_duty"], color=C["orange"])
    axes[1].set_ylabel("PWM duty")
    axes[2].plot(t, d["electrical.motor_current"], color=C["green"], label="current")
    axes[2].set_ylabel("current [A]")
    axes[2].set_xlabel("time [s]")
    save(fig, "tutorial-dc-motor.png")


def ngspice(data: Path) -> None:
    if not need(data, "dc-motor-ngspice.csv", "dc-motor.csv"):
        return
    spice, ideal = read_csv(data / "dc-motor-ngspice.csv"), read_csv(data / "dc-motor.csv")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    axes[0].plot(ideal["time"], ideal["mechanical.velocity"], color=C["grey"], ls="--", label="averaged model")
    axes[0].plot(spice["time"], spice["mechanical.velocity"], color=C["blue"], label="ngspice H-bridge")
    axes[0].set_title("Shaft speed")
    axes[0].set_xlabel("time [s]")
    axes[0].set_ylabel("rad/s")
    axes[0].legend()
    axes[1].plot(ideal["time"], ideal["electrical.motor_current"], color=C["grey"], ls="--", label="averaged model")
    axes[1].plot(spice["time"], spice["electrical.motor_current"], color=C["orange"], label="ngspice H-bridge")
    axes[1].set_title("Motor current")
    axes[1].set_xlabel("time [s]")
    axes[1].set_ylabel("A")
    axes[1].legend()
    fig.suptitle("Same controller, two electrical models", fontweight="bold")
    save(fig, "tutorial-ngspice.png")


def rc_servo(data: Path) -> None:
    if not need(data, "rc-servo-l476.csv"):
        return
    d = read_csv(data / "rc-servo-l476.csv")
    t = d["time"]
    fig, axes = plt.subplots(3, 1, figsize=(8, 6.4), sharex=True)
    axes[0].plot(t, d["mcu.setpoint_deg"], color=C["grey"], ls="--", label="firmware set-point")
    axes[0].plot(t, d["servo.angle_deg"], color=C["blue"], label="servo horn angle")
    axes[0].plot(t, [math.degrees(v) for v in d["servo.decoded_angle"]], color=C["orange"], lw=1, ls=":", label="angle the servo decodes from the pulse")
    axes[0].set_ylabel("angle [deg]")
    axes[0].set_title("STM32L476 firmware in Renode commanding an RC servo")
    axes[0].legend(loc="best")
    axes[1].plot(t, [v * 1e3 if v < 1 else v for v in d["mcu.servo_pulse"]], color=C["purple"])
    axes[1].set_ylabel("pulse width [ms]")
    axes[2].plot(t, d["operator.command_voltage"], color=C["teal"], label="operator pot [V]")
    axes[2].plot(t, d["servo.current"], color=C["green"], label="servo current [A]")
    axes[2].plot(t, d["arm.load_torque"], color=C["red"], lw=1, label="load torque [N·m]")
    axes[2].set_xlabel("time [s]")
    axes[2].legend(loc="best")
    save(fig, "tutorial-rc-servo.png")


def rc_servo_fault(data: Path) -> None:
    if not need(data, "rc-servo-fault.csv"):
        return
    d = read_csv(data / "rc-servo-fault.csv")
    t = d["time"]
    start = 1.2
    pot_deg = [(v - 1.65) * 180 / 2.7 for v in d["servo.feedback_voltage"]]      # feedback pot: 2.7 V per 180 deg
    trip = next((ti for ti, led in zip(t, d["mcu.fault_led"]) if led > 0.5), None)
    fig, axes = plt.subplots(2, 1, figsize=(8, 5.2), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
    for ax in axes:
        ax.axvspan(start, t[-1], color=C["red"], alpha=0.06, lw=0)
    axes[0].plot(t, d["mcu.setpoint_deg"], color=C["grey"], ls="--", label="firmware set-point")
    axes[0].plot(t, pot_deg, color=C["orange"], label="feedback pot as the firmware reads it")
    axes[0].plot(t, d["servo.angle_deg"], color=C["blue"], label="true horn angle")
    axes[0].set_ylabel("angle [deg]")
    axes[0].set_title("Injected fault: the feedback pot sticks at 1.2 s")
    axes[0].annotate("pot stuck", (start, pot_deg[t.index(min(t, key=lambda x: abs(x - start - 0.05)))]),
                     xytext=(start - 0.5, -75), color=C["red"], arrowprops={"arrowstyle": "->", "color": C["red"]})
    if trip is not None:
        axes[0].annotate(f"stall monitor trips\nat {trip:.2f} s: pulses stop,\nthe load back-drives\nthe horn",
                         (trip, 28), xytext=(trip + 0.27, -88), color=C["red"], fontsize=9,
                         arrowprops={"arrowstyle": "->", "color": C["red"]})
    axes[0].legend(loc="lower left")
    axes[1].plot(t, [v * 1e3 for v in d["mcu.servo_pulse"]], color=C["purple"], label="pulse width [ms]")
    axes[1].plot(t, d["mcu.fault_led"], color=C["red"], drawstyle="steps-post", label="fault LED (PA5)")
    axes[1].set_xlabel("time [s]")
    axes[1].legend(loc="center left")
    save(fig, "tutorial-rc-servo-fault.png")


def arm_firmware(data: Path) -> None:
    if not need(data, "mujoco-arm-firmware.csv"):
        return
    d = read_csv(data / "mujoco-arm-firmware.csv")
    t = d["time"]
    fig, axes = plt.subplots(2, 2, figsize=(11, 6.4), sharex=True)
    ax = axes[0, 0]
    ax.plot(t, d["mcu.target"], color=C["grey"], ls="--", label="target")
    ax.plot(t, d["arm.angle"], color=C["blue"], label="arm angle (MuJoCo)")
    ax.plot(t, d["mcu.angle"], color=C["orange"], lw=1, label="firmware: quadrature")
    ax.plot(t, d["mcu.abs_angle"], color=C["green"], lw=1, ls=":", label="firmware: AS5047P (SPI)")
    ax.set_ylabel("rad")
    ax.set_title("Position: plant vs what the firmware measures")
    ax.legend(fontsize=8)
    ax = axes[0, 1]
    ax.plot(t, d["mcu.duty"], color=C["purple"], label="duty command")
    ax.plot(t, d["mcu.duty_limit"], color=C["red"], ls="--", label="thermal duty limit")
    ax.set_title("Drive")
    ax.legend(fontsize=8)
    ax = axes[1, 0]
    ax.plot(t, d["motor.current"], color=C["amber"])
    ax.set_ylabel("A")
    ax.set_title("Motor current")
    ax.set_xlabel("time [s]")
    ax = axes[1, 1]
    ax.plot(t, d["motor.winding_temperature"], color=C["red"], label="winding (thermal RC)")
    ax.plot(t, d["mcu.motor_temperature"], color=C["teal"], ls="--", label="firmware: AS6221 (I²C)")
    ax.set_ylabel("°C")
    ax.set_title("Temperature")
    ax.set_xlabel("time [s]")
    ax.legend(fontsize=8)
    fig.suptitle("Arm joint: Renode firmware + H-bridge + MuJoCo + SPI/I²C sensors", fontweight="bold")
    save(fig, "tutorial-arm-firmware.png")


def mujoco_arm(data: Path) -> None:
    if not need(data, "mujoco-arm.csv"):
        return
    d = read_csv(data / "mujoco-arm.csv")
    t = d["time"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    axes[0].plot(t, d["target.angle"], color=C["grey"], ls="--", label="target")
    axes[0].plot(t, d["arm.angle"], color=C["blue"], label="arm angle")
    axes[0].set_title("Joint angle")
    axes[0].set_xlabel("time [s]")
    axes[0].set_ylabel("rad")
    axes[0].legend()
    axes[1].plot(t, d["motor.torque"], color=C["orange"], label="motor torque")
    axes[1].plot(t, d["arm.joint_torque"], color=C["green"], lw=1, label="joint torque")
    axes[1].set_title("Torques")
    axes[1].set_xlabel("time [s]")
    axes[1].set_ylabel("N·m")
    axes[1].legend()
    save(fig, "tutorial-mujoco-arm.png")


def two_wheel(data: Path) -> None:
    if not need(data, "two-wheel-drive.csv"):
        return
    d = read_csv(data / "two-wheel-drive.csv")
    t = d["time"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    axes[0].plot(t, d["left.target_speed"], color=C["grey"], ls="--", label="targets")
    axes[0].plot(t, d["right.target_speed"], color=C["grey"], ls="--")
    axes[0].plot(t, d["left.wheel_speed"], color=C["blue"], label="left wheel")
    right = d.get("right.wheel_speed") or d.get("right.motor_speed_scaled")
    if right:
        axes[0].plot(t, right, color=C["orange"], label="right wheel")
    axes[0].set_title("Wheel speeds")
    axes[0].set_xlabel("time [s]")
    axes[0].legend()
    axes[1].plot(t, d["left.current"], color=C["blue"], label="left")
    axes[1].plot(t, d["right.current"], color=C["orange"], label="right")
    axes[1].plot(t, d["bus.voltage"], color=C["grey"], lw=1, label="bus voltage [V]")
    axes[1].set_title("Motor currents (one 24 V supply)")
    axes[1].set_xlabel("time [s]")
    axes[1].legend()
    save(fig, "tutorial-two-wheel.png")


def tuning(data: Path) -> None:
    if not need(data, "tune-nelder-mead.json", "tune-cmaes.json"):
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.0))
    for method, color in (("nelder-mead", C["blue"]), ("cmaes", C["orange"])):
        result = json.loads((data / f"tune-{method}.json").read_text())
        history = result["history"]
        best, curve = math.inf, []
        for entry in history:
            if entry["objective"] is not None and not entry["violations"]:
                best = min(best, entry["objective"])
            curve.append(best)
        label = f"{'Nelder–Mead' if method == 'nelder-mead' else 'CMA-ES'} ({result['runs']} runs, {result['wall_seconds']:.0f} s)"
        axes[0].plot(range(1, len(curve) + 1), curve, color=color, label=label)
        kp = [entry["parameters"]["controller.kp"] for entry in history]
        ki = [entry["parameters"]["controller.ki"] for entry in history]
        axes[1].scatter(kp, ki, s=14, color=color, alpha=0.55, label=label.split(" (")[0])
        b = result["best"]["parameters"]
        axes[1].scatter([b["controller.kp"]], [b["controller.ki"]], s=120, marker="*", color=color, edgecolor="black", zorder=5)
    start = result["initial"]["parameters"]
    axes[1].scatter([start["controller.kp"]], [start["controller.ki"]], s=60, marker="s", color="white", edgecolor="black", zorder=6, label="start")
    axes[0].set_title("Best feasible objective so far")
    axes[0].set_xlabel("simulation runs")
    axes[0].set_ylabel("objective (ITAE + gearbox load)")
    axes[0].legend()
    axes[1].set_xscale("log")
    axes[1].set_yscale("log")
    for axis in (axes[1].xaxis, axes[1].yaxis):
        axis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%g"))
        axis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    axes[1].set_title("Where each method evaluated (★ = best)")
    axes[1].set_xlabel("kp")
    axes[1].set_ylabel("ki")
    axes[1].legend()
    save(fig, "tutorial-tuning-convergence.png")

    # Step response before and after tuning (Python plants only, computed here).
    from embeddyn.mechatronics.factory import build_adapters
    from embeddyn.mechatronics.model import MechatronicProject
    from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
    nm = json.loads((data / "tune-nelder-mead.json").read_text())
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for label, params, color in (("initial gains", nm["initial"]["parameters"], C["grey"]),
                                 ("tuned (Nelder–Mead)", nm["best"]["parameters"], C["blue"])):
        project = MechatronicProject.load(ROOT / "examples/mechatronics/servo-drivetrain.yaml")
        project.apply_parameter_overrides(params)
        project.duration_seconds = 0.6
        rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
        ax.plot([r["time"] for r in rows], [r["mechanical.velocity"] for r in rows], color=color,
                label=f"{label}: kp={params['controller.kp']:.2f}, ki={params['controller.ki']:.2f}")
    ax.plot([r["time"] for r in rows], [r["firmware.target_velocity"] for r in rows], color="black", lw=0.8, ls="--", label="target")
    ax.set_title("Geared servo with backlash: step response before and after tuning")
    ax.set_xlabel("time [s]")
    ax.set_ylabel("load speed [rad/s]")
    ax.legend()
    save(fig, "tutorial-tuning-response.png")


def tolerance(data: Path) -> None:
    if not need(data, "tol.json"):
        return
    result = json.loads((data / "tol.json").read_text())
    mc = result["monte_carlo"]
    samples = mc["sample_set"]
    fig, axes = plt.subplots(1, 3, figsize=(14, 3.8), gridspec_kw={"wspace": 0.55})
    for ax, metric, limit, color in ((axes[0], "speed.overshoot", None, C["blue"]),
                                     (axes[1], "shaft.peak", 20.0, C["orange"])):
        values = [s["metrics"][metric] for s in samples if s.get("metrics", {}).get(metric) is not None]
        ax.hist(values, bins=24, color=color, alpha=0.8)
        if limit is not None:
            ax.axvline(limit, color=C["red"], ls="--", label=f"limit {limit:g}")
            ax.legend()
        ax.set_title(metric)
        ax.set_xlabel({"speed.overshoot": "overshoot [%]", "shaft.peak": "peak shaft torque [N·m]"}[metric])
        ax.set_ylabel("samples")
    ax = axes[2]
    sens = result["sensitivity"]["outputs"]["shaft.peak"]["indices"]
    names = list(sens)
    y = range(len(names))
    ax.barh([i + 0.2 for i in y], [sens[n]["total"] for n in names], height=0.4, color=C["purple"], label="total-order")
    ax.barh([i - 0.2 for i in y], [max(0.0, sens[n]["first_order"]) for n in names], height=0.4, color=C["teal"], label="first-order")
    ax.set_yticks(list(y))
    ax.set_yticklabels([n.split(".", 1)[1] for n in names], fontsize=8)
    ax.set_title("Sobol indices for shaft.peak")
    ax.set_xlabel("share of variance")
    ax.legend()
    fig.suptitle(f"Monte Carlo ({mc['samples']} LHS samples): yield {mc['yield'] * 100:.1f} % "
                 f"(95 % CI {mc['yield_ci95'][0] * 100:.0f}–{mc['yield_ci95'][1] * 100:.0f} %)", fontweight="bold")
    save(fig, "tutorial-tolerance.png")


def estimation(data: Path) -> None:
    if not need(data, "est.json"):
        return
    from embeddyn.mechatronics.estimation import build_project, load_estimation_spec
    from embeddyn.mechatronics.factory import build_adapters
    from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
    result = json.loads((data / "est.json").read_text())
    spec = load_estimation_spec(ROOT / "examples/mechatronics/estimation/dc-motor.yaml")
    bench = read_csv(spec.measurements)
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    for label, values, color, style in (
            ("initial guess", {k: v["initial"] for k, v in result["parameters"].items()}, C["grey"], "--"),
            ("estimate", {k: v["estimate"] for k, v in result["parameters"].items()}, C["blue"], "-")):
        project = build_project(spec, values, bench["time"][-1])
        rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
        t = [r["time"] for r in rows]
        axes[0].plot(t, [r["mechanical.velocity"] for r in rows], color=color, ls=style, label=label, zorder=3)
        axes[1].plot(t, [r["electrical.motor_current"] for r in rows], color=color, ls=style, label=label, zorder=3)
    axes[0].scatter(bench["time"][::6], bench["speed"][::6], s=4, color=C["orange"], label="bench (noisy)", zorder=2)
    axes[1].scatter(bench["time"][::6], bench["current"][::6], s=4, color=C["orange"], label="bench (noisy)", zorder=2)
    axes[0].set_title("Speed")
    axes[0].set_xlabel("time [s]")
    axes[0].set_ylabel("rad/s")
    axes[0].legend()
    axes[1].set_title("Current")
    axes[1].set_xlabel("time [s]")
    axes[1].set_ylabel("A")
    truth = {"electrical.motor_resistance": 2.4, "electrical.motor_inductance": 0.0045, "mechanical.inertia": 0.025,
             "mechanical.damping": 0.06, "mechanical.load_torque": 0.05}
    ax = axes[2]
    iterations = [0] + [h["iteration"] for h in result["history"]]
    for (name, true), color in zip(truth.items(), [C["blue"], C["orange"], C["green"], C["purple"], C["red"]]):
        path = [result["parameters"][name]["initial"]] + [h["parameters"][name] for h in result["history"]]
        ax.plot(iterations, [v / true for v in path], marker="o", ms=3, color=color, label=name.split(".")[1])
    ax.axhline(1.0, color="black", lw=0.8)
    ax.set_title("Levenberg–Marquardt iterations")
    ax.set_xlabel("iteration")
    ax.set_ylabel("estimate / true value")
    ax.legend(fontsize=8)
    save(fig, "tutorial-estimation.png")


# ---------------------------------------------------------------------------------------------- theory
def timing_diagram() -> None:
    """One macro step of a multi-rate schedule with Gauss-Seidel ordering and a delayed feedback."""
    fig, ax = plt.subplots(figsize=(10, 3.8))
    lanes = [("firmware (mcu)", 1e-3, C["purple"]), ("electrical (bridge)", 0.25e-3, C["orange"]),
             ("mechanical (arm)", 0.5e-3, C["blue"]), ("thermal (winding)", 1e-3, C["red"])]
    macro = 1e-3
    for row, (name, step, color) in enumerate(lanes):
        y = len(lanes) - row
        ax.text(-0.06e-3, y, name, ha="right", va="center", fontsize=9)
        ax.plot([0, 2 * macro], [y, y], color="#cbd5e1", lw=1, zorder=1)
        k = 1
        while k * step <= 2 * macro + 1e-12:
            t = k * step
            ax.add_patch(FancyBboxPatch((t - step + step * 0.06, y - 0.18), step * 0.88, 0.36,
                                        boxstyle="round,pad=0,rounding_size=0.00002", color=color, alpha=0.85, lw=0, zorder=2))
            ax.plot([t], [y], marker="|", color="black", ms=12, zorder=3)
            k += 1
    for edge in (0, macro, 2 * macro):
        ax.axvline(edge, color="black", lw=1, ls=":")
    ax.annotate("", xy=(macro, len(lanes) + 0.65), xytext=(0, len(lanes) + 0.65),
                arrowprops=dict(arrowstyle="<->", color="black"))
    ax.text(macro / 2, len(lanes) + 0.8, "macro step k  (t = k·h, integer index)", ha="center", fontsize=9)
    ax.annotate("couplings into a domain are applied\njust before its activation",
                xy=(0.25e-3, 3), xytext=(0.35e-3, 0.15), fontsize=8, ha="left",
                arrowprops=dict(arrowstyle="->", color=C["grey"]))
    ax.annotate("feedback mcu ← arm uses delay_steps: 1\n(value committed at the end of step k−1)",
                xy=(1.0e-3, 4), xytext=(1.25e-3, 0.15), fontsize=8, ha="left",
                arrowprops=dict(arrowstyle="->", color=C["grey"]))
    ax.set_xlim(-0.02e-3, 2.05e-3)
    ax.set_ylim(-0.3, len(lanes) + 1.1)
    ax.set_yticks([])
    ax.set_xticks([0, 0.5e-3, 1e-3, 1.5e-3, 2e-3])
    ax.set_xticklabels(["0", "0.5 ms", "1 ms", "1.5 ms", "2 ms"])
    ax.spines["left"].set_visible(False)
    ax.grid(False)
    ax.set_title("Multi-rate fixed-step schedule: each bar is one adapter step ending at its tick")
    save(fig, "theory-schedule.png")


def coupling_delay() -> None:
    """Why feedback needs a delay: zero-order hold over one macro step."""
    fig, ax = plt.subplots(figsize=(8, 3.0))
    h = 1.0
    t = [i * h for i in range(7)]
    plant = [0, 0.45, 0.8, 1.0, 1.1, 1.08, 1.02]
    held = plant[:-1]
    ax.plot(t, plant, "o-", color=C["blue"], label="plant output y(t_k) (committed at end of step k)")
    ax.step([x + h for x in t[:-1]], held, where="post", color=C["orange"], label="controller input: y(t_{k−1}), held for step k")
    ax.set_xlabel("macro step k")
    ax.set_ylabel("signal")
    ax.set_title("delay_steps: 1 — the controller sees the value from the previous macro step")
    ax.legend(loc="lower right")
    save(fig, "theory-delay.png")


def main(argv: list[str]) -> int:
    data = Path(argv[1]) if len(argv) > 1 else ROOT / ".embeddyn" / "doc-figures"
    for figure in (dc_motor, ngspice, rc_servo, rc_servo_fault, arm_firmware, mujoco_arm, two_wheel, tuning, tolerance, estimation):
        figure(data)
    timing_diagram()
    coupling_delay()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
