"""Webots supervisor: IR light-curtain scanning on a SPECT gantry (embeddyn bench code).

The controller runs on the "spect" Robot of a world written by
`embeddyn mechatronics curtain-webots`. Each IrLightCurtain PROTO in the
world is one curtain head. Per Webots step it

* reads the obstacles back from the scene (every plain Solid's Box, Sphere,
  Cylinder and Capsule shapes, with Pose/Transform and scale), so dragging the
  patient or a test rod in the 3-D view changes what the beams see;
* reads each curtain's optical fields (LED half angle, intensity/power, tilt,
  receiver, scan algorithm), so edits in the scene tree apply at once;
* runs the scan algorithm with embeddyn's `HeadScanner` for the elapsed time
  and moves the heads with `ContourFollower` (in while clear, out while dark)
  while the gantry orbits;
* draws the algorithm's beams (green clear, red dark, orange missed, magenta
  false dark; a half-shaded beam is dimmer green), the emission cone of the LED being scanned, and an overlay.

Keys (click the 3-D view first):
  S  slot mode on/off: one scan slot per step, the lit LED glows, the cone follows it
  O  orbit on/off             M  contour motion on/off
  A  next scan algorithm      Left/Right  LED half angle -/+ 1 deg
  Up/Down  LED intensity x/÷ 1.25        R  reset the heads outwards

Arguments (controllerArgs): --config <bench.yaml> [--log frames.csv]
  [--orbit-speed 0.3] [--approach 0.03] [--retract 0.03] [--no-orbit] [--slot-mode]
  [--quit-after s] [--screenshot view.png --screenshot-at s]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("EMBEDDYN_ROOT", HERE.parents[3]))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from controller import Keyboard, Supervisor  # noqa: E402

from embeddyn.mechatronics.lightcurtain import (  # noqa: E402
    ALGORITHMS, ContourFollower, CurtainHead, CurtainLayout, EmitterSpec, HeadScanner, ReceiverSpec, Scene,
    load_bench, make_algorithm)
from embeddyn.mechatronics.lightcurtain.array import chain_optics  # noqa: E402
from embeddyn.mechatronics.lightcurtain.bench import FALSE_ABOVE, MISSED_BELOW  # noqa: E402
from embeddyn.mechatronics.lightcurtain.scene import (  # noqa: E402
    Box, Capsule, Cylinder, Ellipsoid, frame_between, rotation_matrix)
from embeddyn.mechatronics.lightcurtain.webots_world import axis_angle  # noqa: E402

GREEN, RED, ORANGE, MAGENTA = (0.1, 0.9, 0.25), (1.0, 0.1, 0.05), (1.0, 0.55, 0.0), (1.0, 0.1, 0.9)


# -- scene tree helpers ------------------------------------------------------------------------
def children_of(node):
    field = node.getField("children")
    if field is None:
        return []
    return [field.getMFNode(i) for i in range(field.getCount())]


def find_def(root, name):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.getDef() == name:
            return node
        stack.extend(children_of(node))
    return None


def pose_of(node):
    """World position and 3x3 rotation of a Pose-derived node."""
    return np.array(node.getPosition()), np.array(node.getOrientation()).reshape(3, 3)


def local_transform(node):
    t = node.getField("translation")
    r = node.getField("rotation")
    s = node.getField("scale")
    trans = np.array(t.getSFVec3f()) if t else np.zeros(3)
    rot = rotation_matrix(r.getSFRotation()[:3], r.getSFRotation()[3]) if r else np.eye(3)
    scale = np.array(s.getSFVec3f()) if s else np.ones(3)
    return trans, rot, scale


def shapes_to_primitives(node, origin, rot, scale, name, out):
    kind = node.getTypeName()
    if kind == "Shape":
        geometry = node.getField("geometry").getSFNode()
        if geometry is None:
            return
        g = geometry.getTypeName()
        if g == "Sphere":
            radius = geometry.getField("radius").getSFFloat()
            if np.allclose(scale, scale[0]):
                out.append(Capsule(name, origin, rot, radius=radius * scale[0], half_height=0.0))
            else:
                out.append(Ellipsoid(name, origin, rot, radii=radius * scale))
        elif g == "Box":
            out.append(Box(name, origin, rot, half=np.array(geometry.getField("size").getSFVec3f()) * scale / 2))
        elif g in ("Cylinder", "Capsule"):
            radius = geometry.getField("radius").getSFFloat() * max(scale[0], scale[1])
            half = geometry.getField("height").getSFFloat() * scale[2] / 2
            cls = Cylinder if g == "Cylinder" else Capsule
            out.append(cls(name, origin, rot, radius=radius, half_height=half))
        return
    if kind in ("Pose", "Transform", "Group"):
        if kind == "Group":
            t, r, s = np.zeros(3), np.eye(3), np.ones(3)
        else:
            t, r, s = local_transform(node)
        for child in children_of(node):
            shapes_to_primitives(child, origin + rot @ (scale * t), rot @ r, scale * s, name, out)


def read_obstacles(root, skip) -> Scene:
    prims = []
    for node in children_of(root):
        if node.getTypeName() != "Solid" or node.getId() == skip:
            continue
        origin, rot = pose_of(node)
        name_field = node.getField("name")
        name = name_field.getSFString() if name_field else "solid"
        for child in children_of(node):
            shapes_to_primitives(child, origin, rot, np.ones(3), name, prims)
    return Scene(prims)


# -- one curtain head --------------------------------------------------------------------------
class CurtainView:
    """Reads an IrLightCurtain node, scans it, draws its beams."""

    def __init__(self, robot, proto, slide, mount_name, config, follower):
        self.robot, self.proto, self.slide, self.name = robot, proto, slide, mount_name
        self.config = config
        self.follower = follower
        self.signature = None
        self.scanner = None
        self.result = None
        self.sample = None
        self.slot_steps = None
        self.radius = slide.getField("translation").getSFVec3f()[2]
        self.radius_set = self.radius
        self.beam_nodes = []
        self.cone = None
        self.leds_em, self.leds_rx = [], []
        self.lit_shown = set()

    def field(self, name):
        return self.proto.getField(name)

    def specs(self):
        f = self.field
        layout = CurtainLayout(count=f("count").getSFInt32(), pitch=f("pitch").getSFFloat(),
                               span=f("span").getSFFloat(), receiver_offset=f("receiverOffset").getSFFloat())
        power = f("emitterPower").getSFFloat()
        emitter = EmitterSpec(intensity=None if power > 0 else f("emitterIntensity").getSFFloat(),
                              power=power if power > 0 else None,
                              half_angle=f("emitterHalfAngle").getSFFloat(), aperture=f("emitterAperture").getSFFloat(),
                              drive=f("emitterDrive").getSFFloat(), spread=f("intensitySpread").getSFFloat(),
                              tilt=f("emitterTilt").getSFFloat(), yaw=f("emitterYaw").getSFFloat(),
                              tilt_sigma=f("tiltSigma").getSFFloat())
        receiver = ReceiverSpec(half_angle=f("receiverHalfAngle").getSFFloat(), area=f("receiverArea").getSFFloat(),
                                aperture=f("receiverAperture").getSFFloat())
        algorithm = f("algorithm").getSFString()
        try:
            params = json.loads(f("algorithmParams").getSFString() or "{}")
        except json.JSONDecodeError:
            params = {}
        return layout, emitter, receiver, algorithm, params

    def refresh(self):
        """Rebuild the scanner when the curtain's fields changed."""
        layout, emitter, receiver, algorithm, params = self.specs()
        signature = repr((layout, emitter, receiver, algorithm, params))
        if signature == self.signature:
            return False
        self.signature = signature
        self.layout, self.emitter, self.receiver = layout, emitter, receiver
        cfg = self.config
        chain = chain_optics(cfg.raw.get("curtain", {}).get("chain"), emitter, receiver)
        algo = make_algorithm({"name": algorithm, **params}, layout.count, layout.receiver_count,
                              slot=cfg.slot, settle=cfg.settle, threshold=cfg.adc_threshold)
        old_time = self.scanner.time if self.scanner else self.robot.getTime()
        self.scanner = HeadScanner(algo, chain, cfg.rays, cfg.reflections)
        self.scanner.time = old_time
        self.template = CurtainHead.build(self.name, layout, emitter, receiver, seed=cfg.seed)
        self.slot_steps = None
        self.leds_em = [self.robot.getDevice(f"{self.prefix}em{k}") for k in range(layout.count)]
        self.leds_rx = [self.robot.getDevice(f"{self.prefix}rx{k}") for k in range(layout.receiver_count)]
        self.rebuild_beams()
        return True

    @property
    def prefix(self):
        return self.field("prefix").getSFString()

    def head(self):
        """The curtain in the world at the current (possibly not yet drawn) head radius."""
        origin, rot = pose_of(self.proto)
        normal = rot[:, 2]
        origin = origin + normal * (self.radius - self.radius_set)     # radius moved since the last step
        return self.template.moved(origin, rot)

    # -- scanning ------------------------------------------------------------------------------
    def run_frames(self, scene, until, move):
        while self.scanner.time < until:
            result = self.scanner.frame(self.head(), scene)
            self.result = result
            if move:
                self.radius = self.follower.advance(self.radius, result.any_dark, result.end - result.start)
            yield result

    def run_slot(self, scene):
        if self.slot_steps is None:
            self.slot_steps = self.scanner.iter_frame(self.head(), scene)
        try:
            self.sample = next(self.slot_steps)
        except StopIteration as done:
            self.result = done.value
            self.slot_steps = None
            self.sample = None

    # -- drawing -------------------------------------------------------------------------------
    def rebuild_beams(self):
        for node in self.beam_nodes:
            node.remove()
        self.beam_nodes = []
        if self.cone is not None:
            self.cone.remove()
            self.cone = None
        group = self.robot.getRoot().getField("children")
        show_beams = self.field("showBeams").getSFBool()
        if show_beams:
            for _ in self.scanner.algorithm.beams:
                group.importMFNodeFromString(-1, (
                    "Pose { children [ Shape { castShadows FALSE appearance PBRAppearance { baseColor 0 0 0 "
                    "emissiveColor 0.1 0.9 0.25 transparency 0.55 roughness 1 metalness 0 } "
                    "geometry Cylinder { radius 0.0012 height 1 subdivision 6 } } ] }"))
                self.beam_nodes.append(group.getMFNode(-1))
        if self.field("showCone").getSFBool():
            group.importMFNodeFromString(-1, (
                "Pose { children [ Shape { castShadows FALSE appearance PBRAppearance { baseColor 1 0.1 0.05 "
                "emissiveColor 0.6 0.05 0.02 transparency 0.82 roughness 1 metalness 0 } "
                "geometry Cone { bottomRadius 0.1 height 0.5 subdivision 32 } } ] }"))
            self.cone = group.getMFNode(-1)
        self._beam_fields = []
        for node in self.beam_nodes:
            shape = node.getField("children").getMFNode(0)
            appearance = shape.getField("appearance").getSFNode()
            geometry = shape.getField("geometry").getSFNode()
            self._beam_fields.append((node.getField("translation"), node.getField("rotation"),
                                      geometry.getField("height"), appearance.getField("emissiveColor"),
                                      appearance.getField("transparency")))

    def draw(self, slot_mode):
        head = self.head()
        algo = self.scanner.algorithm
        result = self.result
        for k, (e, r) in enumerate(algo.beams):
            if k >= len(self._beam_fields):
                break
            translation, rotation, height, emissive, transparency = self._beam_fields[k]
            centre, rot, length = frame_between(head.emitters[e], head.receivers[r])
            translation.setSFVec3f(centre.tolist())
            rotation.setSFRotation(list(axis_angle(rot)))
            height.setSFFloat(length)
            color, alpha = GREEN, 0.7
            if result is not None and k < len(result.dark):
                dark, t = result.dark[k], result.transmittance[k]
                color, alpha = ((ORANGE, 0.1) if not dark and t < MISSED_BELOW else
                                (MAGENTA, 0.1) if dark and t > FALSE_ABOVE else
                                (RED, 0.25) if dark else (GREEN, 0.7 if t >= 0.999 else 0.45))
            if slot_mode and self.sample is not None and e in self.sample.slot.lit and r in self.sample.slot.read:
                alpha = 0.0
            emissive.setSFColor(list(color))
            transparency.setSFFloat(alpha)
        # LEDs: in slot mode the lit emitters; otherwise emitters of dark beams. Receivers: comparator.
        lit = set(self.sample.slot.lit) if slot_mode and self.sample is not None else (
            {e for (e, _), d in zip(algo.beams, result.dark) if d} if result else set())
        if lit != self.lit_shown:
            for k, led in enumerate(self.leds_em):
                if led is not None:
                    led.set(1 if k in lit else 0)
            self.lit_shown = lit
        if slot_mode and self.sample is not None:
            for k, led in enumerate(self.leds_rx):
                if led is not None:
                    led.set(1 if (k in self.sample.slot.read and self.sample.detect) else 0)
        elif result is not None:
            light = {r for (_, r), d in zip(algo.beams, result.dark) if not d}
            for k, led in enumerate(self.leds_rx):
                if led is not None:
                    led.set(1 if k in light else 0)
        if self.cone is not None:
            if slot_mode and self.sample is not None:
                emitter = self.sample.slot.lit[0]
            elif result is not None and any(result.dark):
                emitter = algo.beams[result.dark.index(True)][0]
            else:
                emitter = len(head.emitters) // 2
            axis = head.emitter_axes[emitter]
            span = self.layout.span
            half = math.radians(self.emitter.half_angle)
            cone_height = span
            shape = self.cone.getField("children").getMFNode(0)
            geometry = shape.getField("geometry").getSFNode()
            geometry.getField("height").setSFFloat(cone_height)
            geometry.getField("bottomRadius").setSFFloat(cone_height * math.tan(half))
            z = -axis                                          # Webots cone apex is +z: open along the axis
            helper = np.array([1.0, 0, 0]) if abs(z[0]) < 0.9 else np.array([0, 1.0, 0])
            x = np.cross(helper, z)
            x /= np.linalg.norm(x)
            rot = np.column_stack([x, np.cross(z, x), z])
            self.cone.getField("translation").setSFVec3f((head.emitters[emitter] + axis * cone_height / 2).tolist())
            self.cone.getField("rotation").setSFRotation(list(axis_angle(rot)))

    def commit_radius(self):
        field = self.slide.getField("translation")
        value = field.getSFVec3f()
        field.setSFVec3f([value[0], value[1], self.radius])
        self.radius_set = self.radius


# -- main loop -----------------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--log")
    parser.add_argument("--orbit-speed", type=float)
    parser.add_argument("--approach", type=float, default=0.03)
    parser.add_argument("--retract", type=float, default=0.03)
    parser.add_argument("--no-orbit", action="store_true")
    parser.add_argument("--slot-mode", action="store_true")
    parser.add_argument("--quit-after", type=float, help="stop the simulation after this many seconds (tests)")
    parser.add_argument("--screenshot", help="save the 3-D view as an image at --screenshot-at seconds")
    parser.add_argument("--screenshot-at", type=float, default=1.0)
    args = parser.parse_args()

    from embeddyn.mechatronics.lightcurtain.bench import BenchConfig
    config = BenchConfig.from_mapping(load_bench(Path(args.config) if Path(args.config).is_absolute() else HERE / args.config))
    scenario = config.scenario
    from embeddyn.mechatronics.quantities import Quantity
    omega = args.orbit_speed if args.orbit_speed is not None else \
        Quantity.parse(scenario.get("orbit_speed", 0.3), "rad/s").to("rad/s")

    robot = Supervisor()
    step = int(robot.getBasicTimeStep())
    keyboard = robot.getKeyboard()
    keyboard.enable(step)
    root = robot.getRoot()
    me = robot.getSelf()
    gantry = find_def(me, "GANTRY")
    views = []
    for mount in config.mounts:
        tag = mount.name.upper()
        holder, slide = find_def(me, f"CURTAIN_{tag}"), find_def(me, f"SLIDE_{tag}")
        if holder is None or slide is None:
            print(f"light_curtain: no CURTAIN_{tag}/SLIDE_{tag} in the world, head skipped", file=sys.stderr)
            continue
        proto = children_of(holder)[0]
        follower = ContourFollower(args.approach, args.retract, mount.radius_min, mount.radius_max)
        views.append(CurtainView(robot, proto, slide, mount.name, config, follower))
    log = None
    if args.log:
        log_file = open(args.log, "w", newline="")
        log = csv.writer(log_file)
        log.writerow(["time", "head", "algorithm", "half_angle", "on_axis", "radius", "frame_time", "dark", "truth_dark",
                      "missed", "false", "clear_min"])
    slot_mode, orbit, move = args.slot_mode, not args.no_orbit, True
    angle = 0.0
    names = list(ALGORITHMS)
    while robot.step(step) != -1:
        now = robot.getTime()
        key = keyboard.getKey()
        while key != -1:
            ch = chr(key & 0xFF).upper() if (key & 0xFF) < 128 else ""
            for view in views:
                f = view.field
                if key == Keyboard.LEFT:
                    f("emitterHalfAngle").setSFFloat(max(1.0, f("emitterHalfAngle").getSFFloat() - 1))
                elif key == Keyboard.RIGHT:
                    f("emitterHalfAngle").setSFFloat(min(80.0, f("emitterHalfAngle").getSFFloat() + 1))
                elif key == Keyboard.UP:
                    f("emitterIntensity").setSFFloat(f("emitterIntensity").getSFFloat() * 1.25)
                elif key == Keyboard.DOWN:
                    f("emitterIntensity").setSFFloat(f("emitterIntensity").getSFFloat() / 1.25)
                elif ch == "A":
                    current = f("algorithm").getSFString()
                    nxt = names[(names.index(current) + 1) % len(names)] if current in names else names[0]
                    f("algorithm").setSFString(nxt)
                    f("algorithmParams").setSFString("{}")
                elif ch == "R":
                    view.radius = view.follower.radius_max
            if ch == "S":
                slot_mode = not slot_mode
            elif ch == "O":
                orbit = not orbit
            elif ch == "M":
                move = not move
            key = keyboard.getKey()

        scene = read_obstacles(root, me.getId())
        for view in views:
            view.refresh()
        if slot_mode:
            for view in views:
                view.run_slot(scene)
                view.scanner.time = max(view.scanner.time, now)     # the scan clock waits for the view
        else:
            if orbit:
                angle += omega * step / 1000.0
                gantry.getField("rotation").setSFRotation([1, 0, 0, angle])
            for view in views:
                for result in view.run_frames(scene, now, move):
                    if log:
                        log.writerow([f"{result.end:.6f}", view.name, view.scanner.algorithm.name,
                                      view.emitter.half_angle, view.emitter.on_axis, f"{view.radius:.5f}",
                                      f"{result.end - result.start:.6g}", sum(result.dark), sum(result.truth),
                                      result.missed, result.false_dark,
                                      "" if math.isinf(result.clear_min) else f"{result.clear_min:.4f}"])
        lines = [f"{'SLOT' if slot_mode else 'SCAN'} t={now:5.2f}s orbit {'on' if orbit else 'off'}"
                 f"   keys: S slot  O orbit  M motion  A algorithm  <> angle  ^v intensity  R reset"]
        for view in views:
            view.draw(slot_mode)
            view.commit_radius()
            r = view.result
            algo = view.scanner.algorithm
            text = (f"{view.name}: {algo.name} | LED {view.emitter.half_angle:.1f} deg "
                    f"{view.emitter.on_axis * 1e3:.2f} mW/sr | r {view.radius * 1000:.1f} mm")
            if r is not None:
                text += (f"\n   frame {1e3 * (r.end - r.start):.2f} ms  dark {sum(r.dark)}/{len(r.dark)}"
                         f"  missed {r.missed}  false {r.false_dark}")
                if not math.isinf(r.clear_min):
                    text += f"  margin {r.clear_min / view.scanner.chain.optics.threshold_on:.2f}"
            if slot_mode and view.sample is not None:
                s = view.sample
                text += (f"\n   slot: lit {list(s.slot.lit)} read {list(s.slot.read)}  node {s.voltage:.3f} V"
                         f"  {'LIGHT' if s.detect else 'dark'}")
            lines.append(text)
        robot.setLabel(0, "\n".join(lines), 0.01, 0.01, 0.055, 0xFFFFFF, 0.0, "Lucida Console")
        if args.screenshot and now >= args.screenshot_at and not getattr(main, "_shot", False):
            main._shot = True
            robot.exportImage(args.screenshot, 90)
        if args.quit_after is not None and now >= args.quit_after:
            if log:
                log_file.flush()
            robot.simulationQuit(0)
    if log:
        log_file.close()


if __name__ == "__main__":
    main()
