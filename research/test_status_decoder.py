import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_galcon import (  # noqa: E402
    decode_active_zones,
    decode_battery_status,
    decode_device_status_details,
)


SAMPLES = {
    0xF0: (1,),
    0xF1: (2,),
    0xF2: (3,),
    0xF3: (4,),
    0x01: (1, 2),
    0x10: (2, 1),
    0x20: (3, 1),
    0x02: (1, 3),
    0x21: (3, 2),
    0x12: (2, 3),
    0x30: (4, 1),
    0x03: (1, 4),
    0x31: (4, 2),
    0x13: (2, 4),
    0x32: (4, 3),
    0x23: (3, 4),
    0x0F: (1,),
    0x1F: (2,),
    0x2F: (3,),
    0x3F: (4,),
    0x22: (3,),
}


for status_byte, expected_zones in SAMPLES.items():
    frame = bytes([status_byte, 0, 0, 11, 0, 0, 22]) + bytes(13)
    decoded = decode_active_zones(frame)
    actual_zones = tuple(zone for zone, _remaining in decoded)
    assert actual_zones == expected_zones, (status_byte, actual_zones)

live_status = bytes.fromhex(
    "ff0000000000000000085a0e001b2fb71e000027")
assert decode_battery_status(live_status) == (3674, 9, 3123, False)
assert decode_device_status_details(live_status) == {
    "sensor_wet": False,
    "master_open": False,
    "fertilizer": False,
    "ac_present": True,
    "rainoff_days": 0,
    "valve_count": 4,
    "supply_type": "DC",
    "firmware_version": 47,
    "signal_strength": -73,
}

low_9v = bytearray(20)
low_9v[10:12] = (3123).to_bytes(2, "little")
assert decode_battery_status(low_9v) == (3123, 9, 3123, True)

healthy_18v = bytearray(20)
healthy_18v[1] = 0x40
healthy_18v[10:12] = (2948).to_bytes(2, "little")
assert decode_battery_status(healthy_18v) == (2948, 18, 2947, False)

print("status decoder: zones, battery, and device details passed")
