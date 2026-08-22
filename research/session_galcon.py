"""
Galcon GL6100 session observer.

Purpose
-------
Reads are unreliable on this device: 20900102/20900105 sometimes return real
data and sometimes all zeros, while the notify on 20900101 consistently
carries a populated frame. This tool separates timing effects from protocol
effects by:

  * connecting once and HOLDING the link open,
  * subscribing to both notify characteristics,
  * polling every vendor characteristic on a timer,
  * printing only CHANGES, with timestamps,
    * optionally re-sending the harmless status-poll wake periodically.

That tells us whether the registers populate after a delay, only after a
wake, only once per connection, or never.

This tool is READ-ONLY apart from the status poll 02 00 on 20900106. Do not
write 01 02 to 20900101 as a wake on GL6100: that characteristic is also the
schedule-save pipe, and 01 02 can be interpreted as zone 1 duration = 2h00m.

Usage
-----
    python session_galcon.py --seconds 90
    python session_galcon.py --seconds 90 --wake-every 10
    python session_galcon.py --seconds 90 --no-wake
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

NAME_HINT = "gl6100"

BASE = "-bdee-493a-aa74-a8137c9d43f0"
CHARS = {
    "20900101": "20900101" + BASE,
    "20900102": "20900102" + BASE,
    "20900103": "20900103" + BASE,
    "20900104": "20900104" + BASE,
    "20900105": "20900105" + BASE,
    "20900106": "20900106" + BASE,
}
CH_POLL = CHARS["20900106"]
WAKE = bytes([0x02, 0x00])


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(data) -> str:
    if data is None:
        return "<unreadable>"
    data = bytes(data)
    if not data:
        return "<empty>"
    h = " ".join(f"{b:02x}" for b in data)
    a = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{h}  |{a}|"


async def find_device(scan_time: float):
    print(f"[{ts()}] Scanning up to {scan_time:.0f}s...")
    found = asyncio.Event()
    holder = {}

    def cb(device, adv):
        name = device.name or adv.local_name or ""
        if NAME_HINT in name.lower():
            holder["dev"] = device
            holder["rssi"] = adv.rssi
            found.set()

    scanner = BleakScanner(cb)
    await scanner.start()
    try:
        await asyncio.wait_for(found.wait(), timeout=scan_time)
    except asyncio.TimeoutError:
        pass
    finally:
        await scanner.stop()

    if "dev" not in holder:
        return None
    print(f"[{ts()}] Found {holder['dev'].name} at {holder['rssi']} dBm")
    return holder["dev"]


async def observe(device, seconds, wake_every, do_wake, interval):
    disconnected = asyncio.Event()
    previous = {}
    notify_count = 0

    def on_disconnect(_c):
        print(f"\n[{ts()}] *** peer disconnected ***")
        disconnected.set()

    def on_notify(sender, data):
        nonlocal notify_count
        notify_count += 1
        handle = getattr(sender, "handle", sender)
        print(f"[{ts()}] NOTIFY handle=0x{handle:04x}: {hexdump(bytes(data))}")

    async with BleakClient(device, timeout=30.0,
                           disconnected_callback=on_disconnect) as client:
        print(f"[{ts()}] Connected. Holding link for {seconds:.0f}s.\n")

        for key in ("20900101", "20900102"):
            try:
                await client.start_notify(CHARS[key], on_notify)
                print(f"[{ts()}] subscribed {key}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{ts()}] subscribe {key} failed: {exc}")

        loop = asyncio.get_running_loop()
        start = loop.time()
        last_wake = -999.0

        while not disconnected.is_set():
            elapsed = loop.time() - start
            if elapsed >= seconds:
                break

            if do_wake and (elapsed - last_wake) >= wake_every:
                last_wake = elapsed
                try:
                    await client.write_gatt_char(CH_POLL, WAKE, response=True)
                    print(f"[{ts()}] --> wake {hexdump(WAKE)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[{ts()}] wake failed: {exc}")

            for key, uuid in CHARS.items():
                try:
                    value = bytes(await client.read_gatt_char(uuid))
                except Exception:  # noqa: BLE001
                    value = None
                if previous.get(key, "unset") != value:
                    marker = "  <-- CHANGED" if key in previous else ""
                    print(f"[{ts()}] {key} = {hexdump(value)}{marker}")
                    previous[key] = value

            await asyncio.sleep(interval)

        print(f"\n[{ts()}] Observation window ended. "
              f"{notify_count} notification(s) seen.")

    print("\n" + "=" * 70)
    print("FINAL VALUES")
    print("=" * 70)
    for key in CHARS:
        print(f"  {key} = {hexdump(previous.get(key))}")
    nonzero = [k for k, v in previous.items() if v and any(v)]
    print(f"\nCharacteristics that ever held non-zero data: "
          f"{', '.join(nonzero) if nonzero else 'NONE'}")


async def main():
    parser = argparse.ArgumentParser(description="Observe a Galcon GL6100 session")
    parser.add_argument("--seconds", type=float, default=90.0,
                        help="How long to hold the link open")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between poll rounds")
    parser.add_argument("--wake-every", type=float, default=15.0,
                        help="Re-send the wake frame this often")
    parser.add_argument("--no-wake", action="store_true",
                        help="Never send the wake frame; pure passive read")
    parser.add_argument("--scan-time", type=float, default=60.0)
    args = parser.parse_args()

    device = await find_device(args.scan_time)
    if device is None:
        print("Valve not found.")
        return 1

    try:
        await observe(device, args.seconds, args.wake_every,
                      not args.no_wake, args.interval)
    except Exception as exc:  # noqa: BLE001
        print(f"\nSession error: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
