"""
Connect-timing diagnostic for the GL6100.

Isolates where time actually goes between "advertisement seen" and "usable
connection", to find out whether connect-time delay can be reduced and
whether a saved MAC lets us skip/shorten scanning.

This does not modify any schedule data - it only connects, times phases,
and disconnects. Safe to run repeatedly.

Usage
-----
    python research/connect_timing.py
    python research/connect_timing.py --mac AA:BB:CC:DD:EE:FF
    python research/connect_timing.py --runs 3
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_galcon import (  # noqa: E402
    NAME_HINT,
    SERVICE_UUID,
    load_saved_mac,
    ts,
)


def log(label, elapsed=None):
    suffix = f" ({elapsed:.2f}s)" if elapsed is not None else ""
    print(f"[{ts()}] {label}{suffix}")


async def scan_for_device(scan_time: float, mac: str | None):
    """Same matching logic as control_galcon.find_device, with timing."""
    found = asyncio.Event()
    holder = {}

    def cb(device, adv):
        name = device.name or adv.local_name or ""
        if (mac and device.address.upper() == mac.upper()) \
                or NAME_HINT in name.lower():
            holder["dev"] = device
            holder["rssi"] = adv.rssi
            found.set()

    scanner = BleakScanner(cb)
    start = time.monotonic()
    await scanner.start()
    try:
        await asyncio.wait_for(found.wait(), timeout=scan_time)
    except asyncio.TimeoutError:
        pass
    finally:
        await scanner.stop()
    elapsed = time.monotonic() - start

    if "dev" not in holder:
        return None, elapsed
    log(f"Found {holder['dev'].name} at {holder['rssi']} dBm", elapsed)
    return holder["dev"], elapsed


async def timed_connect(device, label, **client_kwargs):
    start = time.monotonic()
    client = BleakClient(device, timeout=30.0, **client_kwargs)
    await client.connect()
    elapsed = time.monotonic() - start
    log(f"{label}: connected", elapsed)
    return client, elapsed


async def run(args):
    mac = args.mac or load_saved_mac()

    # --- Phase 1: scan + baseline connect (default BleakClient options) ---
    log("Phase 1: scan, then default connect (no service scoping)")
    device, scan_elapsed = await scan_for_device(args.scan_time, mac)
    if device is None:
        log("Device not found; aborting")
        return 1

    client, connect_elapsed = await timed_connect(device, "default connect")
    await client.disconnect()
    log("disconnected")

    results = [("scan", scan_elapsed), ("default connect", connect_elapsed)]

    # --- Phase 2: reconnect to the SAME device object, default options ---
    # Tests whether Windows caches anything from phase 1 that speeds up an
    # immediate second connect within the same process.
    log("Phase 2: immediate reconnect, same device object, default options")
    client2, reconnect_elapsed = await timed_connect(device, "reconnect (same object)")
    await client2.disconnect()
    log("disconnected")
    results.append(("reconnect same object", reconnect_elapsed))

    # --- Phase 3: reconnect with services= scoping + cached services ---
    log("Phase 3: reconnect with services=[SERVICE_UUID], use_cached_services=True")
    client3, scoped_elapsed = await timed_connect(
        device, "reconnect (scoped)",
        services=[SERVICE_UUID], winrt=dict(use_cached_services=True))
    await client3.disconnect()
    log("disconnected")
    results.append(("reconnect scoped+cached", scoped_elapsed))

    # --- Phase 4: connect directly by MAC string, no scan at all ---
    # Only meaningful if a MAC is known. Tests whether bleak/WinRT can
    # connect without us running BleakScanner first.
    if mac:
        log(f"Phase 4: connect directly by address {mac}, no scan")
        try:
            start = time.monotonic()
            client4 = BleakClient(mac, timeout=30.0,
                                  services=[SERVICE_UUID],
                                  winrt=dict(use_cached_services=True))
            await client4.connect()
            elapsed = time.monotonic() - start
            log("direct-by-address connect: connected", elapsed)
            await client4.disconnect()
            log("disconnected")
            results.append(("direct by address (no scan)", elapsed))
        except Exception as exc:  # noqa: BLE001
            log(f"direct-by-address connect FAILED: {type(exc).__name__}: {exc}")
    else:
        log("Phase 4 skipped: no MAC known (--mac or --set-mac in "
            "control_galcon.py)")

    print(f"\n[{ts()}] Summary:")
    for label, elapsed in results:
        print(f"    {label:<32} {elapsed:6.2f}s")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", help="Controller MAC (defaults to the "
                                     "value saved via control_galcon.py "
                                     "--set-mac, if any)")
    parser.add_argument("--scan-time", type=float, default=60.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
