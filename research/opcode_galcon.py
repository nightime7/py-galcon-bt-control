"""
Galcon GL6100 opcode discovery.

What we know
------------
20900101 (handle 0x000d) is the only live characteristic on this model.
20900102..20900106 stay all-zero across a full session and appear unused.

The framing observed is:

    request   01 <opcode> [args...]
    response  00 <opcode> [payload...]      (delivered as a NOTIFY on 0x000d,
                                             and also latched into the
                                             characteristic value)

Known opcode:
    0x02 -> 00 02 00 00 7f 06 00 0f 00 ff 00 ff 00 80 00 00 00 00 00 00
            Stable and repeatable, so it is a side-effect-free info query.

This tool sweeps opcodes, records which ones answer, and diffs the replies.

!! SAFETY !!
------------
These opcodes are UNDOCUMENTED. On an irrigation controller an unknown
opcode could plausibly open a valve, wipe the schedule, or factory-reset the
unit. This script therefore:

  * sends only 2-byte frames (01 <opcode>) with no arguments, which are far
    less likely to carry a destructive parameter than a full frame,
  * skips a configurable blocklist,
  * requires --yes (or an interactive confirmation) before sending anything,
  * supports --dry-run to preview the exact frames first.

Watch and listen to the valve while this runs. Ctrl+C stops immediately.

Usage
-----
    python opcode_galcon.py --dry-run
    python opcode_galcon.py --start 0x00 --end 0x0f
    python opcode_galcon.py --start 0x00 --end 0x1f --yes
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

NAME_HINT = "gl6100"
CH_CMD = "20900101-bdee-493a-aa74-a8137c9d43f0"

KNOWN = {
    0x02: "info/status query - stable, no side effects observed",
}

# Opcodes to skip by default. Values commonly used for erase/reset in
# embedded command sets. Purely precautionary, not based on evidence.
DEFAULT_BLOCKLIST = {0xEE, 0xEF, 0xFE, 0xFF}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(data) -> str:
    if data is None:
        return "<none>"
    data = bytes(data)
    if not data:
        return "<empty>"
    h = " ".join(f"{b:02x}" for b in data)
    a = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{h}  |{a}|"


def parse_int(text: str) -> int:
    return int(text, 0)


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


async def sweep(device, opcodes, wait_s, settle_s):
    results = {}
    disconnected = asyncio.Event()
    inbox: asyncio.Queue = asyncio.Queue()

    def on_disconnect(_c):
        print(f"\n[{ts()}] *** peer disconnected ***")
        disconnected.set()

    def on_notify(_sender, data):
        inbox.put_nowait(bytes(data))

    async with BleakClient(device, timeout=30.0,
                           disconnected_callback=on_disconnect) as client:
        print(f"[{ts()}] Connected.\n")
        await client.start_notify(CH_CMD, on_notify)

        for opcode in opcodes:
            if disconnected.is_set():
                print(f"[{ts()}] Link lost; stopping sweep.")
                break

            frame = bytes([0x01, opcode])
            note = KNOWN.get(opcode, "")
            label = f"0x{opcode:02x}"
            print(f"[{ts()}] --> {label} : {hexdump(frame)}"
                  f"{'   (' + note + ')' if note else ''}")

            while not inbox.empty():
                inbox.get_nowait()

            try:
                await client.write_gatt_char(CH_CMD, frame, response=True)
            except Exception as exc:  # noqa: BLE001
                print(f"           write rejected: {type(exc).__name__}: {exc}")
                results[opcode] = ("write-rejected", None)
                continue

            try:
                reply = await asyncio.wait_for(inbox.get(), timeout=wait_s)
            except asyncio.TimeoutError:
                reply = None

            if reply is None:
                try:
                    latched = bytes(await client.read_gatt_char(CH_CMD))
                except Exception:  # noqa: BLE001
                    latched = None
                if latched and latched[:1] == b"\x00":
                    print(f"           (no notify, but latched) "
                          f"{hexdump(latched)}")
                    results[opcode] = ("latched", latched)
                else:
                    print("           no response")
                    results[opcode] = ("silent", None)
            else:
                echo_ok = len(reply) > 1 and reply[1] == opcode
                print(f"           <<< {hexdump(reply)}"
                      f"{'' if echo_ok else '   [opcode echo MISMATCH]'}")
                results[opcode] = ("reply", reply)

            await asyncio.sleep(settle_s)

    return results


def report(results):
    print("\n" + "=" * 72)
    print("OPCODE SWEEP RESULTS")
    print("=" * 72)

    answered = {k: v for k, (kind, v) in results.items() if kind in ("reply", "latched")}
    silent = [k for k, (kind, _) in results.items() if kind == "silent"]
    rejected = [k for k, (kind, _) in results.items() if kind == "write-rejected"]

    if answered:
        print("\nOpcodes that answered:")
        baseline = answered.get(0x02)
        for opcode, payload in sorted(answered.items()):
            same = "  (identical to 0x02 reply)" if (
                baseline is not None and payload == baseline and opcode != 0x02
            ) else ""
            print(f"  0x{opcode:02x} -> {hexdump(payload)}{same}")
    else:
        print("\nNo opcode produced a response.")

    if silent:
        print(f"\nSilent opcodes ({len(silent)}): "
              f"{', '.join(f'0x{o:02x}' for o in silent)}")
    if rejected:
        print(f"\nWrite-rejected ({len(rejected)}): "
              f"{', '.join(f'0x{o:02x}' for o in rejected)}")

    distinct = {}
    for opcode, payload in answered.items():
        distinct.setdefault(bytes(payload), []).append(opcode)
    if len(distinct) > 1:
        print(f"\n{len(distinct)} DISTINCT reply payloads - "
              f"these opcodes do different things:")
        for payload, ops in distinct.items():
            ops_s = ", ".join(f"0x{o:02x}" for o in ops)
            print(f"  {ops_s}: {hexdump(payload)}")


async def main():
    parser = argparse.ArgumentParser(description="Sweep Galcon GL6100 opcodes")
    parser.add_argument("--start", type=parse_int, default=0x00)
    parser.add_argument("--end", type=parse_int, default=0x0F)
    parser.add_argument("--wait", type=float, default=2.0,
                        help="Seconds to wait for a reply")
    parser.add_argument("--settle", type=float, default=0.5,
                        help="Seconds between opcodes")
    parser.add_argument("--scan-time", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the frames without sending them")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt")
    parser.add_argument("--allow-blocked", action="store_true",
                        help="Do not skip the precautionary blocklist")
    args = parser.parse_args()

    opcodes = [o for o in range(args.start, args.end + 1)
               if args.allow_blocked or o not in DEFAULT_BLOCKLIST]

    print(f"Opcode range 0x{args.start:02x}..0x{args.end:02x} "
          f"({len(opcodes)} frames)")
    if args.dry_run:
        for o in opcodes:
            print(f"  would send: 01 {o:02x}"
                  f"{'   (' + KNOWN[o] + ')' if o in KNOWN else ''}")
        return 0

    print("\nWARNING: these opcodes are undocumented. One of them may open a")
    print("valve, alter the schedule, or reset the unit. Watch the hardware.")
    if not args.yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 1

    device = await find_device(args.scan_time)
    if device is None:
        print("Valve not found.")
        return 1

    try:
        results = await sweep(device, opcodes, args.wait, args.settle)
    except Exception as exc:  # noqa: BLE001
        print(f"\nSweep error: {type(exc).__name__}: {exc}")
        return 1

    report(results)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
