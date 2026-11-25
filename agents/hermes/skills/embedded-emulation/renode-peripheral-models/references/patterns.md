# Patterns that worked (C#, Renode 1.17)

## Interrupt source pulse

```csharp
IRQ = new GPIO();                 // platform: IRQ -> intc@<vector>
IRQ.Blink();                      // flag set on the rising edge, cleared by software
```

## Publish the best interrupt to the CPU (controller side)

```csharp
cpu.SetPendingInterrupt(best, bestPriority);   // export into tlib
IRQ.Set(best >= 0);                            // the CPU's IRQ line, for wake-up
```

## Bus action that takes time (UART char, SPI word, I2C bit, CAN frame)

```csharp
timer = new LimitTimer(machine.ClockSource, 1000000000, this, "uart-tx", limit: 1,
    direction: Direction.Ascending, workMode: WorkMode.OneShot, eventEnabled: true);
timer.LimitReached += Done;
// start:
timer.Enabled = false; timer.Limit = (ulong)Math.Round(bits * 1e9 / baud); timer.Value = 0; timer.Enabled = true;
```

## Lazy timer (no event per period)

- Run the LimitTimer with `Limit = period * lapLength` (lapLength is chosen so that one event
  happens about every 0.25 s).
- `TMR = timer.Value % period`.
- `Matches = baseMatches + laps * lapLength + timer.Value / period`.
- The interrupt controller polls lazy sources before IFS reads. A source becomes eager (lapLength
  = 1, events on) when its interrupt is enabled or a consumer calls `RequireEvents`.

## Clock and power from the system model

```csharp
system.ClockChanged += _ => Apply();           // re-derive frequencies
system.PowerChanged += _ => Apply();           // freeze in Sleep / SIDL in Idle
ulong Fcy => system?.CpuFrequency ?? frequency;
```

## Read fuses only once the firmware is loaded

```csharp
machine.StateChanged += (m, a) => { if(a.CurrentState == MachineStateChangedEventArgs.State.Started) ConfigureFromFuses(); };
```

## Port mirrors (one pin feeding several peripherals)

- Port outputs 16+n, 32+n, ... carry pin n's level.
- Platform: `portd: 24 -> intc@65` and `40 -> ic1@0` (RD8 feeds both INT1 and IC1).
- Peripheral outputs drive the port's inputs 16+n (`oc1: Pin -> portd@16`).

## Raising a CPU trap from C#

A tlib export, `tlib_raise_trap(n)`, sets the pending trap and the status bit, and clears `wfi`.
The step helper services pending traps before the next instruction.
