# Merging Renode v1.17.1 into the custom-cores fork (2026-11-23)

The xsession `custom-cores` fork of Renode (custom CPU architectures, PIC12F629
platform, platform reorganisation) was brought up to upstream **Renode v1.17.1**
by **merging**, not rebasing. Both histories stay intact, and every decision is
in a commit of its own. This page records what happened and why. Read it
together with the merge commits' messages.

Why: v1.17 adds the External Control **CAN and SPI** commands, the STM32H7
reference platform and precise BusFaults (see the integration research,
`docs/research/integration-candidates.md` at the monorepo root).

## Branches

All three are called `merge-renode-1.17.1` and are based on the fork's
`custom-cores` head. They were built and tested locally in
`/media/riko/model_store/renode-merge/` and **are not pushed yet**.

| Repository | Fork base | Upstream merged | Fork-only / upstream-only commits |
|---|---|---|---|
| xsession/tlib | `ccd92f6e` | antmicro/tlib `c04522a9` (the v1.17.1 pin) | 5 / 248 |
| xsession/renode-infrastructure | `6094ae35b` | renode-infrastructure `137582ff0` (the v1.17.1 pin) | 14 / 1146 |
| xsession/renode | `6520932b` | renode `v1.17.1` (`e91f8232`) | 18 / 895 |

Merges are bottom-up: tlib, then infrastructure pointing at the merged tlib,
then renode pointing at the merged infrastructure.

## Commits, in order

**tlib**
1. `3caf45ac` Merge antmicro/tlib at the v1.17.1 pin. No conflicts.
2. `fe5e8320` Custom cores follow the `void gen_intermediate_code_epilogue` API.
   Upstream changed the return type from `uint32_t`. All seven custom cores
   (AVR, C2000, dsPIC33, MCS51, PIC16, PIC18, STM8) returned 0, which callers
   ignored. It merged cleanly as text and failed to compile.

**renode-infrastructure**
1. `4ad90123e` Merge at the v1.17.1 pin, 21 conflicts:
   * Files an earlier stale merge (`89f0175b6`) had reverted take upstream:
     the STM32 ADCs, DMA, DMAMUX, SPI, MxcUart and CommandLineInterface. The
     same applies to `STM32L5_ADC`, `STM32_FDCAN` and `PicoRV_SPI`, which that
     merge had deleted.
   * Added on both sides: `LPS25HB`, `VEML7700`, `ST7789` and `IIlluminanceSensor`
     take the full upstream models.
   * `PythonEngine`: upstream's `CreateEngine()` now starts from the fork's
     UTF-8 runtime-IO setup, which fixes the console code-page crash.
   * `Infrastructure.csproj` is upstream's SDK-style project. Sources are globbed,
     and `translate-*.so` libraries load from disk, so the fork's explicit
     items are obsolete.
2. `29e258094` `STM32_ADC` implements `IADC`. Previously this was embeddyn patch 0001.
3. `078887d54` `STM32_Timer` keeps PWM active for `CCR >= ARR`. Previously patch
   0003, now ported to the v1.17 channel structure.
4. `d17222bd7` Adapt `STM32_ADC` and `WebSocketConnection` to v1.17:
   * In v1.17, `IADC` is a container of per-channel voltage sources. The F4 ADC
     now converts the channel voltage at `referenceVoltage` (default 3.3 V),
     and `FeedSample` samples still take precedence.
   * Both sides had added `TriggerResize`, and the merge kept both copies. It
     is now one method that raises both events.
5. `96d76bfc5` tlib pointer → `fe5e8320`.
6. `e4a4d8ce3` Restore 23 upstream files reverted by the stash commit
   `40d3abacd` ("WIP on master"):
   * That commit had taken register helpers (`ResetRegister`,
     `WithConditionallyWritableValueField`), `STM32Series` and others back to
     a pre-April state.
   * The merge kept those reversions silently wherever upstream had not touched
     a file since. Upstream's FDCAN, SPI and sensor models then did not compile.

   Patch 0002 (`STM32_ADC_Common` IADC) is obsolete: upstream implements it.

**renode**
1. `5c93d1b2` Merge v1.17.1, 16 conflicts:
   * `build.sh`: upstream's core list (adds `arm-experimental`) plus the fork's
     cores and the `RENODE_CUSTOM_CORES*` hooks.
   * `Renode.csproj`, External Control `SystemBus.cs` and
     `SystemCCortexMSignalsUnit.cs` take upstream. The fork's edits only
     adapted to its older infrastructure.
   * `ExternalControlServer.cs` is deleted, as upstream renamed it to
     `ExternalControlEndpoint`.
   * Two scripts keep the fork's reorganised platform paths.
   * Submodules `lib/*`, `src/UI` and `tools/*` take the v1.17.1 pins.
2. `82e4797b` 25 upstream tests and scripts pointed at flat platform paths. Git's
   directory-rename detection had already moved the new platforms into the
   fork's vendor folders, so the references now follow.
3. `9e3549a7` Infrastructure pointer update (ADC and timer fixes).
4. `960d69bf` The remaining six `lib/*` submodules (BigGustave, CxxDemangler,
   ELFSharp, FdtSharp, InpliTftpServer, Packet.Net) take the v1.17.1 pins. The
   fork had pinned older commits that target .NET Framework 4.6.2. They didn't
   conflict, because upstream hadn't changed them.
5. `a7620b2d` `RenodeKeywords`: back to upstream's LINQ `TakeLast`. Plus the
   infrastructure pointer.
6. `1118afa7` Drop the fork's `SupportedRIDAttribute` backport. Infrastructure
   defines it now, and two definitions made `TypeManager` abort at start-up.

Kept from the fork on purpose: the custom cores and their platforms, the
console code-page fix in `Program.cs`, the Python-engine and TypeManager
robustness edits, and the platform reorganisation.

## Lessons for the next merge

* The fork's earlier merges and a committed stash had silently reverted
  upstream code. A textual merge can't see that. The build and the
  tests found it: grep the fork's own commits for reverts before merging.
* Submodule pins the fork changed to match an older layer need checking even
  when they don't conflict.
* Renode 1.17 builds with **.NET 10** (`TFM=net10.0`). The old fork used .NET 8.

## Result

* Builds with `./build.sh` (.NET 10.0.404 SDK, user-local).
* `renode --version` reports `v1.17.1`, build `1.17.1+20261123gita7620b2dc`.
* embeddyn's Renode tests all pass on it through the new External Control
  message protocol. That covers the STM32F4 motor loop with its golden trace,
  the STM32L476 RC servo, the UART console, and the new CAN tests.
* The same tests still pass on the old fork (1.16.1) through the legacy
  protocol; the CAN tests skip there.

## embeddyn changes that go with it

* `adapters/renode_api_v2.py`: a client for the 1.17 message protocol
  (`id:u16 | size:i32 | command:u16 | type:u8 | data`). Time is in
  nanoseconds. Renode's own event requests during `RunFor` (GPIO edges, CAN
  frames) are served by payload type and answered with the event's id.
* The Renode adapter picks the protocol with `api: auto | legacy | message`.
  `auto` reads `renode --version`.
* ADC bindings written in conversion counts declare `counts_vref` (and
  `counts_full_scale`). On 1.17, whose ADCs take microvolts, the adapter
  converts.
* CAN (`can:` option, 1.17 only): received frames become events, and `fields`
  decode them into signals. `can_write()` sends a frame. Example:
  `rc-servo-l476-can.yaml` with `renode/stm32l476-servo-can.resc`, where the
  firmware drives bxCAN (STMCAN) on a CAN hub.
