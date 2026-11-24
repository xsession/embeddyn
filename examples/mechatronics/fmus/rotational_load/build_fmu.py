"""Build rotational_load.fmu (FMI 2.0) and rotational_load_fmi3.fmu (FMI 3.0) from model.c.

Usage: python build_fmu.py [output_dir]      (needs a C compiler: $CC or cc)
"""
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = "{8c4e810f-3df3-4a00-8276-176fa3c9f000}"
VARIABLES = [  # name, vr, causality, variability, start, description
    ("tau", 0, "input", "continuous", 0.0, "drive torque [N*m]"),
    ("J", 1, "parameter", "fixed", 0.02, "inertia [kg*m^2]"),
    ("b", 2, "parameter", "fixed", 0.01, "viscous damping [N*m*s/rad]"),
    ("tau_load", 3, "input", "continuous", 0.0, "load torque [N*m]"),
    ("coulomb", 4, "parameter", "fixed", 0.0, "Coulomb friction [N*m]"),
    ("w", 5, "output", "continuous", None, "speed [rad/s]"),
    ("theta", 6, "output", "continuous", None, "angle [rad]"),
    ("accel", 7, "output", "continuous", None, "acceleration [rad/s^2]"),
]


def description(version: str) -> str:
    rows = []
    for name, vr, causality, variability, start, text in VARIABLES:
        initial = ' initial="exact"' if causality == "output" else ""
        if version == "2.0":
            start_attr = f' start="{start}"' if start is not None else ""
            rows.append(f'    <ScalarVariable name="{name}" valueReference="{vr}" causality="{causality}" '
                        f'variability="{variability}" description="{text}"{initial if causality != "output" else ""}>'
                        f'<Real{start_attr}/></ScalarVariable>')
        else:
            start_attr = f' start="{start}"' if start is not None else ""
            rows.append(f'    <Float64 name="{name}" valueReference="{vr}" causality="{causality}" '
                        f'variability="{variability}" description="{text}"{start_attr}/>')
    outputs = [i + 1 for i, v in enumerate(VARIABLES) if v[2] == "output"]
    if version == "2.0":
        structure = "".join(f'<Unknown index="{i}"/>' for i in outputs)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="rotational_load" guid="{TOKEN}" generationTool="embeddyn example">
  <CoSimulation modelIdentifier="rotational_load" canHandleVariableCommunicationStepSize="true"
                canGetAndSetFMUstate="true" canSerializeFMUstate="true"/>
  <DefaultExperiment stepSize="1e-4"/>
  <ModelVariables>
{chr(10).join(rows)}
  </ModelVariables>
  <ModelStructure><Outputs>{structure}</Outputs><InitialUnknowns>{structure}</InitialUnknowns></ModelStructure>
</fmiModelDescription>
"""
    # FMI 3 needs exactly one independent variable and lists calculated outputs as InitialUnknowns.
    rows.insert(0, '    <Float64 name="time" valueReference="999" causality="independent" variability="continuous"/>')
    structure = "".join(f'<Output valueReference="{v[1]}"/>' for v in VARIABLES if v[2] == "output")
    structure += "".join(f'<InitialUnknown valueReference="{v[1]}"/>' for v in VARIABLES if v[2] == "output")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="3.0" modelName="rotational_load" instantiationToken="{TOKEN}" generationTool="embeddyn example">
  <CoSimulation modelIdentifier="rotational_load" canHandleVariableCommunicationStepSize="true"
                canGetAndSetFMUState="true" canSerializeFMUState="true"/>
  <DefaultExperiment stepSize="1e-4"/>
  <ModelVariables>
{chr(10).join(rows)}
  </ModelVariables>
  <ModelStructure>{structure}</ModelStructure>
</fmiModelDescription>
"""


def platform_folder(version: str) -> tuple[str, str]:
    if sys.platform.startswith("linux"):
        arm = platform.machine().lower() in ("aarch64", "arm64")
        return ("linux64" if version == "2.0" else ("aarch64-linux" if arm else "x86_64-linux")), ".so"
    if sys.platform == "darwin":
        return ("darwin64" if version == "2.0" else ("aarch64-darwin" if platform.machine() == "arm64" else "x86_64-darwin")), ".dylib"
    raise SystemExit("this example build script supports Linux and macOS")


def build(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    compiler = os.environ.get("CC", "cc")
    built = []
    for version, flags, name in (("2.0", [], "rotational_load.fmu"), ("3.0", ["-DFMI3"], "rotational_load_fmi3.fmu")):
        folder, suffix = platform_folder(version)
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / f"rotational_load{suffix}"
            subprocess.run([compiler, "-shared", "-fPIC", "-O2", "-Wall", *flags, "-o", str(library),
                            str(HERE / "model.c"), "-lm"], check=True)
            target = output_dir / name
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("modelDescription.xml", description(version))
                archive.write(library, f"binaries/{folder}/rotational_load{suffix}")
                archive.writestr("resources/README.txt", "no resources\n")
                archive.write(HERE / "model.c", "sources/model.c")
            built.append(target)
    return built


if __name__ == "__main__":
    for path in build(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent):
        print(path)
