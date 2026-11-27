"""Generate a Webots world for a light-curtain bench file.

The world holds the obstacles as static Solids (one per primitive, so they
can be dragged in the 3-D view), a supervisor Robot "spect" with the gantry,
the detector heads and one IrLightCurtain PROTO per head, and the
`light_curtain` controller that runs the bench code (`HeadScanner`,
`ContourFollower`) against the geometry it reads back from the scene.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .bench import BenchConfig
from .scene import Box, Capsule, Cylinder, Ellipsoid, Primitive

SKIN = "0.93 0.76 0.62"
GREY = "0.35 0.38 0.42"


def axis_angle(rotation: np.ndarray) -> tuple[float, float, float, float]:
    """3x3 rotation matrix -> (x, y, z, angle) as Webots writes rotations."""
    r = np.asarray(rotation, dtype=float)
    angle = math.acos(max(-1.0, min(1.0, (np.trace(r) - 1) / 2)))
    if angle < 1e-9:
        return (0.0, 0.0, 1.0, 0.0)
    if abs(angle - math.pi) < 1e-6:
        diag = np.sqrt(np.maximum((np.diag(r) + 1) / 2, 0.0))
        k = int(np.argmax(diag))
        axis = r[:, k] / (2 * diag[k])
        axis[k] = diag[k]
        axis /= np.linalg.norm(axis)
        return (float(axis[0]), float(axis[1]), float(axis[2]), math.pi)
    axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / (2 * math.sin(angle))
    return (float(axis[0]), float(axis[1]), float(axis[2]), angle)


def _f(v: float) -> str:
    return f"{v:.6g}"


def _vec(values) -> str:
    return " ".join(_f(float(v)) for v in values)


def _rot(rotation: np.ndarray) -> str:
    return _vec(axis_angle(rotation))


def _color(prim: Primitive) -> str:
    return GREY if prim.name in ("couch", "table") or isinstance(prim, Box) else SKIN


def obstacle_node(prim: Primitive) -> str:
    """A static Solid whose visual shapes the controller reads back as optical obstacles."""
    color = _color(prim)
    appearance = f"PBRAppearance {{ baseColor {color} roughness 0.7 metalness 0 }}"
    if isinstance(prim, Ellipsoid):
        inner = (f"Transform {{ scale {_vec(prim.radii)} children [ Shape {{ appearance {appearance} "
                 f"geometry Sphere {{ radius 1 subdivision 4 }} }} ] }}")
    elif isinstance(prim, Box):
        inner = f"Shape {{ appearance {appearance} geometry Box {{ size {_vec(prim.half * 2)} }} }}"
    elif isinstance(prim, Capsule) and prim.half_height == 0:
        inner = f"Shape {{ appearance {appearance} geometry Sphere {{ radius {_f(prim.radius)} subdivision 3 }} }}"
    elif isinstance(prim, Capsule):
        inner = (f"Shape {{ appearance {appearance} geometry Capsule {{ radius {_f(prim.radius)} "
                 f"height {_f(2 * prim.half_height)} subdivision 16 }} }}")
    elif isinstance(prim, Cylinder):
        inner = (f"Shape {{ appearance {appearance} geometry Cylinder {{ radius {_f(prim.radius)} "
                 f"height {_f(2 * prim.half_height)} subdivision 24 }} }}")
    else:  # pragma: no cover - every primitive type is handled above
        raise ValueError(f"no Webots shape for {type(prim).__name__}")
    name = prim.name or "obstacle"
    return (f"DEF {name.upper().replace('-', '_')} Solid {{\n  translation {_vec(prim.position)}\n"
            f"  rotation {_rot(prim.rotation)}\n  name \"{name}\"\n  children [ {inner} ]\n}}")


def curtain_node(config: BenchConfig, prefix: str) -> str:
    e, r, lay = config.emitter, config.receiver, config.layout
    algorithm = dict(config.algorithm)
    name = algorithm.pop("name")
    return (f"IrLightCurtain {{\n"
            f"            name \"{prefix}curtain\"\n            prefix \"{prefix}\"\n"
            f"            count {lay.count}\n            pitch {_f(lay.pitch)}\n            span {_f(lay.span)}\n"
            f"            receiverOffset {_f(lay.receiver_offset)}\n"
            f"            emitterIntensity {_f(e.intensity or 0.0)}\n            emitterPower {_f(e.power or 0.0)}\n"
            f"            emitterHalfAngle {_f(e.half_angle)}\n            emitterAperture {_f(e.aperture)}\n"
            f"            emitterDrive {_f(e.drive)}\n            emitterTilt {_f(e.tilt)}\n"
            f"            emitterYaw {_f(e.yaw)}\n            tiltSigma {_f(e.tilt_sigma)}\n"
            f"            intensitySpread {_f(e.spread)}\n"
            f"            receiverHalfAngle {_f(r.half_angle)}\n            receiverArea {_g(r.area)}\n"
            f"            receiverAperture {_f(r.aperture)}\n"
            f"            algorithm \"{name}\"\n            algorithmParams \"{_escape(json.dumps(algorithm))}\"\n"
            f"          }}")


def _g(v: float) -> str:
    return f"{v:.6g}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def world_text(config: BenchConfig, config_path: str, controller_args: list[str] | None = None) -> str:
    heads = []
    for mount in config.mounts:
        prefix = f"{mount.name}_"
        def_name = mount.name.upper()
        heads.append(f"""      DEF HEAD_{def_name} Pose {{
        rotation 1 0 0 {_f(mount.angle)}
        children [
          DEF SLIDE_{def_name} Pose {{
            translation 0 0 {_f(mount.radius)}
            children [
              Pose {{
                translation 0 0 0.06
                children [
                  Shape {{
                    appearance PBRAppearance {{ baseColor 0.75 0.78 0.82 roughness 0.4 metalness 0.3 }}
                    geometry Box {{ size 0.4 0.54 0.12 }}
                  }}
                ]
              }}
              DEF CURTAIN_{def_name} Pose {{
                translation 0 0 {_f(-mount.below_face)}
                children [
          {curtain_node(config, prefix)}
                ]
              }}
            ]
          }}
        ]
      }}""")
    obstacles = "\n".join(obstacle_node(p) for p in config.scene.primitives)
    args = " ".join(f'"{_escape(a)}"' for a in ["--config", config_path, *(controller_args or [])])
    return f"""#VRML_SIM R2025a utf8
# Generated by `embeddyn mechatronics curtain-webots` from a light-curtain bench file.
# The obstacles are static Solids (drag them in the 3-D view); the supervisor
# robot "spect" runs embeddyn's scan algorithms on the IrLightCurtain heads.

EXTERNPROTO "../protos/IrLightCurtain.proto"

WorldInfo {{
  info [ "{_escape(config.name)}: infrared light-curtain bench (embeddyn)" ]
  basicTimeStep 8
}}
Viewpoint {{
  orientation -0.1822 0.0621 0.9813 2.496
  position 1.9 -1.35 0.85
}}
Background {{
  skyColor [ 0.16 0.18 0.22 ]
}}
DirectionalLight {{
  direction -0.4 0.5 -1
  intensity 2.2
  castShadows TRUE
}}
DirectionalLight {{
  direction 0.5 -0.3 0.6
  intensity 0.6
}}
Pose {{
  translation 0 0 -0.9
  children [
    Shape {{
      appearance PBRAppearance {{ baseColor 0.55 0.57 0.6 roughness 0.9 metalness 0 }}
      geometry Plane {{ size 6 6 }}
    }}
  ]
}}
{obstacles}
DEF SPECT Robot {{
  name "spect"
  supervisor TRUE
  controller "light_curtain"
  controllerArgs [ {args} ]
  children [
    DEF GANTRY Pose {{
      children [
        Pose {{
          translation 0.32 0 0
          rotation 0 1 0 1.5708
          children [
            Shape {{
              appearance PBRAppearance {{ baseColor 0.6 0.62 0.66 transparency 0.75 roughness 0.5 }}
              geometry Cylinder {{ radius 0.62 height 0.04 subdivision 64 }}
            }}
          ]
        }}
{chr(10).join(heads)}
      ]
    }}
  ]
}}
"""


def write_world(config_file: Path, output: Path, controller_args: list[str] | None = None) -> Path:
    from .bench import load_bench
    raw = load_bench(config_file)
    config = BenchConfig.from_mapping(raw)
    controller_dir = output.resolve().parent.parent / "controllers" / "light_curtain"
    relative = os.path.relpath(config_file.resolve(), controller_dir)
    output.write_text(world_text(config, relative, controller_args))
    return output
