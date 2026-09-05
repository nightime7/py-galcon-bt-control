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
        self.program_commands = []
        self.program_error = None
        self.ble_connect_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def _ensure_ble_connected(self, load_programs=False):
        self.ble_connect_calls += 1

    async def _set_zone(self, zone, value):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.001)
        self.commands.append((zone, value))
        self.in_flight -= 1

    async def _disconnect_ble(self):
        pass

    async def _set_program(self, zone, text):
        if self.program_error is not None:
            raise self.program_error
        self.program_commands.append((zone, json.loads(text)))


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
    weekday_update = apply_program_changes(record, {"weekdays": {
        "sunday": False, "monday": False,
    }})
    assert weekday_update[4] == 0x7c
    cyclic_record = record[:4] + b"\x80" + record[5:]
    weekly_update = apply_program_changes(cyclic_record, {"weekdays": {
        "wednesday": True, "saturday": True,
    }})
    assert weekly_update[4] == 0x48
    batch_update = apply_program_changes(record, {"windows": [
        {"window": 1, "hour": 8, "minute": 10},
        {"window": 2, "hour": 14, "minute": 35},
    ]})
    assert batch_update[5:9] == b"\x08\x0a\x0e\x23"
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
            "hour": 12, "minute": 30,
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
        pending_duration = f"test/zone/{zone}/program/pending/duration"
        assert duration["state_topic"] == pending_duration
        assert duration["command_topic"] == f"{pending_duration}/set"
        assert duration["max"] == 600
        assert "command_template" not in duration
        old_duration_topic = (
            f"homeassistant/sensor/test/zone_{zone}_program_duration/config")
        assert any(topic == old_duration_topic and payload == ""
                   for topic, payload, _kwargs in bridge.mqtt.messages)
        commit_topic = (
            f"homeassistant/button/test/zone_{zone}_program_commit/config")
        commit = discovery[commit_topic]
        assert commit["command_topic"] == f"test/zone/{zone}/program/commit"
        discard_topic = (
            f"homeassistant/button/test/zone_{zone}_program_discard/config")
        discard = discovery[discard_topic]
        assert discard["command_topic"] == f"test/zone/{zone}/program/discard"
        for day in ("sunday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday"):
            topic = f"homeassistant/switch/test/zone_{zone}_day_{day}/config"
            config = discovery[topic]
            pending = f"test/zone/{zone}/program/pending/day/{day}"
            assert config["state_topic"] == pending
            assert config["command_topic"] == f"{pending}/set"
        for window in range(1, 5):
            base = f"homeassistant/switch/test/zone_{zone}_window_{window}"
            enabled = discovery[f"{base}_enabled/config"]
            pending_window = (
                f"test/zone/{zone}/program/pending/window/{window}")
            assert enabled["state_topic"] == f"{pending_window}/enabled"
            assert enabled["command_topic"] == f"{pending_window}/enabled/set"
            assert "command_template" not in enabled
            for field, maximum in (("hour", 23), ("minute", 59)):
                topic = (f"homeassistant/number/test/zone_{zone}_window_"
                         f"{window}_{field}/config")
                config = discovery[topic]
                assert config["max"] == maximum
                pending = (f"test/zone/{zone}/program/pending/window/"
                           f"{window}/{field}")
                assert config["state_topic"] == pending
                assert config["command_topic"] == f"{pending}/set"
                assert "command_template" not in config

    before_pending = bridge.ble_connect_calls
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/duration/set", b"125")
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/day/sunday/set", b"OFF")
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/day/wednesday/set", b"ON")
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/window/3/hour/set", b"16")
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/window/3/minute/set", b"45")
    await bridge._handle_mqtt(
        "test/zone/2/program/pending/window/3/enabled/set", b"ON")
    assert bridge.ble_connect_calls == before_pending
    assert bridge.pending_duration_changes[2] == 125
    assert bridge.pending_day_changes[2] == {
        "sunday": False, "wednesday": True,
    }
    assert bridge.pending_window_changes[2] == {
        (3, "hour"): 16, (3, "minute"): 45, (3, "enabled"): True,
    }
    await bridge._handle_mqtt("test/zone/2/program/commit", b"commit")
    assert bridge.ble_connect_calls == before_pending + 1
    assert bridge.program_commands == [(2, {
        "duration": 125,
        "weekdays": {"sunday": False, "wednesday": True},
        "windows": [{
            "window": 3, "enabled": True, "hour": 16, "minute": 45,
        }],
    })]
    assert 2 not in bridge.pending_duration_changes
    assert 2 not in bridge.pending_day_changes
    assert 2 not in bridge.pending_window_changes
    after_commit = bridge.ble_connect_calls
    await bridge._handle_mqtt("test/zone/2/program/commit", b"commit")
    assert bridge.ble_connect_calls == after_commit

    await bridge._handle_mqtt(
        "test/zone/4/program/pending/duration/set", b"30")
    await bridge._handle_mqtt(
        "test/zone/4/program/pending/day/friday/set", b"OFF")
    bridge.program_error = RuntimeError("simulated write failure")
    try:
        await bridge._handle_mqtt("test/zone/4/program/commit", b"commit")
    except RuntimeError as exc:
        assert str(exc) == "simulated write failure"
    else:
        raise AssertionError("failed commit did not raise")
    assert bridge.pending_duration_changes[4] == 30
    assert bridge.pending_day_changes[4] == {"friday": False}

    bridge.program_error = None
    bridge.programs[1] = record
    before_discard = bridge.ble_connect_calls
    await bridge._handle_mqtt(
        "test/zone/1/program/pending/duration/set", b"90")
    await bridge._handle_mqtt(
        "test/zone/1/program/pending/day/sunday/set", b"OFF")
    await bridge._handle_mqtt(
        "test/zone/1/program/pending/window/1/enabled/set", b"OFF")
    await bridge._handle_mqtt(
        "test/zone/1/program/pending/window/1/hour/set", b"12")
    before_discard_messages = len(bridge.mqtt.messages)
    await bridge._handle_mqtt("test/zone/1/program/discard", b"discard")
    assert bridge.ble_connect_calls == before_discard
    assert 1 not in bridge.pending_program_baselines
    assert 1 not in bridge.pending_day_changes
    assert 1 not in bridge.pending_duration_changes
    assert 1 not in bridge.pending_window_changes
    latest = {}
    for topic, payload, _kwargs in bridge.mqtt.messages:
        latest[topic] = payload
    assert latest["test/zone/1/program/pending/duration"] == "15"
    assert latest["test/zone/1/program/pending/day/sunday"] == "ON"
    assert latest["test/zone/1/program/pending/day/saturday"] == "ON"
    assert latest["test/zone/1/program/pending/window/1/enabled"] == "ON"
    assert latest["test/zone/1/program/pending/window/1/hour"] == "6"
    assert latest["test/zone/1/program/pending/window/1/minute"] == "0"
    restored = bridge.mqtt.messages[before_discard_messages:]
    restored_topics = {topic for topic, _payload, _kwargs in restored}
    assert len(restored_topics) == 20
    assert all(topic.startswith("test/zone/1/program/pending/")
               for topic in restored_topics)

    print(f"MQTT command routing passed: {len(expected)} zone commands")
    print("Concurrent MQTT commands serialized: yes")
    print("Indexed program window edits: windows 1, 2, 3, 4")
    print("Home Assistant window controls: 48 discovery entities")
    print("Home Assistant weekday controls: 28 discovery entities")
    print("Home Assistant duration controls: 4 number entities")
    print("Pending duration/day/window edits: staged until zone commit")
    print("Discard pending changes: restores pre-edit values without BLE")
    print("Singles: zones 1, 2, 3, 4")
    print("Pairs: 1+2, 1+3, 1+4, 2+3, 2+4, 3+4")


asyncio.run(main())
