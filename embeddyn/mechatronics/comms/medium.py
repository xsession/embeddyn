"""Impaired communication media: noise, fading, interference and crosstalk on wired and radio traffic.

A project's ``media:`` section names event topics (a CAN wire, a radio channel...) and
how the medium spoils the frames on them. The orchestrator passes every event published
on such a topic through `Media.apply` before any receiver, or the analyzer, sees it:

```yaml
media:
  can:                          # the CAN bus wire (the CanBus model's `wire` topic)
    kind: wired
    bit_error_rate: 1.0e-5      # random bit flips (per payload bit)
    loss: 0.0                   # frames that vanish
    jitter_us: 0                # extra random delay
    modulation: {shape: sine, frequency: 2.0, depth: 0.8}   # impairment strength over time (EMI, fading)
    bursts: [{start: 0.8, end: 1.0, bit_error_rate: 1.0e-3, loss: 0.1}]    # noise bursts (also period/duty)
    crosstalk:
      - {signal: electrical.motor_current, ber_per_unit: 2.0e-5}  # coupling from an analog signal (|value|)
      - {topic: ble, window_us: 400, probability: 0.3, bits: 2}   # coupling from frames on another topic
    can_errors: true            # corrupted CAN frames: error frame + retransmission (default for CAN)
    seed: 1
  ble:
    kind: radio
    rssi_offset: -12            # dB added to every packet (distance, walls)
    fading_std: 6               # dB, Gaussian per packet (multipath)
    sensitivity: -92            # packets weaker than this are lost
    interferer: {period_ms: 10, duration_us: 1500, loss: 0.9}   # a periodic interferer (e.g. microwave oven)
```

Strength over time: a burst or the base settings apply while their window is active
(``start``/``end``, ``period``/``duty``); ``modulation`` scales the error rate and the
loss by ``1 - depth + depth * w(t)`` with ``w`` a sine (0..1) or square (0/1) of
``frequency`` Hz. Losses keep their place in the capture: the event stays, flagged
``"medium": {"lost": ...}``, and receivers do not get it. Flipped bits are listed in
``"medium": {"flipped": [...]}``; receivers get the corrupted bytes and their own
checks (CRC, FCS, MIC) decide.

CAN (`can_errors`): a frame with bit errors is not delivered; an error frame takes its
place on the bus and the frame is sent again (up to `max_retries`), like a real
transmitter that sees the error flag.

UART, RS-485, SPI, I2C and Ethernet frames go through `comms.faults` instead of the
generic bit flips: a bit-level UART line (baud mismatch, glitches, breaks, overruns,
bus contention, floating RS-485 lines), SPI clock-mode and timing errors, I2C bus
hangs and NACKs, Ethernet FCS errors and collisions, switched on by a ``faults:`` section.
"""
from __future__ import annotations

import copy
import math
import random
import threading
from typing import Any, Callable, Mapping

from . import faults
from .faults import window as _window
from .physical import cable_effects, ethernet_check, i2c_check, radio_link, rs485_check, spi_check, uart_check

_RADIO = ("ble", "wifi", "lora", "nfc", "rfid")


def _modulation(spec: Mapping[str, Any] | None, t: float) -> float:
    if not spec:
        return 1.0
    depth = min(1.0, max(0.0, float(spec.get("depth", 1.0))))
    frequency = float(spec.get("frequency", 1.0))
    phase = 2 * math.pi * frequency * t + float(spec.get("phase", 0.0))
    wave = 0.5 + 0.5 * math.sin(phase) if spec.get("shape", "sine") == "sine" else (1.0 if math.sin(phase) >= 0 else 0.0)
    return 1.0 - depth + depth * wave


class Medium:
    """Impairments of one topic."""

    def __init__(self, topic: str, spec: Mapping[str, Any]) -> None:
        self.topic = topic
        self.update(spec)
        self.stats = {"frames": 0, "corrupted": 0, "lost": 0, "errors": 0, "retransmissions": 0, "abandoned": 0,
                      "flipped_bits": 0}

    def update(self, spec: Mapping[str, Any]) -> None:
        self.spec = dict(spec or {})
        self.random = random.Random(self.spec.get("seed", 1))
        self.kind = str(self.spec.get("kind", "radio" if self.topic.split(".")[0] in _RADIO else "wired"))
        self.links: dict[str, dict[str, Any]] = {}            # last link budget per sender (radio)
        self.cable_cache: dict[str, dict[str, Any]] = {}       # cable effects per protocol (wired)
        self.state: dict[str, Any] = {}                        # fault models' bus state (who drives, overruns...)

    def cable(self, proto: str | None, rate: float | None = None) -> dict[str, Any] | None:
        """Cable effects for a protocol at a bit rate (`rate`: baud, SPI clock, Ethernet speed)."""
        cable = self.spec.get("cable")
        if not cable:
            return None
        rate = rate or self.spec.get("bitrate")
        key = f"{proto}@{rate}"
        if key not in self.cable_cache:
            effects = cable_effects(cable, proto, self.spec.get("bitrate"))
            if proto == "i2c" or cable.get("i2c"):
                effects["i2c"] = i2c_check({**cable, **(cable.get("i2c") or {})})
            checks = {"rs485": rs485_check, "uart": uart_check, "spi": spi_check, "eth": ethernet_check}
            if proto in checks:
                effects[str(proto)] = checks[str(proto)](cable, rate)
            self.cable_cache[key] = effects
        return self.cable_cache[key]

    def protocol(self) -> str | None:
        """The protocol on this topic: ``protocol:`` in the spec, else from the topic name (can.*, i2c ...)."""
        if self.spec.get("protocol"):
            return str(self.spec["protocol"])
        head = self.topic.split(".")[0]
        return head if head in ("can", "i2c", "spi", "uart", "rs485", "eth") else None

    def derived(self) -> dict[str, Any]:
        """What the physical models work out: link budgets (radio), cable and I2C checks (wired)."""
        out: dict[str, Any] = {}
        if self.links:
            out["links"] = self.links
        if self.spec.get("cable"):
            out["cable"] = self.cable(self.protocol())
        return out

    def strength(self, t: float) -> tuple[float, float]:
        """(bit error rate, loss probability) at time t from the base settings, bursts and modulation."""
        spec = self.spec
        ber = float(spec.get("bit_error_rate", 0.0)) if _window(spec, t) else 0.0
        loss = float(spec.get("loss", 0.0)) if _window(spec, t) else 0.0
        for burst in spec.get("bursts") or []:
            if _window(burst, t):
                ber = max(ber, float(burst.get("bit_error_rate", 0.0)))
                loss = max(loss, float(burst.get("loss", 0.0)))
        scale = _modulation(spec.get("modulation"), t)
        return ber * scale, min(1.0, loss * scale)


class Media:
    """All impaired topics of a run; `apply` is called for every published event."""

    def __init__(self, spec: Mapping[str, Any] | None, signal: Callable[[str], float] | None = None) -> None:
        self.media = {str(topic): Medium(str(topic), cfg or {}) for topic, cfg in (spec or {}).items()}
        self.signal = signal or (lambda name: 0.0)
        self.recent: dict[str, list[float]] = {}               # topic -> recent frame times (crosstalk sources)
        self.lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self.media)

    def update(self, topic: str, spec: Mapping[str, Any] | None) -> None:
        """Change a topic's impairments during a run (the Lab's medium panel)."""
        with self.lock:
            if not spec:
                self.media.pop(topic, None)
            elif topic in self.media:
                self.media[topic].update(spec)
            else:
                self.media[topic] = Medium(topic, spec)

    def specs(self) -> dict[str, Any]:
        return {topic: {**medium.spec, "stats": dict(medium.stats), "derived": medium.derived()}
                for topic, medium in self.media.items()}

    def apply(self, t: float, topic: str, payload: Any) -> list[tuple[float, Any]]:
        """The events that come out of the medium for one published event: (time, payload) pairs."""
        times = self.recent.setdefault(topic, [])
        times.append(t)
        if len(times) > 64:
            del times[:32]
        with self.lock:
            medium = self.media.get(topic)
            if medium is None:
                return [(t, payload)]
            return self._apply(medium, t, payload)

    def _apply(self, medium: Medium, t: float, payload: Any) -> list[tuple[float, Any]]:
        spec, rng = medium.spec, medium.random
        medium.stats["frames"] += 1
        if isinstance(payload, dict) and (payload.get("medium") or payload.get("error")):
            return [(t, payload)]                              # already handled (a retransmission's error frame...)
        jitter = float(spec.get("jitter_us", 0.0)) * 1e-6
        if jitter:
            t += rng.random() * jitter
        ber, loss = medium.strength(t)
        notes: dict[str, Any] = {}
        proto = payload.get("proto") if isinstance(payload, dict) else None
        cable = medium.cable(proto)
        pickup = 1.0
        if cable is not None:                                   # wired: delay, EMI pickup, CAN timing, I2C edges
            t += cable["delay_s"]
            pickup = cable["pickup"]
            ber += cable["emi_ber"]
            if cable.get("i2c"):
                ber += cable["i2c"]["ber"]
            if cable["timing_error"] and rng.random() < cable["timing_error"]:
                notes["extra_bits"] = 1
                notes["timing"] = f"cable too long for {cable['bitrate'] / 1000:g} kbit/s (max {cable['max_length_m']} m)"
        for coupling in spec.get("crosstalk") or []:
            if "signal" in coupling:                          # a cable's twist and shield reduce the pickup
                ber += abs(float(self.signal(str(coupling["signal"])))) * float(coupling.get("ber_per_unit", 0.0)) * pickup
            elif "topic" in coupling:
                window = float(coupling.get("window_us", 200.0)) * 1e-6
                near = any(0 <= t - other <= window for other in self.recent.get(str(coupling["topic"]), []))
                if near and rng.random() < float(coupling.get("probability", 1.0)):
                    notes["crosstalk"] = coupling["topic"]
                    notes["extra_bits"] = int(coupling.get("bits", 1))
        payload = copy.deepcopy(payload)
        radio = medium.kind == "radio"
        link = spec.get("link")
        if radio and link and isinstance(payload, dict):        # distance-based link budget
            fade = rng.gauss(0.0, float(spec.get("fading_std", 0.0))) if spec.get("fading_std") else 0.0
            budget = radio_link(payload, link, fade)
            medium.links[str(payload.get("src"))] = {k: budget[k] for k in ("distance_m", "rssi", "snr", "ber", "margin_db",
                                                                             "tag_power_dbm", "path_loss_db") if k in budget}
            if budget.get("rssi") is not None:
                payload["rssi"] = budget["rssi"]
            if budget.get("snr") is not None and payload.get("snr") is not None:
                payload["snr"] = budget["snr"]
            payload["link"] = {"distance_m": budget["distance_m"], "ber": budget["ber"]}
            ber += budget["ber"] if budget["ber"] > 1e-8 else 0.0
            if budget["lost"]:
                notes["lost"] = budget["why"]
        elif radio and isinstance(payload, dict) and payload.get("rssi") is not None:
            rssi = float(payload["rssi"]) + float(spec.get("rssi_offset", 0.0)) + rng.gauss(0.0, float(spec.get("fading_std", 0.0)))
            payload["rssi"] = round(rssi, 1)
            if payload.get("snr") is not None:
                payload["snr"] = round(float(payload["snr"]) + float(spec.get("rssi_offset", 0.0)), 1)
            if spec.get("sensitivity") is not None and rssi < float(spec["sensitivity"]):
                notes["lost"] = f"below sensitivity ({rssi:.1f} dBm)"
        interferer = spec.get("interferer")
        if interferer and "lost" not in notes:
            period = float(interferer.get("period_ms", 10.0)) * 1e-3
            if (t % period) < float(interferer.get("duration_us", 1000.0)) * 1e-6 and rng.random() < float(interferer.get("loss", 1.0)):
                notes["lost"] = "interferer"
        if "lost" not in notes and loss and rng.random() < loss:
            notes["lost"] = "noise"
        if notes.get("lost"):
            medium.stats["lost"] += 1
            return [(t, self._mark(payload, notes))]
        if isinstance(payload, dict) and proto in faults.WIRED and not radio:
            return faults.apply(medium, t, payload, ber, notes, rng, self._flip, self._mark)
        original = payload.get("data") if isinstance(payload, dict) else None
        flipped = self._flip(payload, ber, notes.get("extra_bits", 0), rng)
        if flipped:
            notes["flipped"] = flipped
            notes["original"] = original
            medium.stats["corrupted"] += 1
            medium.stats["flipped_bits"] += len(flipped)
            can = isinstance(payload, dict) and payload.get("proto") == "can"
            if can and spec.get("can_errors", True):
                return self._can_retransmit(medium, t, payload, notes, ber, rng)
        return [(t, self._mark(payload, notes) if notes else payload)]

    def _can_retransmit(self, medium: Medium, t: float, corrupted: dict, notes: dict, ber: float,
                        rng: random.Random) -> list[tuple[float, Any]]:
        """An error frame replaces the corrupted frame, then the transmitter sends it again."""
        original = dict(corrupted)
        original["data"] = notes["original"]
        bitrate = float(medium.spec.get("bitrate", 500_000))
        bits = int(corrupted.get("bits", 111))
        out = [(t, {**self._mark(corrupted, notes), "error": "bit error: error frame, retransmission"})]
        medium.stats["errors"] += 1
        at = t + 20 / bitrate                                   # error flag + delimiter + intermission
        cable = medium.cable("can")
        timing = cable["timing_error"] if cable else 0.0
        for attempt in range(int(medium.spec.get("max_retries", 8))):
            medium.stats["retransmissions"] += 1
            at += bits / bitrate
            again = dict(original, retry=attempt + 1)
            flipped = self._flip(again, ber, 1 if timing and rng.random() < timing else 0, rng)
            if not flipped:
                return out + [(at, again)]
            out.append((at, {**self._mark(again, {"flipped": flipped, "original": original["data"]}),
                             "error": "bit error: error frame, retransmission"}))
            medium.stats["errors"] += 1
            again["data"] = original["data"]
            at += 20 / bitrate
        medium.stats["abandoned"] += 1                          # the transmitter gives up (error passive / bus off)
        return out

    @staticmethod
    def _mark(payload: Any, notes: Mapping[str, Any]) -> Any:
        if isinstance(payload, dict):
            notes = {k: v for k, v in notes.items() if k != "extra_bits"}
            return {**payload, "medium": notes}
        return payload

    @staticmethod
    def _flip(payload: Any, ber: float, extra: int, rng: random.Random) -> list[int]:
        """Flip bits of the payload in place (hex `data` or a text line); returns the flipped bit indices."""
        if isinstance(payload, dict) and isinstance(payload.get("data"), str):
            data = bytearray(bytes.fromhex(payload["data"]))
        elif isinstance(payload, str):
            return []                                           # text events (UART lines) stay as they are
        else:
            return []
        nbits = len(data) * 8
        if not nbits:
            return []
        count = extra
        if ber > 0:                                             # Binomial(nbits, ber) via inversion for small means
            mean = nbits * min(ber, 1.0)
            if mean > 30:
                count += max(0, int(round(rng.gauss(mean, math.sqrt(mean)))))
            else:
                k, p, cumulative, u = 0, math.exp(-mean), math.exp(-mean), rng.random()
                while u > cumulative and k < nbits:
                    k += 1
                    p *= mean / k
                    cumulative += p
                count += k
        if not count:
            return []
        positions = sorted(rng.sample(range(nbits), min(count, nbits)))
        for bit in positions:
            data[bit // 8] ^= 0x80 >> (bit % 8)
        payload["data"] = data.hex()
        return positions
