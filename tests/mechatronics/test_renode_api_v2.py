"""Renode >= 1.17 External Control message protocol: client framing against a scripted fake server."""
import socket
import struct
import threading

import pytest

from embeddyn.mechatronics.adapters.renode_api import RenodeApiError
from embeddyn.mechatronics.adapters.renode_api_v2 import Command, RenodeMessageClient, Type


def _read(sock):
    header = sock.recv(6, socket.MSG_WAITALL)
    message_id, size = struct.unpack("<Hi", header)
    payload = sock.recv(size, socket.MSG_WAITALL)
    command, kind = struct.unpack_from("<HB", payload)
    return message_id, command, kind, payload[3:]


def _send(sock, message_id, command, kind, data=b""):
    payload = struct.pack("<HB", command, kind) + data
    sock.sendall(struct.pack("<Hi", message_id, len(payload)) + payload)


@pytest.fixture
def server():
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
                log.append((message_id, command, kind, data))
                if command == Command.CHECK_VERSION:
                    _send(conn, message_id, command, Type.SUCCESS)
                elif command == Command.GET_MACHINE:
                    _send(conn, message_id, command, Type.SUCCESS, struct.pack("<i", 0))
                elif command == Command.GPIO and struct.unpack_from("<i", data)[0] == -1:   # instance lookup
                    _send(conn, message_id, command, Type.SUCCESS, struct.pack("<i", 7))
                elif command == Command.GPIO and data[4] == 2:                             # register event
                    _send(conn, message_id, command, Type.SUCCESS)
                elif command == Command.RUN_FOR:
                    # Renode sends its own event request (id with bit 15 set, like the real server)
                    # before answering: a GPIO edge at t = 1.5 us, state high.
                    _send(conn, 0x8001, Command.GPIO, Type.EVENT_REQUEST, struct.pack("<iQB", 0, 1500, 1))
                    event_reply = _read(conn)
                    log.append(("event-reply",) + event_reply)
                    _send(conn, message_id, command, Type.SUCCESS)
                elif command == Command.GET_TIME:
                    _send(conn, message_id, command, Type.SUCCESS, struct.pack("<Q", 2_000_000))
                else:
                    _send(conn, message_id, command, Type.ERROR, b"unsupported in fake")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], log
    listener.close()


def test_framing_events_and_errors(server):
    port, log = server
    client = RenodeMessageClient.connect("127.0.0.1", port, timeout=5)
    assert log[0][:3] == (0x8000, Command.CHECK_VERSION, Type.REQUEST) and log[0][3] == struct.pack("<I", 0)
    events = []
    client.gpio_subscribe("m", "sysbus.gpioPortA", 5, events.append)
    client.run_for(10)                                       # microseconds -> 10_000 ns on the wire
    run_for = next(entry for entry in log if entry[1] == Command.RUN_FOR)
    assert struct.unpack("<Q", run_for[3])[0] == 10_000 and run_for[0] & 0x8000
    assert len(events) == 1 and events[0].timestamp_us == 1.5 and events[0].state is True
    reply = next(entry for entry in log if entry[0] == "event-reply")
    assert reply[1:4] == (0x8001, Command.GPIO, Type.SUCCESS)   # echoes the event's id
    assert client.get_time_us() == 2000
    with pytest.raises(RenodeApiError, match="unsupported in fake"):
        client.adc_get("m", "sysbus.adc1", 1)
    client.close()
