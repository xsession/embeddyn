# Light-curtain bench and Webots world

Infrared light curtains find a body without touching it: a row of IR LEDs faces a row of photodiodes, and a scanning controller lights the LEDs one at a time and reads the matching receiver. SPECT cameras use them for body contouring: the detector head moves in until a beam under its face breaks.

The **light-curtain bench** lets you test the two things that decide whether such a curtain works:

* **The scan algorithm:** which LEDs are lit and which receivers are read, slot by slot, and how the samples become "beam interrupted" decisions.
* **The LED and receiver choice:** half angle, intensity or total power, aperture, mounting error, receiver field of view and gain.

It runs headless in Python, which makes it fast enough for sweeps. The Webots world generated from the same file shows the scan in 3-D, using the same code.

| File | Role |
|---|---|
| `embeddyn/mechatronics/lightcurtain/scene.py` | primitives (box, sphere, ellipsoid, cylinder, capsule) and exact, vectorised ray casting |
| `embeddyn/mechatronics/lightcurtain/array.py` | LED and receiver rows, the coupling matrix, partial blocking, one-bounce stray light |
| `embeddyn/mechatronics/lightcurtain/scanning.py` | the scan-algorithm interface and the built-in algorithms |
| `embeddyn/mechatronics/lightcurtain/bench.py` | receiver chain per head, scenarios, metrics, sweeps |
| `embeddyn/mechatronics/lightcurtain/webots_world.py` | the Webots world generator |
| `examples/mechatronics/light-curtain/*.yaml` | the SPECT bench and two sweeps |
| `examples/mechatronics/protos/IrLightCurtain.proto` | the curtain PROTO: rails, LED devices for the emitters and receivers |
| `examples/mechatronics/controllers/light_curtain/` | the supervisor controller that runs the bench in Webots |
| `examples/mechatronics/worlds/spect_light_curtain.wbt` | the generated SPECT world |

The receiver chain is the same model as the MuJoCo co-simulation's `light_curtain` (`optics.py`). It covers ambient IR with synchronous rejection, shot noise, an amplifier noise floor, settling after the multiplexer switches, and a Schmitt-trigger comparator. Those firmware-in-the-loop runs are described in the [MuJoCo adapter](mujoco.md) and `spect-body-contour.yaml`.

## Quick start

```bash
embeddyn mechatronics curtain-run examples/mechatronics/light-curtain/spect-curtain.yaml
embeddyn mechatronics curtain-run examples/mechatronics/light-curtain/spect-curtain.yaml --scenario rod --algorithm crossed
embeddyn mechatronics curtain-run ... -p emitter.half_angle="20 deg" -p emitter.intensity="5 mW/sr" -o frames.csv
embeddyn mechatronics curtain-sweep examples/mechatronics/light-curtain/led-sweep.yaml -j 4 -o led-sweep.csv
embeddyn mechatronics curtain-sweep examples/mechatronics/light-curtain/spect-curtain.yaml \
    -g emitter.half_angle="5 deg,15 deg,30 deg" -g emitter.power="1 mW,3 mW" -g scenario.type=static
embeddyn mechatronics curtain-webots examples/mechatronics/light-curtain/spect-curtain.yaml \
    -o examples/mechatronics/worlds/spect_light_curtain.wbt
webots examples/mechatronics/worlds/spect_light_curtain.wbt
```

## Optics

For emitter *e* and receiver *r* at distance *d*, the coupling is

\[
P_{er} = T_{er}\; I_e \cos^{m_e}\theta_e \;\; A \cos^{m_r}\theta_r \,/\, d^2,
\qquad m = -\ln 2 / \ln\cos\theta_{1/2}
\]

The terms are:

* *T* is the unblocked fraction of the pencil between the LED lens and the photodiode aperture. It is found by casting rays between sample points on both discs: 49 rays per pair with `rays: 1`, 169 with `rays: 2`.
* *θe* and *θr* are the angles off each device's optical axis.
* *I_e* is the LED's on-axis radiant intensity, after per-LED spread and drive.

The diagonal of *P* holds the straight beams. The off-diagonal terms are what a receiver sees from the other LEDs: crossed beams for algorithms that use them, crosstalk for algorithms that light several LEDs at once.

* **Intensity or power.** Give `emitter.intensity` (W/sr on axis) to compare LEDs as a datasheet lists them. Give `emitter.power` (W total radiant flux) to compare at the same LED chip and current. With power fixed, a narrower LED puts more on the axis: *I₀ = Φ (m + 1) / 2π*.
* **Mounting.**
  * `tilt` and `yaw` aim all LEDs: about the rail axis (towards +z) and along the rail (towards +x).
  * `tilt_sigma` adds a random error per LED. The same seed gives the same LEDs, and the tilt and the intensity spread draw from separate random streams.
  * `spread` is the relative intensity sigma between LEDs.
* **Stray light** (`curtain.reflections`, off by default). Each lit LED shoots `rays` rays distributed like its cos^m lobe. Where a ray lands on a primitive, the patch reflects diffusely with `albedo`, which can be set per obstacle. Every receiver that sees the patch unobstructed collects that light. `face_albedo` adds the detector face above the curtain as a reflector.
* **Ground truth.** A beam is interrupted when *T* < 0.5. It is shadowed when *T* < 1. A partly shaded beam may read either way, because the comparator thresholds decide. Errors are therefore counted outside a don't-care band:
  * **missed:** reported clear while *T* < 0.25;
  * **false dark:** reported dark while *T* > 0.75.

## The bench file

```yaml
name: spect-curtain
seed: 1
scene:
  obstacles:                     # metres; also the Webots obstacles
    - {name: torso, type: ellipsoid, radii: [0.45, 0.17, 0.11]}
    - {name: arm_l, type: capsule, from: [-0.3, 0.235, 0], to: [0.3, 0.235, 0], radius: 0.045}
    - {name: couch, type: box, position: [0, 0, -0.125], size: [2.0, 0.5, 0.03], albedo: 0.3}
curtain:
  heads:                         # gantry angle about the patient axis (x), radius of the face
    - {name: a, angle: 0 deg, radius: 0.35}
    - {name: b, angle: 180 deg, radius: 0.35}
  count: 16                      # LEDs; `receivers:` if the row differs
  pitch: 24 mm
  span: 580 mm                   # emitter rail to receiver rail
  below_face: 12 mm
  receiver_offset: 0 mm          # staggered receiver row
  emitter: {intensity: 10 mW/sr, half_angle: 10 deg, aperture: 3 mm, spread: 0.1, tilt_sigma: 0.5 deg}
  receiver: {half_angle: 20 deg, area: 7 mm^2, aperture: 3 mm}
  chain: {gain: 1.0e7, tau: 20 us, ambient: 1 W/m^2, threshold_on: 0.6 V, threshold_off: 0.4 V}
  reflections: {enabled: false, albedo: 0.5, face_albedo: 0.0, rays: 256}
scan:
  algorithm: sequential          # or crossed, group, or "package.module:Class"
  params: {}                     # algorithm parameters
  slot: 200 us                   # default slot length
  settle: 150 us                 # sampling instant after the slot starts
  adc_threshold: null            # set: decide on the ADC voltage instead of the comparator
  rays: 1
scenario: {type: approach, speed: 50 mm/s, angles: [0 deg, 30 deg]}
```

Primitive types are `box` (`size`), `sphere` (`radius`), `ellipsoid` (`radii`), `cylinder` and `capsule`. Cylinders and capsules take `radius` and `height` along the local z axis, or `from`/`to` end points. Every primitive takes `position`, `rotation: [x, y, z, angle]`, an optional `albedo` and a `group`.

`include: other.yaml` merges another bench file underneath. A `scenario` or `sweep` in the including file replaces the included one whole.

## Scan algorithms

An algorithm is a generator. It yields a `Slot`, receives the `Sample` taken in it, and finally returns one "dark" decision per entry of `self.beams`:

```python
from embeddyn.mechatronics.lightcurtain import ScanAlgorithm, Slot

class EveryOther(ScanAlgorithm):
    name = "every-other"

    def setup(self):                       # self.params holds scan.params
        self.beams = [(k, k) for k in range(0, self.emitters, 2)]

    def scan(self):
        dark = []
        for e, r in self.beams:
            sample = yield Slot(lit=(e,), read=(r,))
            dark.append(not self.light(sample))  # comparator, or voltage vs adc_threshold
        return dark
```

Use it with `scan: {algorithm: "mypackage.scans:EveryOther"}`. The fields involved are:

* **`Slot`:** `lit` (LEDs on), `read` (receivers switched onto the amplifier node), `duration`, `settle`, `drive` (relative LED current) and a free `tag`.
* **`Sample`:** `voltage` and `detect` at the sampling instant, plus `power`, the ground-truth optical power. `power` is for diagnostics only; decisions should use `voltage` or `detect`.

The hardware model has one receiver node: a multiplexer, or the shared current bus of chained receiver ICs. Lighting several LEDs or enabling several receivers in one slot sums their light. The node settles with `tau` across slots, so the scan order matters.

| Built-in | Slots per frame (16 beams) | What it tests |
|---|---|---|
| `sequential` (`order: linear` or `interleaved`) | 16 | the baseline: LED k with receiver k |
| `crossed` (`reach: 1`) | 46 | diagonal beams e → e ± 1..reach between the straight ones, for finer resolution |
| `group` (`size: 4`) | 4 when clear | lights a group and reads its receivers together, then re-scans a group that dropped |

**Group scanning on a shared node is weak.** With 10° LEDs, each enabled receiver also sees the neighbouring LEDs of its group, so a group of 4 collects about 14 beams' worth of light. One dark straight beam then removes only about 1/16 of the signal. The defaults follow from that:

* LED drive is 1/g² so the node stays out of the rail.
* The threshold is 1 − 0.5/g² of the clear level, which the algorithm learns beam by beam in the first frame.

A noisier chain or ambient flicker breaks it first.

## Scenarios and metrics

| `scenario.type` | Parameters | Metrics, beyond those in every scenario |
|---|---|---|
| `static` | `frames`, `radius`, `gantry` | — |
| `approach` | `speed`, `angles`, `lead` (start this far outside contact) or `start` | `overshoot_max`/`overshoot_mean`: how far the head travelled past geometric contact before the scan reported a dark beam. Negative means early. Also `no_stop` |
| `rod` | `diameters`, `steps`, `across` (position along the beams), `length`, `keep_scene` | `detect_<d>mm`: rate of frames with a dark decision on a beam the rod shadows and none on an unshadowed beam. `resolution`: smallest diameter detected everywhere |
| `orbit` | `orbit_speed`, `duration`, `approach`, `retract`, `truth_every` | `gap_mean`/`gap_min`/`gap_max`: head radius minus the contact radius after the first close-in. `intrusion` |

Every scenario reports:

* `frame_time` and `slots_per_frame`;
* `missed_beams` (reported clear with *T* < 0.25) and `false_beams` (reported dark with *T* > 0.75);
* `clear_margin`: the weakest fully clear beam's voltage over `threshold_on`. It must be above 1, and 2–3 is comfortable;
* `dark_margin`: `threshold_off` over the brightest fully blocked beam.

## What the example sweeps show

These results come from the SPECT bench with its 16 beams, 24 mm pitch and 580 mm span. Only the optics and the algorithm change between rows.

**Intensity, angle and algorithm on the rod test** (`led-sweep.yaml`, *I* on axis):

| LED | Algorithm | Clear margin | 12 mm rod | 16 mm rod | 24 mm rod | Frame |
|---|---|---|---|---|---|---|
| 10°, 2 mW/sr | any | 0.44 | 0 (curtain reads dark everywhere) | 0 | 0 | — |
| 10°, 10 mW/sr | sequential | 1.88 | 45 % | 69 % | 94 % | 3.2 ms |
| 10°, 10 mW/sr | crossed | 1.74 | 88 % | 100 % | 100 % | 9.2 ms |
| 10°, 40 mW/sr | sequential | 5.33 (rail) | 45 % | 57 % | 94 % | 3.2 ms |
| 3°, 10 mW/sr | crossed | 0.83 | 90 % | 94 % | 92 % | 9.2 ms |

* **Too little light** fails safe but useless: every beam reads dark.
* **Too much light** fails quietly. A rod that blocks 60 % of a pencil still leaves the receiver above threshold, so partial blocks are missed. Overshoot in the approach test grows from 0.08 mm at 6.7 mW/sr to 0.9 mm at 58 mW/sr.
* **Crossed beams** resolve a 16 mm rod everywhere, and the sequential scan cannot. A 3° LED is too narrow for the diagonal at atan(24/580) = 2.4°: those beams sit at half intensity and drop out.

**Half angle against mounting error** (`alignment-sweep.yaml`, same on-axis intensity; clear margin, with false dark beams in brackets):

| Tilt σ | 3° | 6° | 10° | 20° |
|---|---|---|---|---|
| 0° | 1.76 | 1.76 | 1.76 | 1.76 |
| 1° | 1.30 | 1.75 | 1.75 | 1.76 |
| 2° | 0.26 (60) | 1.31 | 1.70 | 1.75 |
| 4° | 0.08 (380) | 0.26 (60) | 1.00 | 1.71 |

A narrow LED buys intensity, but it has to be aimed to a fraction of its half angle.

**Stray light.** In this geometry the diffuse bounce off the skin and the detector face stays below 1 % of the direct beam, even for 60° LEDs at equal on-axis intensity. The skin is seen at grazing angles from receivers 12 mm above it. Turn `reflections` on when a scene has surfaces facing the receivers: a hand near the rail, a white sheet, a reflective panel.

**Grazing edges.** An edge parallel to the rails, like an arm beside the curtain at 90° orbit, shades every pencil by the same fraction. In the Webots run at 4 s, all 16 beams of head A sat at *T* = 0.41. The node voltage lands in the comparator's hysteresis band, and the contour follower hovers there with every beam about half shaded. That is the curtain's real working point: the thresholds, not the beam centre line, set how close the face gets.

## The Webots world

`curtain-webots` writes a world with three parts:

* the obstacles, as static Solids (drag them in the 3-D view);
* a supervisor Robot `spect` with the gantry and its heads, each carrying an `IrLightCurtain` PROTO;
* the `light_curtain` controller, whose `controllerArgs` point at the bench file.

Webots looks for the world, controllers and PROTOs relative to one project folder. Write the world into `examples/mechatronics/worlds/` (or another project's `worlds/` folder that has the PROTO and controller next to it).

Every step, the controller:

* reads the obstacles back from the scene: each plain Solid's Box, Sphere, Cylinder and Capsule shapes, through Pose, Transform and scale;
* reads each curtain's PROTO fields;
* runs the bench's `HeadScanner` up to the simulation time;
* moves the heads with `ContourFollower` (in while clear, out while dark) while the gantry orbits.

The PROTO fields can be edited live in the scene tree. The controller picks up a change on the next step:

* **LED:** `emitterHalfAngle`, `emitterIntensity`, `emitterPower`, `emitterDrive`, `emitterTilt`, `emitterYaw`, `tiltSigma`, `intensitySpread`, `emitterAperture`;
* **Receiver:** `receiverHalfAngle`, `receiverArea`, `receiverAperture`;
* **Scan:** `algorithm` and `algorithmParams` (JSON);
* **Geometry:** `count`, `pitch`, `span` and `receiverOffset`. These regenerate the PROTO.

The receiver chain, reflections, slot and settle times come from the bench file.

What the 3-D view shows:

* **Beams:** clear green (dimmer when partly shaded), dark red, missed orange, false dark magenta.
* **Cone:** the emission cone of the scanned LED, drawn at its half angle.
* **LED devices:** emitters glow while lit; receivers glow while their comparator sees light.

Keys (click the 3-D view first):

| Key | Action |
|---|---|
| S | slot mode: one scan slot per Webots step. The lit LED glows, the cone follows it, and the overlay shows the node voltage |
| O / M | orbit on/off, head motion on/off |
| A | next built-in algorithm |
| ← / → | LED half angle −/+ 1° |
| ↑ / ↓ | LED intensity × / ÷ 1.25 |
| R | heads back out |

Controller arguments are:

* `--log frames.csv`, `--orbit-speed`, `--approach`, `--retract`, `--no-orbit`, `--slot-mode`;
* `--quit-after <s>`;
* `--screenshot view.png --screenshot-at <s>`, which saves the 3-D view.

Pass them with `curtain-webots --arg=...`, or edit `controllerArgs`.

Runtime requirements:

* The controller imports embeddyn from the repository; set `EMBEDDYN_ROOT` if the world is used from elsewhere.
* Webots runs it with the system `python3`, which needs numpy and PyYAML.
* The world uses no Webots asset PROTOs, so it opens offline.

Tested with Webots R2025a, unpacked from the official Linux tarball without root. Headless:

```bash
webots --batch --minimize --no-rendering --mode=fast --stdout --stderr examples/mechatronics/worlds/spect_light_curtain.wbt
```

`tests/mechatronics/test_light_curtain_bench.py` runs the world for 1.5 s when `WEBOTS_HOME` points at the installation or `webots` is on the `PATH`.

## Limits

* **Reflections are diffuse and single-bounce.** There are no specular surfaces (polished metal, wet skin) and no multipath.
* **LED spectrum and photodiode responsivity are fixed numbers.** There is no temperature drift and no ageing other than `spread`.
* **Geometry is frozen during a frame.** Head motion is applied between frames: at 50 mm/s and 3.2 ms frames, that is 0.16 mm.
* **Obstacles are primitives.** Webots meshes are not read; approximate them with primitives.
