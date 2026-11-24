# Build Renode with custom cores

embeddyn works with stock Renode ≥ 1.17 for most features. The xsession fork adds custom CPU cores and peripheral fixes (STM32 ADC `IADC`, STM32 timer PWM at CCR ≥ ARR) that the firmware examples rely on. Two builds exist.

## Option A — the custom-cores branch (≤ 1.16 protocol)

```bash
tools/renode/build-custom-cores.sh ~/renode-custom-cores
. ~/renode-custom-cores/env.sh            # sets RENODE_BIN, DOTNET_ROOT
"$RENODE_BIN" --version
```

The script clones the branch, installs a user-local .NET 8 SDK (no root, no Docker), applies the patches in `externals/patches/`, works around the branch's build problems and writes `env.sh`. Every workaround and its proper fix is listed in the [Renode adapter reference](../reference/renode.md#building-the-custom-cores-branch).

## Option B — custom cores merged with Renode 1.17.1 (message protocol)

The fork has been merged with upstream v1.17.1 (merge, not rebase, so history stays visible) on `merge-renode-1.17.1` branches of `renode`, `renode-infrastructure` and `tlib`. This build gives both the custom cores and the 1.17 External Control features (CAN, SPI peripherals). The full record of the merge, the conflicts and each decision is in [Renode 1.17 merge record](../project/renode-1-17-merge.md).

```bash
cd renode && ./build.sh                   # with a .NET 10 SDK on PATH
export RENODE_BIN=$PWD/renode
```

## Check it with embeddyn

```bash
embeddyn mechatronics integration-validate examples/mechatronics/rc-servo-l476.yaml
RENODE_BIN=... pytest tests/mechatronics/test_renode_*.py tests/mechatronics/test_arm_firmware.py -q
```

`api: auto` (the default) picks the protocol from `renode --version`, so the same projects run on either build; SPI and CAN need option B or stock 1.17.

## Tips

* Renode logs go where `options.log` says; with parallel workers each instance writes `<log>.<pid>.log`.
* If a run fails with "Can't start emulation while disposing", an old Renode is still shutting down; the adapter now waits for the whole process group, but a Renode you started by hand on the same port can still be in the way.
* Set `TMPDIR` to a roomy disk: .NET and Renode write temporary files.
