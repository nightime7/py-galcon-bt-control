"""Offline MQTT command-contract test for all zone singles and pairs.

This does not connect to MQTT or BLE and does not operate a controller. It
verifies that every zone command topic is accepted, routed to the correct
zone, and serialized by the bridge handler.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from galcon_mqtt import GalconMqttBridge  # noqa: E402


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
    print(f"MQTT command routing passed: {len(expected)} zone commands")
    print("Concurrent MQTT commands serialized: yes")
    print("Singles: zones 1, 2, 3, 4")
    print("Pairs: 1+2, 1+3, 1+4, 2+3, 2+4, 3+4")


asyncio.run(main())
