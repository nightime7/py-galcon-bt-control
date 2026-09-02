"""Offline MQTT command-contract test for all zone singles and pairs.

This does not connect to MQTT or BLE and does not operate a controller. It
verifies that every zone command topic is accepted, routed to the correct
zone, and serialized by the bridge handler.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from galcon_mqtt import GalconMqttBridge, apply_program_changes  # noqa: E402


class FakeBridge(GalconMqttBridge):
    def __init__(self, args):
        super().__init__(args)
        self.commands = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def _ensure_ble_connected(self, load_programs=False):
        pass

    async def _set_zone(self, zone, value):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.001)
        self.commands.append((zone, value))
        self.in_flight -= 1

    async def _disconnect_ble(self):
        pass


class FakeMqtt:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload, kwargs))


async def main():
    args = argparse.Namespace(prefix="test", keep_connected=False,
                              idle_grace=120)
    bridge = FakeBridge(args)
    expected = []

    for zone in range(1, 5):
        topic = f"test/zone/{zone}/set"
        await bridge._handle_mqtt(topic, b"OPEN:1")
        expected.append((zone, "OPEN:1"))

    for first in range(1, 5):
        for second in range(first + 1, 5):
            for zone in (first, second):
                topic = f"test/zone/{zone}/set"
                await bridge._handle_mqtt(topic, b"OPEN:1")
                expected.append((zone, "OPEN:1"))

    assert bridge.commands == expected
    bridge.commands.clear()
    await asyncio.gather(
        bridge._handle_mqtt("test/zone/1/set", b"OPEN:1"),
        bridge._handle_mqtt("test/zone/2/set", b"OPEN:1"),
    )
    assert bridge.max_in_flight == 1

    record = bytes.fromhex(
        "01000f007f06001e00ff00ff0080000000000000")
    duration_update = apply_program_changes(record, {"duration": 125})
    assert duration_update[1:3] == b"\x02\x05"
    positions = (5, 7, 9, 11)
    for window, position in enumerate(positions, 1):
        updated = apply_program_changes(record, {
            "window": window, "enabled": True,
            "hour": window + 10, "minute": window * 5,
        })
        assert updated[position:position + 2] == bytes(
            (window + 10, window * 5))
        for other_position in positions:
            if other_position != position:
                assert updated[other_position:other_position + 2] == \
                    record[other_position:other_position + 2]
        disabled = apply_program_changes(updated, {
            "window": window, "enabled": False,
        })
        assert disabled[position:position + 2] == b"\xff\x00"

    bridge.mqtt = FakeMqtt()
    bridge._publish_discovery()
    discovery = {
        topic: json.loads(payload)
        for topic, payload, _kwargs in bridge.mqtt.messages
        if topic.endswith("/config") and payload
    }
    for zone in range(1, 5):
        duration_topic = (
            f"homeassistant/number/test/zone_{zone}_program_duration/config")
        duration = discovery[duration_topic]
        assert duration["command_topic"] == f"test/zone/{zone}/program/set"
        assert duration["max"] == 600
        assert '"duration"' in duration["command_template"]
        old_duration_topic = (
            f"homeassistant/sensor/test/zone_{zone}_program_duration/config")
        assert any(topic == old_duration_topic and payload == ""
                   for topic, payload, _kwargs in bridge.mqtt.messages)
        for window in range(1, 5):
            base = f"homeassistant/switch/test/zone_{zone}_window_{window}"
            enabled = discovery[f"{base}_enabled/config"]
            assert enabled["command_topic"] == \
                f"test/zone/{zone}/program/set"
            assert f'"window": {window}' in enabled["command_template"]
            for field, maximum in (("hour", 23), ("minute", 59)):
                topic = (f"homeassistant/number/test/zone_{zone}_window_"
                         f"{window}_{field}/config")
                config = discovery[topic]
                assert config["max"] == maximum
                assert f'"window": {window}' in config["command_template"]

    print(f"MQTT command routing passed: {len(expected)} zone commands")
    print("Concurrent MQTT commands serialized: yes")
    print("Indexed program window edits: windows 1, 2, 3, 4")
    print("Home Assistant window controls: 48 discovery entities")
    print("Home Assistant duration controls: 4 number entities")
    print("Singles: zones 1, 2, 3, 4")
    print("Pairs: 1+2, 1+3, 1+4, 2+3, 2+4, 3+4")


asyncio.run(main())
