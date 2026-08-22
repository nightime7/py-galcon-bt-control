"""Replay the observable GATT timing around the GL6100 enrollment capture.

This reproduces only the confirmed wireless sequence. It cannot reproduce the
app's local Unpair action or force the controller into enrollment state.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from bleak import BleakClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_galcon import CHAR_PIN, CHAR_POLL, CHAR_STATUS, STATUS_POLL  # noqa: E402
from control_galcon import find_device, hexdump, pin_to_bytes, ts  # noqa: E402


async def warmup_session(scan_time):
    device = await find_device(scan_time)
    if device is None:
        return False

    async with BleakClient(device, timeout=30.0) as client:
        print(f"[{ts()}] Warmup connection established.")
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        print(f"[{ts()}] Warmup poll 1: {hexdump(STATUS_POLL)}")
        await client.read_gatt_char(CHAR_STATUS)
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        print(f"[{ts()}] Warmup poll 2: {hexdump(STATUS_POLL)}")
    print(f"[{ts()}] Warmup connection closed; scanning for the next advertisement.")
    return True


async def enrollment_session(scan_time, pin_timeout):
    device = await find_device(scan_time)
    if device is None:
        return False

    async with BleakClient(device, timeout=30.0) as client:
        print(f"[{ts()}] Enrollment connection established.")
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        print(f"[{ts()}] Enrollment poll: {hexdump(STATUS_POLL)}")
        print("Watch the controller display for a four-digit PIN.")
        try:
            pin = await asyncio.wait_for(
                asyncio.to_thread(input, "Enter the displayed PIN: "),
                timeout=pin_timeout)
        except asyncio.TimeoutError:
            print(f"No PIN entered within {pin_timeout:.0f} seconds.")
            return False

        pin = pin.strip()
        if not (pin.isdigit() and len(pin) == 4):
            print("PIN must be exactly four digits.")
            return False

        await client.write_gatt_char(CHAR_PIN, pin_to_bytes(pin), response=True)
        print(f"[{ts()}] Enrollment PIN submitted.")
    return True


async def main():
    parser = argparse.ArgumentParser(
        description="Replay the observable GL6100 enrollment timing")
    parser.add_argument("--scan-time", type=float, default=60.0)
    parser.add_argument("--pin-timeout", type=float, default=30.0)
    args = parser.parse_args()

    if not await warmup_session(args.scan_time):
        print("Warmup controller connection failed.")
        return 1
    if not await enrollment_session(args.scan_time, args.pin_timeout):
        print("Enrollment replay did not complete.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
