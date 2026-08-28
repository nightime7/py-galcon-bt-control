import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_galcon import decode_active_zones  # noqa: E402


SAMPLES = {
    0xF0: (1,),
    0xF1: (2,),
    0xF2: (3,),
    0xF3: (4,),
    0x01: (1, 2),
    0x20: (1, 3),
    0x21: (2, 3),
    0x30: (1, 4),
    0x31: (2, 4),
    0x32: (3, 4),
}


for status_byte, expected_zones in SAMPLES.items():
    frame = bytes([status_byte, 0, 0, 11, 0, 0, 22]) + bytes(13)
    decoded = decode_active_zones(frame)
    actual_zones = tuple(zone for zone, _remaining in decoded)
    assert actual_zones == expected_zones, (status_byte, actual_zones)

print("status decoder: all tested zone combinations passed")
