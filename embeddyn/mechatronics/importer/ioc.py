"""Read an STM32CubeMX project (``.ioc``): the MCU, the pin signals and labels, the enabled peripherals, the DMA
requests, the clock tree and the middlewares (USB device class, FreeRTOS)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IocPin:
    name: str            # "PA5"
    signal: str = ""     # "SPI1_SCK", "GPIO_Output", "GPXTI2", "S_TIM3_CH1"
    label: str = ""      # GPIO_Label
    mode: str = ""

    @property
    def port(self) -> str:
        return self.name[1] if len(self.name) > 1 else ""

    @property
    def number(self) -> int:
        m = re.match(r"P[A-K](\d+)", self.name)
        return int(m.group(1)) if m else -1


@dataclass
class IocProject:
    path: Path
    mcu: str = ""                    # "STM32F405RGTx"
    mcu_family: str = ""             # "STM32F4"
    pins: dict[str, IocPin] = field(default_factory=dict)
    ips: list[str] = field(default_factory=list)          # enabled IP instances: SPI1, TIM3, USB_OTG_FS, ...
    dma: list[dict[str, str]] = field(default_factory=list)
    clocks: dict[str, str] = field(default_factory=dict)  # RCC.* values (SYSCLKFreq_VALUE, HSE_VALUE, ...)
    middlewares: list[str] = field(default_factory=list)  # USB_DEVICE CDC_FS, FREERTOS, ...
    raw: dict[str, str] = field(default_factory=dict)

    def pin_by_label(self, label: str) -> IocPin | None:
        return next((p for p in self.pins.values() if p.label == label), None)

    def signal_of(self, pin: str) -> str:
        entry = self.pins.get(pin)
        return entry.signal if entry else ""

    @property
    def hse_frequency(self) -> int | None:
        value = self.clocks.get("HSE_VALUE")
        return int(value) if value and value.isdigit() else None

    @property
    def sysclk(self) -> int | None:
        value = self.clocks.get("SYSCLKFreq_VALUE")
        return int(value) if value and value.isdigit() else None


def read_ioc(path: Path) -> IocProject:
    raw: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        raw[key.strip()] = value.strip().replace("\\:", ":").replace("\\#", "#")
    project = IocProject(path=path, raw=raw)
    project.mcu = raw.get("Mcu.UserName") or raw.get("Mcu.Name", "")
    project.mcu_family = raw.get("Mcu.Family", project.mcu[:7])
    pin_names = [v for k, v in raw.items() if re.match(r"Mcu\.Pin\d+$", k)]
    for name in pin_names:
        base = name.split("-")[0].split(" ")[0]      # "PH0-OSC_IN" -> "PH0"
        pin = IocPin(name=base, signal=raw.get(f"{name}.Signal", ""), label=raw.get(f"{name}.GPIO_Label", ""),
                     mode=raw.get(f"{name}.Mode", ""))
        project.pins[base] = pin
    project.ips = [v for k, v in raw.items() if re.match(r"Mcu\.IP\d+$", k)]
    for k, v in raw.items():
        m = re.match(r"Dma\.Request(\d+)$", k)
        if m:
            request = v
            entry = {"request": request}
            prefix = f"Dma.{request}."
            for key, value in raw.items():
                if key.startswith(prefix):
                    entry[key.split(".")[-1]] = value
            project.dma.append(entry)
    project.clocks = {k[4:]: v for k, v in raw.items() if k.startswith("RCC.")}
    if any(k.startswith("USB_DEVICE.") for k in raw) or "USB_DEVICE" in project.ips:
        classes = {v for k, v in raw.items() if k.startswith("USB_DEVICE.") and "CLASS_NAME" in k}
        project.middlewares.append("USB_DEVICE " + ",".join(sorted(classes)) if classes else "USB_DEVICE")
    if "FREERTOS" in project.ips or any(k.startswith("FREERTOS.") for k in raw):
        project.middlewares.append("FREERTOS")
    return project


def find_ioc(root: Path) -> list[Path]:
    return sorted(root.rglob("*.ioc"))
