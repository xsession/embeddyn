"""Pin plan (CodeLayer Forge Pin Configurator) -> Renode bindings."""
import pytest
import yaml

from embeddyn.mechatronics.pinplan import merge_bindings, renode_bindings, to_yaml

DEVICE = {"id": "dev", "pins": [
    {"name": "PA0", "capabilities": ["GPIO", "PWM"],
     "renode": {"GPIO": {"object": "sysbus.gpioPortA", "pin": 0}, "PWM": {"object": "sysbus.timer2", "pin": 0}}},
    {"name": "PA1", "capabilities": ["GPIO"], "renode": {"GPIO": {"object": "sysbus.gpioPortA", "pin": 1}}},
    {"name": "PC0", "capabilities": ["ADC"], "renode": {"ADC": {"object": "sysbus.adc1", "channel": 1}}},
    {"name": "PB6", "capabilities": ["I2C_SCL"]},
    {"name": "PB7", "capabilities": ["GPIO"]},
]}


def plan(signals, assignments):
    return {"schemaVersion": 1, "device": "dev", "signals": signals, "assignments": assignments}


def test_capability_defaults_and_cosim_overrides():
    result = renode_bindings(plan(
        [{"id": "motor", "requires": ["PWM"]}, {"id": "led", "requires": ["GPIO"]},
         {"id": "sense", "requires": ["ADC"], "cosim": {"signal": "plant.current_sense", "scale": 1000.0}},
         {"id": "enable", "requires": ["GPIO_IN", "GPIO"], "cosim": {"property": "gpio", "direction": "input"}}],
        {"motor": "PA0", "led": "PA1", "sense": "PC0", "enable": "PA1"}), DEVICE, machine="m")
    by_signal = {b["signal"]: b["endpoint"] for b in result["bindings"]}
    assert by_signal["mcu.motor"] == {"object": "sysbus.timer2", "property": "gpio_duty", "pin": 0, "direction": "output"}
    assert by_signal["mcu.led"]["property"] == "gpio_state"
    assert by_signal["plant.current_sense"] == {"object": "sysbus.adc1", "property": "adc", "channel": 1, "scale": 1000.0,
                                               "direction": "input", "unit": "V"}
    assert by_signal["mcu.enable"]["direction"] == "input"
    assert result["machine"] == "m" and result["diagnostics"] == []


def test_diagnostics_for_unmappable_signals():
    result = renode_bindings(plan(
        [{"id": "scl", "requires": ["I2C_SCL"]}, {"id": "x", "requires": ["GPIO"]}, {"id": "y", "requires": ["GPIO"]},
         {"id": "z", "requires": ["GPIO"]}],
        {"scl": "PB6", "x": "PB7", "z": "P99"}), DEVICE)
    codes = {d["signal"]: d["code"] for d in result["diagnostics"]}
    assert codes == {"scl": "bus-not-bindable", "x": "no-renode-mapping", "y": "unassigned", "z": "unknown-pin"}
    assert result["bindings"] == []


def test_wrong_device_and_merge_keeps_sysbus_entries():
    with pytest.raises(ValueError, match="for device other"):
        renode_bindings({"schemaVersion": 1, "device": "other", "signals": [], "assignments": {}}, DEVICE)
    generated = renode_bindings(plan([{"id": "led", "requires": ["GPIO"]}], {"led": "PA1"}), DEVICE)
    existing = {"machine": "old", "bindings": [
        {"signal": "mcu.led", "endpoint": {"object": "stale"}},
        {"signal": "mcu.telemetry", "endpoint": {"object": "sysbus", "property": "sysbus", "address": 4}}]}
    merged = merge_bindings(generated, existing)
    assert [b["signal"] for b in merged["bindings"]] == ["mcu.led", "mcu.telemetry"]
    assert merged["bindings"][0]["endpoint"]["object"] == "sysbus.gpioPortA" and merged["machine"] == "old"
    assert yaml.safe_load(to_yaml(merged))["bindings"][1]["signal"] == "mcu.telemetry"
