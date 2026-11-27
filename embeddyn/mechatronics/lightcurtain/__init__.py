"""Infrared light-curtain bench: scene ray casting, LED/receiver arrays, scan algorithms,
scenarios and sweeps, and the Webots world generator (see docs/reference/light-curtain-bench.md)."""
from .array import CurtainHead, CurtainLayout, EmitterSpec, LazyCoupling, ReceiverSpec, coupling, geometric_gain
from .bench import BenchConfig, ContourFollower, HeadMount, HeadScanner, contact_radius, load_bench, run_bench, sweep
from .scanning import ALGORITHMS, Sample, ScanAlgorithm, Slot, make_algorithm
from .scene import Box, Capsule, Cylinder, Ellipsoid, Scene, Sphere, scene_from_mapping

__all__ = ["ALGORITHMS", "BenchConfig", "Box", "Capsule", "ContourFollower", "CurtainHead", "CurtainLayout",
           "Cylinder", "Ellipsoid", "EmitterSpec", "HeadMount", "HeadScanner", "LazyCoupling", "ReceiverSpec",
           "Sample", "ScanAlgorithm", "Scene", "Slot", "Sphere", "contact_radius", "coupling", "load_bench", "geometric_gain",
           "make_algorithm", "run_bench", "scene_from_mapping", "sweep"]
