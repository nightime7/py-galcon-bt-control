"""Isolated GL6100 enrollment-flow probe.

This deliberately sends only the application-level command observed immediately
before the official app wrote its four-digit PIN during enrollment:
02 00 -> 20900106, then a raw-digit PIN -> 20900105.

It does not send the normal 01 02 wake command, alter schedules, or control a
valve. Put the controller in its unpaired/enrollment state before running.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from bleak import BleakClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_galcon import CHAR_PIN, CHAR_POLL, STATUS_POLL  # noqa: E402
from control_galcon import find_device, hexdump, load_saved_mac, pin_to_bytes, ts  # noqa: E402


async def run(args):
    device = await find_device(args.scan_time, mac=args.mac)
    if device is None:
        print("Controller not found. Put it in enrollment mode and try again.")
        return 1

    async with BleakClient(device, timeout=30.0) as client:
        print(f"[{ts()}] Connected without sending the wake command.")
        print(f"[{ts()}] Sending observed enrollment probe to 20900106: "
              f"{hexdump(STATUS_POLL)}")
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)

        print("\nWatch the controller display for a four-digit PIN.")
        try:
            pin = await asyncio.wait_for(
                asyncio.to_thread(input, "Enter the displayed PIN: "),
                timeout=args.pin_timeout)
        except asyncio.TimeoutError:
            print(f"No PIN entered within {args.pin_timeout:.0f} seconds.")
            return 1

        pin = pin.strip()
        if not (pin.isdigit() and len(pin) == 4):
            print("PIN must be exactly four digits.")
            return 2

        payload = pin_to_bytes(pin)
        print(f"[{ts()}] Sending PIN to 20900105: {hexdump(payload)}")
        await client.write_gatt_char(CHAR_PIN, payload, response=True)
        print(f"[{ts()}] PIN submitted. Leave the app/controller state unchanged "
              "until the enrollment result is known.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Probe the GL6100 application-level enrollment flow")
    parser.add_argument("--scan-time", type=float, default=60.0)
    parser.add_argument("--pin-timeout", type=float, default=30.0,
                        help="Seconds to wait for the displayed PIN")
    parser.add_argument("--mac", default=None,
                        help="Optional MAC to disambiguate scans; defaults "
                             "to the value saved via control_galcon.py "
                             "--set-mac, if any")
    args = parser.parse_args()
    if not args.mac:
        args.mac = load_saved_mac()
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
