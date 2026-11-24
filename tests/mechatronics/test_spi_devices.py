"""SPI sensor models (spi_devices) and their wiring into the Renode adapter, client and Lab, without Renode."""
import math
import socket
import struct
import threading
from pathlib import Path

import pytest

from embeddyn.mechatronics.adapters.renode import RenodeAdapter
from embeddyn.mechatronics.adapters.renode_api_v2 import Command, RenodeMessageClient, Type
from embeddyn.mechatronics.gui.document import load_graph
from embeddyn.mechatronics.spi_devices import AS5047P, SpiDevice, create_device

ROOT = Path(__file__).resolve().parents[2]


def word(device, command):
    """One 16-bit frame, MSB first; returns the 16-bit answer."""
    return (device.transmit(command >> 8) << 8) | device.transmit(command & 0xFF)


def with_parity(word15):
    return ((bin(word15 & 0x7FFF).count("1") & 1) << 15) | (word15 & 0x7FFF)


def parity_ok(answer):
    return bin(answer).count("1") % 2 == 0


READ_ANGLE = 0xFFFF


def test_as5047p_answers_one_frame_late_with_even_parity():
    sensor = AS5047P()
    sensor.set_inputs({"angle": math.pi / 2})
    assert word(sensor, READ_ANGLE) == 0                 # power-up: nothing latched yet
    sensor.set_inputs({"angle": math.pi})                # moves after the command was latched
    answer = word(sensor, READ_ANGLE)
    assert parity_ok(answer) and not answer & 0x4000 and answer & 0x3FFF == 4096   # 90 deg of 16384
    assert word(sensor, READ_ANGLE) & 0x3FFF == 8192
    assert sensor.output_values() == {"frames": 3.0, "errors": 0.0}


def test_as5047p_wraps_and_applies_direction_and_zero():
    sensor = AS5047P(direction=-1, zero=0.25)
    sensor.set_inputs({"angle": 0.5})
    word(sensor, READ_ANGLE)
    counts = word(sensor, READ_ANGLE) & 0x3FFF
    assert counts == round(((-0.5 - 0.25) / (2 * math.pi)) * 16384) % 16384


def test_as5047p_zero_position_register_write():
    sensor = AS5047P()
    sensor.set_inputs({"angle": 0.0})
    word(sensor, with_parity(AS5047P.ZPOSM))             # write command (R/W = 0) ...
    word(sensor, with_parity(0x0010))                    # ... then the data frame: ZPOSM = 0x10
    word(sensor, READ_ANGLE)
    assert word(sensor, READ_ANGLE) & 0x3FFF == (-(0x10 << 6)) & 0x3FFF


def test_as5047p_error_flag_parity_and_errfl():
    sensor = AS5047P()
    word(sensor, READ_ANGLE ^ 0x0001)                    # parity error
    answer = word(sensor, with_parity(0x4000 | AS5047P.ERRFL))
    assert answer & 0x4000                                # EF reported in the next frame
    errfl = word(sensor, READ_ANGLE) & 0x3FFF
    assert errfl & AS5047P.PARERR
    word(sensor, with_parity(0x4000 | AS5047P.ERRFL))
    assert word(sensor, READ_ANGLE) & 0x3FFF == 0         # cleared by the read
    word(sensor, with_parity(0x4000 | 0x1234))           # no such register
    assert word(sensor, READ_ANGLE) & 0x4000
    assert sensor.errors == 2


def test_as5047p_resynchronises_on_chip_select_release():
    sensor = AS5047P()
    sensor.set_inputs({"angle": math.pi / 2})
    sensor.transmit(0xFF)
    sensor.finish()                                       # CS released after one byte
    word(sensor, READ_ANGLE)
    assert word(sensor, READ_ANGLE) & 0x3FFF == 4096      # frames line up again


def test_as5047p_magnet_diagnostics():
    sensor = AS5047P()
    sensor.set_inputs({"magnitude": 0})
    word(sensor, with_parity(0x4000 | AS5047P.DIAAGC))
    assert word(sensor, READ_ANGLE) & (1 << 10)           # MAGL: magnet too weak / missing


def test_create_device_and_validation():
    assert isinstance(create_device("AS5047P", {"direction": -1}), AS5047P)
    assert isinstance(create_device("embeddyn.mechatronics.spi_devices:AS5047P"), SpiDevice)
    with pytest.raises(ValueError, match="unknown SPI device"):
        create_device("bme280")
    with pytest.raises(KeyError, match="no input 'speed'"):
        AS5047P().set_inputs({"speed": 1.0})


def test_adapter_checks_spi_options_before_launching():
    adapter = RenodeAdapter(spi=[{"peripheral": "sysbus.spi1.enc", "device": "as5047p",
                                  "inputs": {"angle": {"signal": "arm.angle", "scale": -1}},
                                  "outputs": {"errors": "mcu.enc_errors"}}])
    assert adapter.spi[0]["inputs"]["angle"] == {"signal": "arm.angle", "scale": -1.0, "offset": 0.0}
    with pytest.raises(ValueError, match="no input 'speed'"):
        RenodeAdapter(spi=[{"peripheral": "sysbus.spi1.enc", "device": "as5047p", "inputs": {"speed": "x"}}])
    with pytest.raises(ValueError, match="no output"):
        RenodeAdapter(spi=[{"peripheral": "sysbus.spi1.enc", "device": "as5047p", "outputs": {"angle": "x"}}])
    with pytest.raises(ValueError, match="peripheral"):
        RenodeAdapter(spi=[{"peripheral": "sysbus.spi1 enc; quit", "device": "as5047p"}])


def test_lab_shows_spi_device_ports():
    graph = load_graph(ROOT / "examples/mechatronics/mujoco-arm-firmware.yaml")
    mcu = next(node for node in graph["nodes"] if node["id"] == "mcu")
    ports = {port["name"]: port for port in mcu["ports"]}
    assert ports["spi:abs_encoder.angle"]["direction"] == "in" and ports["spi:abs_encoder.angle"]["signal"] == "arm.angle"
    assert ports["spi:abs_encoder.errors"]["signal"] == "mcu.abs_errors"


def _read(sock):
    message_id, size = struct.unpack("<Hi", sock.recv(6, socket.MSG_WAITALL))
    payload = sock.recv(size, socket.MSG_WAITALL)
    command, kind = struct.unpack_from("<HB", payload)
    return message_id, command, kind, payload[3:]


def _send(sock, message_id, command, kind, data=b""):
    payload = struct.pack("<HB", command, kind) + data
    sock.sendall(struct.pack("<Hi", message_id, len(payload)) + payload)


def test_client_answers_spi_byte_events():
    """During RunFor Renode sends one event per byte (Transmit, MOSI) and FinishTransmission;
    the client answers each Transmit with the device's MISO byte."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    log = []

    def serve():
        conn, _ = listener.accept()
        with conn:
            while True:
                try:
                    message_id, command, kind, data = _read(conn)
                except (struct.error, OSError):
                    return
                log.append((command, data))
                if command == Command.SPI and struct.unpack_from("<i", data)[0] == -1:
                    _send(conn, message_id, command, Type.SUCCESS, struct.pack("<i", 4))
                elif command == Command.RUN_FOR:
                    ed = struct.unpack_from("<i", log[-2][1], 4)[0]          # from the registration
                    for request_id, event in enumerate((bytes([0, 0xFF]), bytes([0, 0xFF]), bytes([1]),
                                                        bytes([0, 0xFF]), bytes([0, 0xFF]))):
                        _send(conn, 0x8100 + request_id, Command.SPI, Type.EVENT_REQUEST, struct.pack("<i", ed) + event)
                        reply_id, _, reply_kind, reply = _read(conn)
                        log.append(("reply", reply_id, reply_kind, reply))
                    _send(conn, message_id, command, Type.SUCCESS)
                else:
                    _send(conn, message_id, command, Type.SUCCESS, struct.pack("<i", 0))

    threading.Thread(target=serve, daemon=True).start()
    client = RenodeMessageClient.connect("127.0.0.1", listener.getsockname()[1], timeout=5)
    sensor = AS5047P()
    sensor.set_inputs({"angle": math.pi / 2})
    client.spi_register("m", "sysbus.spi1.enc", sensor.transmit, sensor.finish)
    registration = next(data for command, data in log if command == Command.SPI and struct.unpack_from("<i", data)[0] == 4)
    assert len(registration) == 8                                    # instance id + event descriptor
    client.run_for(1)
    replies = [entry for entry in log if entry[0] == "reply"]
    assert [entry[1] for entry in replies] == [0x8100, 0x8101, 0x8102, 0x8103, 0x8104]
    assert all(entry[2] == Type.SUCCESS for entry in replies)
    assert replies[2][3] == b""                                      # FinishTransmission: no data
    answer = (replies[3][3][0] << 8) | replies[4][3][0]
    assert answer & 0x3FFF == 4096 and parity_ok(answer)
    client.close()
    listener.close()
