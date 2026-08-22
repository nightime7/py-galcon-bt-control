"""
Galcon BLE discovery / probe tool.

Goal: find out how the Galcon 6100BT/DC4 actually talks, instead of guessing UUIDs.

What it does:
  1. Scans and prints FULL advertisement data (service UUIDs, manufacturer data,
     service data, TX power) for every candidate device.
  2. Connects to the chosen device.
  3. Enumerates every service / characteristic / descriptor with its properties.
  4. Reads every readable characteristic and descriptor (hex + ascii).
  5. Subscribes to every notify/indicate characteristic and logs traffic.
  6. Keeps listening so you can press buttons on the unit / use the Galcon app
     and watch which handles change.

Usage:
    python probe_galcon.py                 # scan + auto-pick best Galcon match
    python probe_galcon.py --scan-only     # just dump advertisements
    python probe_galcon.py --address AA:BB:CC:DD:EE:FF
    python probe_galcon.py --name GL6100
    python probe_galcon.py --listen 120    # listen 120s for notifications
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

# Galcon sells its BLE valve controllers under several names. "Tondo" is a
# Galcon product line and its units advertise as "Tondo-<serial hex>".
DEFAULT_NAME_HINTS = (
    "galcon",
    "tondo",
    "gl6100",
    "gl61",
    "dc4",
    "6100",
    "6104",
    "9001",
    "bt-",
)

# Characteristics that are noisy / pointless to poll.
SKIP_READ_UUIDS = set()


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(data: bytes) -> str:
    if data is None:
        return "<none>"
    if len(data) == 0:
        return "<empty>"
    hexpart = " ".join(f"{b:02x}" for b in data)
    asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hexpart}  |{asciipart}|"


# ---------------------------------------------------------------- scanning


async def scan(seconds: float, name_hints, address=None, show_all=False,
               quiet=False):
    """Scan and return {address: (device, advertisement_data)} for candidates."""
    seen = {}
    best_rssi = {}

    def callback(device, adv):
        addr = device.address.upper()
        name = (device.name or adv.local_name or "").strip()
        is_match = False
        if address and addr == address.upper():
            is_match = True
        elif name and any(h in name.lower() for h in name_hints):
            is_match = True

        if not (is_match or show_all):
            return

        # Keep the record with the strongest signal and the richest name.
        prev = seen.get(addr)
        if prev is None or adv.rssi > prev[1].rssi or (not prev[0].name and name):
            seen[addr] = (device, adv)

        if quiet:
            return

        # Only print when RSSI changes meaningfully, to keep output readable.
        prev_rssi = best_rssi.get(addr)
        if prev_rssi is None or abs(prev_rssi - adv.rssi) >= 5:
            best_rssi[addr] = adv.rssi
            print(f"\n[{ts()}] {'MATCH' if is_match else 'dev  '} "
                  f"{name or '<no name>'} ({device.address})  RSSI {adv.rssi} dBm")
            if adv.service_uuids:
                for u in adv.service_uuids:
                    print(f"          service uuid : {u}")
            if adv.manufacturer_data:
                for cid, blob in adv.manufacturer_data.items():
                    print(f"          mfr 0x{cid:04x}  : {hexdump(bytes(blob))}")
            if adv.service_data:
                for u, blob in adv.service_data.items():
                    print(f"          svc data {u}: {hexdump(bytes(blob))}")
            if adv.tx_power is not None:
                print(f"          tx power     : {adv.tx_power}")

    print(f"[{ts()}] Scanning {seconds:.0f}s "
          f"({'all devices' if show_all else 'Galcon candidates'})...")
    print("      Tip: press a button on the valve to make it advertise.\n")

    scanner = BleakScanner(callback)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()

    return seen


async def rssi_meter(name_hints, address=None):
    """Live signal-strength meter. Walk around to find a workable spot."""
    print("Live RSSI meter. Move the PC (or a USB BT dongle on an extension")
    print("cable) toward the valve until you are better than -75 dBm.")
    print("Ctrl+C to stop.\n")

    def callback(device, adv):
        name = (device.name or adv.local_name or "").strip()
        if address and device.address.upper() != address.upper():
            return
        if not address and not (name and any(h in name.lower()
                                             for h in name_hints)):
            return

        rssi = adv.rssi
        # -100 dBm -> empty bar, -40 dBm -> full bar.
        filled = max(0, min(30, int((rssi + 100) / 2)))
        bar = "#" * filled + "." * (30 - filled)
        if rssi >= -70:
            verdict = "GOOD"
        elif rssi >= -80:
            verdict = "marginal"
        else:
            verdict = "TOO WEAK"
        print(f"[{ts()}] {name:<26} {rssi:>4} dBm [{bar}] {verdict}")

    scanner = BleakScanner(callback)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()


# ---------------------------------------------------------------- probing


async def enumerate_gatt(client: BleakClient):
    """Print the full GATT table and return the list of notifiable characteristics."""
    notifiables = []

    print("\n" + "=" * 78)
    print("GATT TABLE")
    print("=" * 78)

    services = client.services
    for service in services:
        print(f"\n[service] {service.uuid}")
        if service.description:
            print(f"          {service.description}")

        for char in service.characteristics:
            props = ",".join(char.properties)
            print(f"  [char] handle=0x{char.handle:04x} {char.uuid}")
            print(f"         props: {props}")
            if char.description:
                print(f"         desc : {char.description}")

            if "read" in char.properties and char.uuid not in SKIP_READ_UUIDS:
                try:
                    value = await client.read_gatt_char(char)
                    print(f"         READ : {hexdump(bytes(value))}")
                except Exception as exc:  # noqa: BLE001 - probe tool, report and continue
                    print(f"         READ : <failed: {exc}>")

            if "notify" in char.properties or "indicate" in char.properties:
                notifiables.append(char)

            for desc in char.descriptors:
                try:
                    value = await client.read_gatt_descriptor(desc.handle)
                    print(f"    [desc] 0x{desc.handle:04x} {desc.uuid} "
                          f"= {hexdump(bytes(value))}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    [desc] 0x{desc.handle:04x} {desc.uuid} "
                          f"<read failed: {exc}>")

    print("\n" + "=" * 78)
    print("SUMMARY - writable characteristics (candidates for commands)")
    print("=" * 78)
    for service in services:
        for char in service.characteristics:
            if "write" in char.properties or "write-without-response" in char.properties:
                print(f"  {char.uuid}  handle=0x{char.handle:04x}  "
                      f"[{','.join(char.properties)}]")

    print("\nNotify/indicate characteristics (device -> us):")
    for char in notifiables:
        print(f"  {char.uuid}  handle=0x{char.handle:04x}  "
              f"[{','.join(char.properties)}]")

    return notifiables


async def subscribe_all(client: BleakClient, notifiables):
    """Turn on notifications for everything we can."""
    subscribed = []
    print("\n" + "=" * 78)
    print("SUBSCRIBING TO NOTIFICATIONS")
    print("=" * 78)

    for char in notifiables:
        uuid = char.uuid
        handle = char.handle

        def make_handler(u, h):
            def handler(_sender, data: bytearray):
                print(f"[{ts()}] NOTIFY 0x{h:04x} {u}\n"
                      f"           {hexdump(bytes(data))}")
            return handler

        try:
            await client.start_notify(char, make_handler(uuid, handle))
            subscribed.append(char)
            print(f"  subscribed: {uuid} (0x{handle:04x})")
        except Exception as exc:  # noqa: BLE001
            print(f"  failed    : {uuid} (0x{handle:04x}) -> {exc}")

    return subscribed


async def probe(device, listen_seconds: float, do_pair: bool = False):
    disconnected = asyncio.Event()

    def on_disconnect(_client):
        print(f"\n[{ts()}] *** DISCONNECTED by peer ***")
        disconnected.set()

    print(f"\n[{ts()}] Connecting to {device.name or '<no name>'} "
          f"({device.address})...")

    async with BleakClient(device, timeout=30.0,
                           disconnected_callback=on_disconnect) as client:
        print(f"[{ts()}] Connected. MTU = {getattr(client, 'mtu_size', '?')}")

        if do_pair:
            print(f"[{ts()}] Attempting to pair/bond...")
            print("        If Windows shows a PIN/confirm prompt, accept it.")
            print("        Galcon units commonly use PIN 000000 or 123456.")
            try:
                paired = await client.pair(protection_level=2)
                print(f"[{ts()}] pair() returned: {paired}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{ts()}] pair() failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)

        notifiables = await enumerate_gatt(client)
        subscribed = await subscribe_all(client, notifiables)

        print("\n" + "=" * 78)
        print(f"LISTENING {listen_seconds:.0f}s - now interact with the device")
        print("=" * 78)
        print("  * Press buttons on the valve")
        print("  * Open/close a zone from the official Galcon app (on a phone)")
        print("    NOTE: the app must connect to the valve, so it may kick us off.")
        print("  * Watch which handle emits data and what the bytes look like.\n")

        try:
            await asyncio.wait_for(disconnected.wait(), timeout=listen_seconds)
        except asyncio.TimeoutError:
            pass

        if not disconnected.is_set():
            print(f"\n[{ts()}] Re-reading all readable characteristics "
                  f"(diff against the first dump to spot state bytes)...")
            for service in client.services:
                for char in service.characteristics:
                    if "read" in char.properties:
                        try:
                            value = await client.read_gatt_char(char)
                            print(f"  {char.uuid} = {hexdump(bytes(value))}")
                        except Exception as exc:  # noqa: BLE001
                            print(f"  {char.uuid} <failed: {exc}>")

            for char in subscribed:
                try:
                    await client.stop_notify(char)
                except Exception:  # noqa: BLE001
                    pass

    print(f"\n[{ts()}] Probe finished.")


# ---------------------------------------------------------------- main


async def main():
    parser = argparse.ArgumentParser(description="Galcon BLE probe")
    parser.add_argument("--address", help="Exact BLE address to target "
                                          "(unreliable: Galcon rotates its address)")
    parser.add_argument("--name", help="Name substring to match, e.g. the serial "
                                       "'6a88e810' (case-insensitive)")
    parser.add_argument("--scan-time", type=float, default=20.0,
                        help="Seconds to scan (default 20)")
    parser.add_argument("--listen", type=float, default=90.0,
                        help="Seconds to listen for notifications (default 90)")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only scan and dump advertisements, do not connect")
    parser.add_argument("--all", action="store_true",
                        help="Show every BLE device seen, not just Galcon candidates")
    parser.add_argument("--retries", type=int, default=5,
                        help="Connection attempts before giving up (default 5)")
    parser.add_argument("--meter", action="store_true",
                        help="Live RSSI meter: walk around to find a spot with "
                             "good signal, then connect. Ctrl+C to stop.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the live scan log; print only the summary")
    parser.add_argument("--pair", action="store_true",
                        help="Pair/bond with the device after connecting. "
                             "Needed if all characteristics read as zeros.")
    args = parser.parse_args()

    hints = list(DEFAULT_NAME_HINTS)
    if args.name:
        hints.append(args.name.lower())

    if args.meter:
        await rssi_meter(hints, args.address)
        return 0

    found = await scan(args.scan_time, hints, args.address, show_all=args.all,
                       quiet=args.quiet)

    if not found:
        print("\nNo matching device found.")
        print("Try: python probe_galcon.py --all --scan-time 30 --scan-only")
        print("     (then re-run with --address of whatever looks like the valve)")
        print("Also check: is a phone running the Galcon app currently connected?")
        print("A connected BLE device stops advertising and becomes invisible.")
        return 1

    print("\n" + "=" * 78)
    print("SCAN SUMMARY")
    print("=" * 78)
    for addr, (dev, adv) in sorted(found.items(),
                                   key=lambda kv: kv[1][1].rssi, reverse=True):
        name = dev.name or adv.local_name or "<no name>"
        # Random/private addresses start with 4,5,6,7,C,D,E,F in the top bits.
        first = int(addr[:2], 16)
        addr_kind = "random" if (first & 0xC0) else "public"
        print(f"\n  {name}")
        print(f"    address : {addr}  ({addr_kind})")
        print(f"    rssi    : {adv.rssi} dBm")
        if adv.service_uuids:
            for u in adv.service_uuids:
                print(f"    service : {u}")
        if adv.manufacturer_data:
            for cid, blob in adv.manufacturer_data.items():
                print(f"    mfr id  : 0x{cid:04x}")
                print(f"    mfr data: {hexdump(bytes(blob))}")
        if adv.service_data:
            for u, blob in adv.service_data.items():
                print(f"    svc data {u}:")
                print(f"              {hexdump(bytes(blob))}")
        if adv.tx_power is not None:
            print(f"    tx power: {adv.tx_power}")
    print("\n" + "=" * 78)
    print(f"{len(found)} device(s) seen.")
    print("=" * 78)

    if args.scan_only:
        return 0

    # Pick the strongest signal.
    device, adv = max(found.values(), key=lambda kv: kv[1].rssi)
    if adv.rssi < -85:
        print(f"\nWARNING: RSSI {adv.rssi} dBm is very weak. Connections will "
              f"likely fail during GATT discovery.")
        print("         Run with --meter and move the PC closer to the valve.")
        print("         Aim for better than -75 dBm.")

    if len(found) > 1:
        print(f"\nNOTE: {len(found)} Galcon units found. Targeting "
              f"'{device.name}'. Use --name <serial> to pick a specific one.")

    for attempt in range(1, args.retries + 1):
        try:
            await probe(device, args.listen, do_pair=args.pair)
            return 0
        except (BleakError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
            print(f"\n[{ts()}] Attempt {attempt}/{args.retries} failed: "
                  f"{type(exc).__name__}: {exc}")
            if attempt < args.retries:
                print(f"[{ts()}] Re-scanning and retrying in 3s...")
                await asyncio.sleep(3.0)
                # Address rotates, so re-resolve the device each attempt.
                refreshed = await scan(10.0, hints, None)
                if refreshed:
                    device, adv = max(refreshed.values(),
                                      key=lambda kv: kv[1].rssi)
                    print(f"[{ts()}] Re-acquired at {device.address} "
                          f"({adv.rssi} dBm)")

    print("\nAll connection attempts failed. Most likely causes, in order:")
    print("  1. SIGNAL TOO WEAK. This is the #1 cause of a timeout during")
    print("     service discovery. Move within a few metres, line of sight.")
    print("  2. A phone running the Galcon app is holding the connection.")
    print("     Close the app and disable Bluetooth on the phone.")
    print("  3. Stale Windows pairing. Check Settings > Bluetooth & devices")
    print("     and 'Remove device' for any Tondo/Galcon entry, then retry.")
    print("  4. Weak/low battery in the valve reduces TX power.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
