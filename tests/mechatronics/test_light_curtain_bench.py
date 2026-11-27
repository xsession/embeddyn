"""Light-curtain bench: ray casting, LED optics, scan algorithms, scenarios, Webots world."""
from __future__ import annotations

import importlib.util
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import yaml

from embeddyn.mechatronics.lightcurtain import (
    Box, Capsule, Cylinder, Ellipsoid, EmitterSpec, HeadScanner, ReceiverSpec, Scene, ScanAlgorithm, Slot,
    Sphere, coupling, geometric_gain, load_bench, make_algorithm, run_bench, sweep)
from embeddyn.mechatronics.lightcurtain.array import CurtainHead, CurtainLayout, chain_optics
from embeddyn.mechatronics.lightcurtain.bench import BenchConfig
from embeddyn.mechatronics.lightcurtain.scene import rotation_matrix
from embeddyn.mechatronics.lightcurtain.webots_world import axis_angle, world_text
from embeddyn.mechatronics.optics import Beam, LightCurtain

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "examples/mechatronics/light-curtain/spect-curtain.yaml"


def seg(*points):
    return np.array([points[0]], dtype=float), np.array([points[1]], dtype=float)


@pytest.mark.parametrize("prim, through, past", [
    (Box("b", half=np.array([0.1, 0.1, 0.1])), ([-1, 0, 0], [1, 0, 0]), ([-1, 0.11, 0], [1, 0.11, 0])),
    (Ellipsoid("e", radii=np.array([0.3, 0.1, 0.05])), ([0.25, -1, 0], [0.25, 1, 0]), ([0.31, -1, 0], [0.31, 1, 0])),
    (Cylinder("c", radius=0.05, half_height=0.2), ([-1, 0.04, 0.19], [1, 0.04, 0.19]), ([-1, 0, 0.21], [1, 0, 0.21])),
    (Capsule("k", radius=0.05, half_height=0.2), ([-1, 0, 0.24], [1, 0, 0.24]), ([-1, 0, 0.26], [1, 0, 0.26])),
    (Sphere("s", radius=0.1), ([-1, 0.099, 0], [1, 0.099, 0]), ([-1, 0.101, 0], [1, 0.101, 0])),
])
def test_primitives_block_exactly_at_their_surface(prim, through, past):
    scene = Scene([prim])
    assert scene.blocked(*seg(*through))[0]
    assert not scene.blocked(*seg(*past))[0]
    # A segment that stops short of the solid is not blocked.
    start, end = np.array(through[0], float), np.array(through[1], float)
    assert not scene.blocked(*seg(start, start + (end - start) * 0.2))[0]


def test_rotated_primitive_and_segment_from_inside():
    box = Box("b", rotation=rotation_matrix([0, 0, 1], math.pi / 4), half=np.array([0.2, 0.01, 0.2]))
    scene = Scene([box])
    assert scene.blocked(*seg([-0.1, -0.1, 0], [-0.1, -0.1, 0.01]))[0]          # starts inside
    assert not scene.blocked(*seg([0.1, -0.1, -1], [0.1, -0.1, 1]))[0]          # beside the thin rotated wall


def test_straight_beam_power_matches_the_single_beam_optics():
    emitter, receiver = EmitterSpec(intensity=0.01, half_angle=10), ReceiverSpec(half_angle=20)
    head = CurtainHead.build("a", CurtainLayout(count=4, pitch=0.024, span=0.58), emitter, receiver)
    gain = geometric_gain(head)
    curtain = LightCurtain(chain_optics({}, emitter, receiver))
    reading = curtain.read_beam(Beam(head.emitters[1].tolist(), head.receivers[1].tolist()), lambda *a: False)
    assert gain[1, 1] == pytest.approx(reading.power, rel=1e-9)
    # A neighbour receiver sees less (off both axes), more so with a narrower LED.
    narrow = CurtainHead.build("a", CurtainLayout(count=4, pitch=0.024, span=0.58),
                               EmitterSpec(intensity=0.01, half_angle=3), receiver)
    assert geometric_gain(narrow)[1, 2] / geometric_gain(narrow)[1, 1] < gain[1, 2] / gain[1, 1] < 1


def test_total_power_gives_more_on_axis_intensity_to_a_narrow_led():
    wide, narrow = EmitterSpec.from_mapping({"power": "5 mW", "half_angle": "30 deg"}), \
        EmitterSpec.from_mapping({"power": "5 mW", "half_angle": "5 deg"})
    assert narrow.on_axis > 20 * wide.on_axis
    m = narrow.order
    assert narrow.on_axis == pytest.approx(5e-3 * (m + 1) / (2 * math.pi))
    with pytest.raises(ValueError):
        EmitterSpec.from_mapping({"colour": 1})


def test_half_angle_is_half_intensity():
    head = CurtainHead.build("a", CurtainLayout(count=1, span=1.0), EmitterSpec(intensity=1.0, half_angle=15,
                             yaw=15), ReceiverSpec(half_angle=89))
    straight = CurtainHead.build("a", CurtainLayout(count=1, span=1.0), EmitterSpec(intensity=1.0, half_angle=15),
                                 ReceiverSpec(half_angle=89))
    assert geometric_gain(head)[0, 0] / geometric_gain(straight)[0, 0] == pytest.approx(0.5, rel=1e-6)


def test_coupling_partial_blocking_and_truth():
    head = CurtainHead.build("a", CurtainLayout(count=3, pitch=0.03, span=0.5), EmitterSpec(), ReceiverSpec())
    edge = Box("edge", position=np.array([0.0, 0.0, -0.05]), half=np.array([0.2, 0.01, 0.05]))   # top face at z = 0
    result = coupling(head, Scene([edge]), rays=2, pairs=[(0, 0), (1, 1), (2, 2)])
    t = result.transmittance.diagonal()
    assert 0.3 < t[1] < 0.7                       # the edge cuts the pencil through its middle
    clear = coupling(head, Scene([]), pairs=[(1, 1)])
    assert clear.transmittance[1, 1] == 1.0 and clear.power[1, 1] == pytest.approx(clear.clear[1, 1])


def _bench(**changes):
    raw = load_bench(BENCH)
    for key, value in changes.items():
        node = raw
        *path, last = key.split("__")
        for part in path:
            node = node.setdefault(part, {})
        node[last] = value
    return raw


def test_sequential_scan_reports_the_blocked_beam():
    config = BenchConfig.from_mapping(_bench(scan__algorithm="sequential"))
    head = config.head(config.mounts[0], 0.0, 0.35)
    k = 5
    rod = Cylinder("rod", head.origin + head.rotation @ np.array([(k - 7.5) * 0.024, 0, 0]), head.rotation,
                   radius=0.006, half_height=0.05)
    scanner = HeadScanner(config.make_algorithm(), config.chain, rays=1)
    scanner.frame(head, Scene([]))                     # settle
    result = scanner.frame(head, Scene([rod]))
    assert result.dark == result.truth
    assert [i for i, d in enumerate(result.dark) if d] == [k]
    assert result.end - result.start == pytest.approx(16 * 200e-6)
    assert result.clear_min > config.chain.threshold_on


def test_group_scan_is_fast_when_clear_and_still_finds_one_dark_beam():
    config = BenchConfig.from_mapping(_bench(scan__algorithm="group", scan__params={"size": 4}))
    head = config.head(config.mounts[0], 0.0, 0.35)
    scanner = HeadScanner(config.make_algorithm(), config.chain, rays=1)
    scanner.frame(head, Scene([]))                     # learns the group levels
    clear = scanner.frame(head, Scene([]))
    assert clear.slots == 4 and not clear.any_dark
    rod = Cylinder("rod", head.origin + head.rotation @ np.array([(9 - 7.5) * 0.024, 0, 0]), head.rotation,
                   radius=0.006, half_height=0.05)
    dark = scanner.frame(head, Scene([rod]))
    assert [i for i, d in enumerate(dark.dark) if d] == [9]
    assert dark.slots == 4 + 4


class EveryOther(ScanAlgorithm):
    """A custom algorithm loaded by module path: scans even beams only."""
    name = "every-other"

    def setup(self):
        self.beams = [(k, k) for k in range(0, self.emitters, 2)]

    def scan(self):
        dark = []
        for e, r in self.beams:
            sample = yield Slot((e,), (r,))
            dark.append(not self.light(sample))
        return dark


def test_custom_algorithm_by_module_path():
    algo = make_algorithm(f"{__name__}:EveryOther", 16, 16)
    assert len(algo.beams) == 8
    with pytest.raises(ValueError):
        make_algorithm("nope", 16, 16)
    assert len(make_algorithm({"name": "crossed", "reach": 2}, 16, 16).beams) == 16 * 5 - 6


def test_rod_resolution_crossed_beats_sequential():
    rod = {"type": "rod", "diameters": ["10 mm", "16 mm"], "steps": 31}
    seq = run_bench(_bench(scenario=rod, scan__algorithm="sequential"))["metrics"]
    crossed = run_bench(_bench(scenario=rod, scan__algorithm="crossed"))["metrics"]
    assert crossed["detect_16mm"] == 1.0
    assert seq["detect_16mm"] < 1.0
    assert crossed["detect_10mm"] > seq["detect_10mm"]


def test_approach_stops_within_a_frame_of_contact():
    out = run_bench(_bench(scenario={"type": "approach", "speed": "50 mm/s", "angles": ["0 deg"], "lead": "5 mm"}))
    for item in out["approaches"]:
        assert item["stop_radius"] is not None
        assert -0.0005 < item["overshoot"] < 0.001


def test_too_little_light_reads_dark_everywhere():
    out = run_bench(_bench(scenario={"type": "static", "frames": 3}, curtain__emitter={"intensity": "1 mW/sr"}))
    assert out["metrics"]["false_beams"] > 0
    assert out["metrics"]["clear_margin"] < 1


def test_sweep_grid_runs_variants():
    raw = _bench(scenario={"type": "static", "frames": 2})
    rows = sweep(raw, {"emitter.half_angle": ["5 deg", "20 deg"], "scan.algorithm": ["sequential", "crossed"]})
    assert len(rows) == 4 and all("error" not in r for r in rows)
    assert {r["scan.algorithm"] for r in rows} == {"sequential", "crossed"}


def test_led_sweep_file_includes_the_bench():
    raw = load_bench(BENCH.parent / "led-sweep.yaml")
    assert raw["scenario"]["type"] == "rod" and raw["curtain"]["count"] == 16 and "include" not in raw


def test_axis_angle_round_trip():
    for axis, angle in [([1, 0, 0], 0.3), ([0, 1, 1], 2.0), ([1, 2, 3], math.pi), ([0, 0, 1], 0.0)]:
        r = rotation_matrix(axis, angle)
        x, y, z, a = axis_angle(r)
        assert np.allclose(rotation_matrix([x, y, z], a), r, atol=1e-9)


def test_world_has_obstacles_heads_and_curtains():
    config = BenchConfig.from_mapping(load_bench(BENCH))
    text = world_text(config, "../../light-curtain/spect-curtain.yaml")
    for token in ("DEF TORSO Solid", "DEF COUCH Solid", "DEF SPECT Robot", "supervisor TRUE",
                  'controller "light_curtain"', "DEF CURTAIN_A Pose", "DEF SLIDE_B Pose", 'prefix "a_"',
                  "emitterHalfAngle 10", 'EXTERNPROTO "../protos/IrLightCurtain.proto"'):
        assert token in text
    assert text.count("IrLightCurtain {") == 2
    committed = (ROOT / "examples/mechatronics/worlds/spect_light_curtain.wbt").read_text()
    assert committed == text, "regenerate with: embeddyn mechatronics curtain-webots ... -o worlds/spect_light_curtain.wbt"


# -- the Webots controller's scene reader, against fake nodes ------------------------------------
class FakeField:
    def __init__(self, value):
        self.value = value

    def getSFVec3f(self): return list(self.value)
    def getSFRotation(self): return list(self.value)
    def getSFFloat(self): return float(self.value)
    def getSFString(self): return self.value
    def getSFNode(self): return self.value
    def getCount(self): return len(self.value)
    def getMFNode(self, i): return self.value[i]


class FakeNode:
    def __init__(self, type_name, fields=None, position=(0, 0, 0), orientation=None, node_id=1, defname=""):
        self.type_name, self.fields = type_name, fields or {}
        self.position = position
        self.orientation = orientation or [1, 0, 0, 0, 1, 0, 0, 0, 1]
        self.id, self.defname = node_id, defname

    def getTypeName(self): return self.type_name
    def getField(self, name): return FakeField(self.fields[name]) if name in self.fields else None
    def getPosition(self): return list(self.position)
    def getOrientation(self): return list(self.orientation)
    def getId(self): return self.id
    def getDef(self): return self.defname


def _controller_module():
    sys.modules.setdefault("controller", types.SimpleNamespace(Keyboard=object, Supervisor=object))
    path = ROOT / "examples/mechatronics/controllers/light_curtain/light_curtain.py"
    spec = importlib.util.spec_from_file_location("light_curtain_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_reads_obstacles_from_the_scene_tree():
    ctl = _controller_module()
    sphere = FakeNode("Shape", {"geometry": FakeNode("Sphere", {"radius": 1.0})})
    torso = FakeNode("Solid", {"name": "torso", "children": [
        FakeNode("Transform", {"translation": [0, 0, 0], "rotation": [0, 0, 1, 0], "scale": [0.45, 0.17, 0.11],
                               "children": [sphere]})]}, position=(0.1, 0, 0), node_id=2)
    rod = FakeNode("Solid", {"name": "rod", "children": [
        FakeNode("Pose", {"translation": [0, 0, 0.5], "rotation": [0, 0, 1, 0], "children": [
            FakeNode("Shape", {"geometry": FakeNode("Cylinder", {"radius": 0.01, "height": 0.2})})]})]}, node_id=3)
    robot = FakeNode("Robot", {"children": []}, node_id=9)
    root = FakeNode("Group", {"children": [torso, rod, robot, FakeNode("Floor", {})]})
    scene = ctl.read_obstacles(root, skip=9)
    kinds = sorted(type(p).__name__ for p in scene.primitives)
    assert kinds == ["Cylinder", "Ellipsoid"]
    ell = scene.find("torso")
    assert np.allclose(ell.radii, [0.45, 0.17, 0.11]) and np.allclose(ell.position, [0.1, 0, 0])
    assert np.allclose(scene.find("rod").position, [0, 0, 0.5])
    assert ctl.find_def(FakeNode("Group", {"children": [FakeNode("Pose", {"children": []}, defname="GANTRY")]}),
                        "GANTRY").getDef() == "GANTRY"


def test_ray_intersections_agree_with_segment_tests_and_normals_point_out():
    rng = np.random.default_rng(3)
    starts = rng.uniform(-1.5, 1.5, (20000, 3))
    dirs = rng.normal(size=(20000, 3))
    dirs /= np.linalg.norm(dirs, axis=1)[:, None]
    for prim in (Box("b", half=np.array([.1, .2, .3])), Ellipsoid("e", radii=np.array([.3, .1, .2])),
                 Cylinder("c", radius=.1, half_height=.2), Capsule("k", radius=.1, half_height=.2), Sphere("s", radius=.2)):
        prim = prim.moved([0.5, 0.2, -0.1], rotation_matrix([1, 2, 3], 0.7))
        t, n = prim.intersect(starts, dirs)
        hit = np.isfinite(t)
        assert hit.sum() > 50
        assert np.array_equal(hit, prim.hits(starts, starts + dirs * 10))
        p = starts[hit] + dirs[hit] * t[hit][:, None]
        assert not prim.hits(p + n[hit] * 1e-6, p + n[hit] * 1e-6 + 1e-12).any()
        assert prim.hits(p - n[hit] * 1e-6, p - n[hit] * 1e-6 + 1e-12).all()


def test_stray_light_grows_with_led_angle_and_albedo():
    from embeddyn.mechatronics.lightcurtain.array import ReflectionModel, stray_power
    wall = Box("wall", position=np.array([0.0, 0.0, -0.03]), half=np.array([0.5, 0.5, 0.01]))   # 2 cm under the plane
    layout = CurtainLayout(count=5, pitch=0.024, span=0.4)

    def stray(half_angle, albedo, enabled=True):
        head = CurtainHead.build("a", layout, EmitterSpec(intensity=0.01, half_angle=half_angle), ReceiverSpec(half_angle=40))
        return stray_power(head, Scene([wall]), ReflectionModel(enabled=enabled, albedo=albedo, rays=512), [2])[0, 2]

    assert stray(10, 0.5, enabled=False) == 0.0
    assert stray(40, 0.5) > 1.3 * stray(10, 0.5) > 0          # more flux at the same on-axis intensity
    assert stray(40, 0.8) == pytest.approx(stray(40, 0.4) * 2, rel=1e-9)


def test_tilted_narrow_leds_lose_their_margin():
    def margin(half_angle, sigma):
        raw = _bench(scenario={"type": "static", "frames": 2})
        raw["curtain"]["emitter"] = {"intensity": "10 mW/sr", "half_angle": f"{half_angle} deg", "tilt_sigma": f"{sigma} deg"}
        return run_bench(raw)["metrics"]["clear_margin"]
    assert margin(3, 3) < 0.6 * margin(3, 0)
    assert margin(20, 3) > 0.8 * margin(20, 0)


def _webots() -> str | None:
    import os
    import shutil
    home = os.environ.get("WEBOTS_HOME")
    candidate = Path(home) / "webots" if home else None
    return str(candidate) if candidate and candidate.exists() else shutil.which("webots")


@pytest.mark.skipif(_webots() is None, reason="Webots not found (set WEBOTS_HOME)")
def test_webots_world_scans_and_moves_the_heads(tmp_path):
    """Opt-in: run the generated world headless for 1.5 s and read the controller's frame log."""
    import csv
    import subprocess
    from embeddyn.mechatronics.lightcurtain.webots_world import write_world
    worlds = ROOT / "examples/mechatronics/worlds"
    world = worlds / f"_pytest_{tmp_path.name}.wbt"
    log = tmp_path / "frames.csv"
    try:
        write_world(BENCH, world, ["--quit-after", "1.5", "--log", str(log)])
        subprocess.run([_webots(), "--batch", "--minimize", "--no-rendering", "--mode=fast", "--stdout", "--stderr",
                        str(world)], check=True, timeout=300, capture_output=True)
    finally:
        world.unlink(missing_ok=True)
        (worlds / f".{world.stem}.wbproj").unlink(missing_ok=True)     # Webots' per-world GUI state
    rows = list(csv.DictReader(log.open()))
    heads = {row["head"] for row in rows}
    assert heads == {"a", "b"}
    assert len(rows) > 2 * 400                                   # 1.5 s of 3.2 ms frames per head
    assert all(row["algorithm"] == "sequential" for row in rows)
    assert min(float(row["radius"]) for row in rows) < 0.34      # the heads moved in
    assert sum(int(row["false"]) for row in rows) == 0
