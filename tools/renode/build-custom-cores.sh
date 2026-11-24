#!/usr/bin/env bash
# Build Renode from the xsession/renode `custom-cores` branch on a Linux host
# with a user-local .NET 8 SDK (no Docker, no root), applying the fixes the
# branch currently needs to build and run with Embeddyn.
#
# Usage: tools/renode/build-custom-cores.sh <workdir>
#   <workdir> gets: src/ (Renode checkout), dotnet/, nuget/, env.sh, build.log
#   Afterwards:  . <workdir>/env.sh && <workdir>/src/renode --version
#
# Why each fix is needed is documented in docs/reference/renode.md
# ("Building the custom-cores branch"). Upstreaming them to the fork makes
# this script reduce to clone + ./build.sh.
set -euo pipefail

WORK="$(mkdir -p "${1:?usage: $0 <workdir>}" && cd "$1" && pwd)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRANCH="${RENODE_BRANCH:-custom-cores}"
URL="${RENODE_URL:-https://github.com/xsession/renode.git}"
# bc-csharp commit whose crypto_NET.csproj matches the fork's other _NET projects
BC_CSHARP_COMMIT="33d24f1307d267c34b7f1439bc159a8825c5aa3d"

cat > "$WORK/env.sh" <<EOF
# Source before running Renode built by build-custom-cores.sh
export DOTNET_ROOT="$WORK/dotnet" PATH="$WORK/dotnet:$WORK/src:\$PATH"
export NUGET_PACKAGES="$WORK/nuget" DOTNET_CLI_HOME="$WORK/dotnet-home"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
# Checkouts on volumes owned by another user trip git's ownership check.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'
export RENODE_BIN="$WORK/src/renode"
EOF
# shellcheck disable=SC1091
. "$WORK/env.sh"
mkdir -p "$WORK/nuget" "$WORK/dotnet-home" "$WORK/tmp"
export TMPDIR="$WORK/tmp"

step() { printf '\n==> %s\n' "$*"; }

if [[ ! -x "$WORK/dotnet/dotnet" ]]; then
    step "Installing .NET 8 SDK into $WORK/dotnet"
    curl -sSfL https://dot.net/v1/dotnet-install.sh -o "$WORK/dotnet-install.sh"
    bash "$WORK/dotnet-install.sh" --channel 8.0 --install-dir "$WORK/dotnet" --no-path
fi

if [[ ! -d "$WORK/src/.git" && ! -f "$WORK/src/.git" ]]; then
    step "Cloning $URL ($BRANCH) with submodules"
    git clone --branch "$BRANCH" --depth 1 "$URL" "$WORK/src"
fi
cd "$WORK/src"

step "Syncing submodules (retried; GitHub resets long clones occasionally)"
for attempt in 1 2 3; do
    if git submodule update --init --recursive --depth 1 --jobs 4; then
        break
    fi
    [[ $attempt == 3 ]] && { echo "submodule sync failed" >&2; exit 1; }
    sleep 5
done

step "Pinning lib/bc-csharp to $BC_CSHARP_COMMIT"
git -C lib/bc-csharp fetch -q --depth 1 origin "$BC_CSHARP_COMMIT" || true
git -C lib/bc-csharp checkout -q "$BC_CSHARP_COMMIT"

step "Applying Embeddyn Renode patches"
for patch in "$REPO_ROOT"/externals/patches/renode-infrastructure/*.patch; do
    if patch -d src/Infrastructure -p1 --dry-run -R -s < "$patch" >/dev/null 2>&1; then
        echo "already applied: $(basename "$patch")"
    else
        patch -d src/Infrastructure -p1 < "$patch"
    fi
done

step "Fetching prebuilt libraries (renode-resources)"
./tools/building/fetch_libraries.sh

step "Adapting solution/projects to the branch's _NET project layout"
dotnet sln Renode.sln remove \
    src/Infrastructure/src/Emulator/Extensions/MonitorTests/MonitorTests.csproj \
    src/Infrastructure/src/Emulator/Main/Tests/UnitTests/UnitTests.csproj \
    src/Infrastructure/src/Emulator/Peripherals/Test/PeripheralsTests/PeripheralsTests.csproj \
    tests/unit-tests/RenodeTests/RenodeTests.csproj \
    lib/options-parser/OptionsParserTests.csproj \
    lib/termsharp/xwt/Xwt.WPF/Xwt.WPF.csproj \
    src/Plugins/SystemCPlugin/SystemCPlugin.csproj >/dev/null 2>&1 || true
python3 - <<'PY'
import re
from pathlib import Path

pattern = re.compile(r'([A-Za-z0-9_.\\/-]+?)\.csproj')
files = [Path("Renode.sln")] + [p for p in [*Path("src/Renode").glob("*.csproj"), *Path("src/Plugins").glob("*/*.csproj")]
                                if not p.name.endswith("_NET.csproj")]
for f in files:
    text = f.read_text(encoding="utf-8-sig")
    def repoint(match):
        rel = match.group(1)
        if rel.endswith("_NET"):
            return match.group(0)
        sibling = f.parent / (rel.replace("\\", "/") + "_NET.csproj")
        return rel + "_NET.csproj" if sibling.exists() else match.group(0)
    new = pattern.sub(repoint, text)
    # Test/WPF/SystemC projects removed above may still be listed under their _NET names.
    if f.name == "Renode.sln":
        for name in ("MonitorTests", "UnitTests", "PeripheralsTests", "OptionsParserTests", "Xwt.WPF", "SystemCPlugin", "RenodeTests"):
            new = re.sub(r'Project\("[^"]+"\) = "%s", "[^"]+", "\{[^}]+\}"\r?\nEndProject\r?\n' % re.escape(name), "", new)
    if f.name == "Renode.csproj":
        new = new.replace('Include="System.CodeDom" Version="6.0.0"', 'Include="System.CodeDom" Version="8.0.0"')
    if new != text:
        f.write_text(new, encoding="utf-8")
        print("updated", f)

# Top-level sources newer than the branch's src/Infrastructure.
fixes = {
    Path("src/Renode/RobotFrameworkEngine/RenodeKeywords.cs"):
        [("Misc.TakeLast(", "System.Linq.Enumerable.TakeLast(")],
    **{p: [("Antmicro.Renode.Exceptions.UnreachableException", "System.Diagnostics.UnreachableException")]
       for p in Path("src/Renode/Network/ExternalControl").glob("*.cs")},
}
for path, replacements in fixes.items():
    text = path.read_text(encoding="utf-8-sig")
    new = text
    for old, repl in replacements:
        new = new.replace(old, repl)
    if new != text:
        path.write_text(new, encoding="utf-8")
        print("patched", path)
PY
# Projects of this generation import output/properties.csproj (older build.sh created it).
mkdir -p output
cp src/Infrastructure/src/Emulator/Cores/linux-properties.csproj output/properties.csproj

step "Building native cores (tlib)"
./build.sh --no-gui --skip-fetch > "$WORK/build.log" 2>&1 || true
# Older Infrastructure_NET embeds cores from Cores/bin/<cfg>/lib; the build writes linux-x64.
CORES_BIN="src/Infrastructure/src/Emulator/Cores/bin/Release"
if [[ -d "$CORES_BIN/linux-x64" && ! -e "$CORES_BIN/lib" ]]; then
    ln -s linux-x64 "$CORES_BIN/lib"
fi

step "Building Renode (.NET)"
if ! ./build.sh --no-gui --skip-fetch >> "$WORK/build.log" 2>&1; then
    grep -E ": error " "$WORK/build.log" | sort -u | tail -20
    echo "Build failed, see $WORK/build.log" >&2
    exit 1
fi

OUT=output/bin/Release
# build.sh symlinks RenodeWPF.dll -> Renode.dll; this TypeManager loads both and aborts.
if [[ -L "$OUT/RenodeWPF.dll" ]]; then
    unlink "$OUT/RenodeWPF.dll"
fi
# Needed for execution tracing / disassembly.
cp lib/resources/llvm/libllvm-disas.so "$OUT/libllvm-disas.so"

step "Smoke test"
./renode --disable-gui --console --plain -e "quit" < /dev/null | grep -m1 "Renode, version"
echo "Done. Use:  . $WORK/env.sh  (sets RENODE_BIN=$WORK/src/renode)"
