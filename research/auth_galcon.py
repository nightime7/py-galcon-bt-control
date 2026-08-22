"""
Galcon GL6100 application-level authentication prober.

Background
----------
The valve exposes six vendor characteristics that all read back as zeros
until the session is authenticated:

    0x000d  20900101  write + notify + read   (20 bytes)  command/response pipe
    0x0010  20900102  notify + read           (20 bytes)  status
    0x0013  20900103  write + read            (20 bytes)
    0x0015  20900104  write + read            (20 bytes)
    0x0017  20900105  write + read            ( 4 bytes)  <- 4-digit PIN?
    0x0019  20900106  write + read            ( 2 bytes)  <- uint16 PIN?

The official app asks for a 4-digit PIN. A 4-digit PIN cannot be a BLE SMP
passkey (those are always 6 digits), so the PIN is presumably written to a
characteristic. This script tries the plausible encodings and reports which
one, if any, causes the device to stop returning zeros.

SAFETY
------
By default this only writes to the PIN/poll characteristics (20900105,
20900106). It does NOT write to 20900101, 20900103, or 20900104 because those
can hold command/schedule/configuration data and may overwrite valve settings.
Use --include-command or --include-config only for intentional protocol
probing.

Usage
-----
    python auth_galcon.py --pin 1234
    python auth_galcon.py --pin 1234 --verbose
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

SERVICE = "20900100-bdee-493a-aa74-a8137c9d43f0"
CH_CMD = "20900101-bdee-493a-aa74-a8137c9d43f0"
CH_STATUS = "20900102-bdee-493a-aa74-a8137c9d43f0"
CH_CFG_A = "20900103-bdee-493a-aa74-a8137c9d43f0"
CH_CFG_B = "20900104-bdee-493a-aa74-a8137c9d43f0"
CH_PIN4 = "20900105-bdee-493a-aa74-a8137c9d43f0"
CH_PIN2 = "20900106-bdee-493a-aa74-a8137c9d43f0"

ALL_VENDOR = [CH_CMD, CH_STATUS, CH_CFG_A, CH_CFG_B, CH_PIN4, CH_PIN2]


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


def build_candidates(pin: str, include_config: bool, include_command: bool):
    """Return [(label, char_uuid, payload)] auth attempts, cheapest first."""
    digits = [int(c) for c in pin]
    n = int(pin)
    ascii4 = pin.encode("ascii")                      # b"1234"
    bcd2 = bytes([digits[0] << 4 | digits[1],
                  digits[2] << 4 | digits[3]])        # 0x12 0x34
    u16le = n.to_bytes(2, "little")
    u16be = n.to_bytes(2, "big")
    raw4 = bytes(digits)                              # 01 02 03 04

    cands = [
        # --- 4-byte register: the Galcon 9001BT writes the PIN to its own
        # 4-byte characteristic (e8680401) as RAW DIGIT BYTES, i.e. PIN 1234
        # becomes 01 02 03 04. This is the highest-confidence candidate. ---
        ("PIN4 raw digits", CH_PIN4, raw4),
        ("PIN4 ascii", CH_PIN4, ascii4),
        ("PIN4 u16le+pad", CH_PIN4, u16le + b"\x00\x00"),
        ("PIN4 u32le", CH_PIN4, n.to_bytes(4, "little")),
        ("PIN4 u32be", CH_PIN4, n.to_bytes(4, "big")),
        ("PIN4 bcd+pad", CH_PIN4, bcd2 + b"\x00\x00"),

        # --- 2-byte register: uint16 or packed BCD ---
        ("PIN2 u16le", CH_PIN2, u16le),
        ("PIN2 u16be", CH_PIN2, u16be),
        ("PIN2 bcd", CH_PIN2, bcd2),

    ]

    if include_command:
        cands += [
            # 20900101 is also the schedule-save pipe. These are risky and
            # should only be used for intentional protocol probing.
            ("CMD ascii", CH_CMD, ascii4),
            ("CMD 01+ascii", CH_CMD, b"\x01" + ascii4),
            ("CMD 01+u16le", CH_CMD, b"\x01" + u16le),
            ("CMD 01+bcd", CH_CMD, b"\x01" + bcd2),
            ("CMD 02+ascii", CH_CMD, b"\x02" + ascii4),
            ("CMD 01 04 +ascii", CH_CMD, b"\x01\x04" + ascii4),
            ("CMD 01 02 +bcd", CH_CMD, b"\x01\x02" + bcd2),
            ("CMD aa+u16le", CH_CMD, b"\xaa" + u16le),
        ]

    if include_config:
        cands += [
            ("CFG_A ascii", CH_CFG_A, ascii4),
            ("CFG_B ascii", CH_CFG_B, ascii4),
        ]

    return cands


async def read_all(client, verbose=False):
    """Read every vendor characteristic. Returns {uuid: bytes}."""
    out = {}
    for uuid in ALL_VENDOR:
        try:
            out[uuid] = bytes(await client.read_gatt_char(uuid))
        except Exception as exc:  # noqa: BLE001
            out[uuid] = None
            if verbose:
                print(f"      read {uuid[:8]} failed: {exc}")
    return out


def any_nonzero(snapshot) -> bool:
    return any(v for v in snapshot.values() if v and any(v))


# Use the GL6100 status-poll characteristic as a harmless wake/refresh poke.
# Do not write 01 02 to CH_CMD: 20900101 is also the schedule-save pipe, and
# that short frame can be interpreted as zone 1 duration = 2h00m.
WAKE_PAYLOAD = bytes([0x02, 0x00])


async def wake(client, verbose=False):
    """Poke the poll characteristic so status registers populate."""
    for with_response in (True, False):
        try:
            await client.write_gatt_char(CH_PIN2, WAKE_PAYLOAD,
                                         response=with_response)
            await asyncio.sleep(0.4)
            return True
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"      wake(response={with_response}) failed: {exc}")
    return False


def describe(snapshot) -> str:
    lines = []
    for uuid, val in snapshot.items():
        if val and any(val):
            lines.append(f"      {uuid[:8]} = {hexdump(val)}")
    return "\n".join(lines)


async def find_device(name_hint: str, mac: str, scan_time: float):
    print(f"[{ts()}] Scanning up to {scan_time:.0f}s for the valve...")
    found = asyncio.Event()
    holder = {}

    def cb(device, adv):
        name = (device.name or adv.local_name or "")
        if device.address.upper() == mac.upper() or name_hint.lower() in name.lower():
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
    if holder["rssi"] < -80:
        print("       WARNING: weak signal, the link may drop mid-sweep.")
    return holder["dev"]


async def run_sweep(device, candidates, verbose):
    notifications = []
    disconnected = asyncio.Event()

    def on_disconnect(_c):
        print(f"\n[{ts()}] *** peer disconnected ***")
        disconnected.set()

    def make_handler(label):
        def handler(_sender, data):
            payload = bytes(data)
            notifications.append((label, payload))
            print(f"[{ts()}]   <<< NOTIFY {label}: {hexdump(payload)}")
        return handler

    async with BleakClient(device, timeout=30.0,
                           disconnected_callback=on_disconnect) as client:
        print(f"[{ts()}] Connected.")

        for uuid, label in ((CH_CMD, "20900101"), (CH_STATUS, "20900102")):
            try:
                await client.start_notify(uuid, make_handler(label))
                print(f"[{ts()}] subscribed to {label}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{ts()}] could not subscribe {label}: {exc}")

        baseline = await read_all(client, verbose)
        print(f"[{ts()}] Baseline all-zero: {not any_nonzero(baseline)}")

        # Try wake alone first - the device may need no PIN at all.
        print(f"[{ts()}] Trying wake ({hexdump(WAKE_PAYLOAD)}) with no PIN...")
        await wake(client, verbose)
        snapshot = await read_all(client, verbose)
        if any_nonzero(snapshot):
            print(f"\n{'=' * 70}")
            print("HIT! Wake alone was enough - no PIN required:")
            print(describe(snapshot))
            print(f"{'=' * 70}")
            return True, notifications
        print(f"[{ts()}] Still zeros after wake; sweeping PIN encodings.")

        for label, uuid, payload in candidates:
            if disconnected.is_set():
                print(f"[{ts()}] Link lost; stopping sweep.")
                break

            print(f"\n[{ts()}] TRY {label:<20} -> {uuid[:8]}  {hexdump(payload)}")
            wrote = False
            for with_response in (True, False):
                try:
                    await client.write_gatt_char(uuid, payload,
                                                 response=with_response)
                    wrote = True
                    break
                except Exception as exc:  # noqa: BLE001
                    if verbose:
                        print(f"      write(response={with_response}) "
                              f"failed: {type(exc).__name__}: {exc}")
            if not wrote:
                print("      write rejected by device")
                continue

            # The 9001BT needs a wake poke before status reads are valid.
            await wake(client, verbose)
            await asyncio.sleep(0.4)

            snapshot = await read_all(client, verbose)
            if any_nonzero(snapshot):
                print(f"\n{'=' * 70}")
                print(f"HIT! '{label}' produced non-zero data:")
                print(describe(snapshot))
                print(f"{'=' * 70}")
                return True, notifications

            if verbose:
                print("      still all zeros")

        return False, notifications


async def main():
    parser = argparse.ArgumentParser(
        description="Probe Galcon app-level authentication")
    parser.add_argument("--pin", required=True, help="The 4-digit PIN")
    parser.add_argument("--mac", default="",
                        help="Optional MAC to disambiguate scans; discovery "
                             "works by device name alone otherwise")
    parser.add_argument("--name", default="gl6100")
    parser.add_argument("--scan-time", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--include-config", action="store_true",
                        help="Also write to 20900103/104. These likely hold "
                             "zone/schedule config and could be corrupted.")
    parser.add_argument("--include-command", action="store_true",
                        help="Also write auth probes to 20900101. Dangerous: "
                             "this is the schedule-save pipe on GL6100.")
    args = parser.parse_args()

    if not (args.pin.isdigit() and len(args.pin) == 4):
        print("PIN must be exactly 4 digits.")
        return 2

    device = await find_device(args.name, args.mac, args.scan_time)
    if device is None:
        print("Valve not found. It advertises infrequently; try again, or "
              "press a button on the unit first.")
        return 1

    candidates = build_candidates(args.pin, args.include_config,
                                  args.include_command)
    print(f"[{ts()}] {len(candidates)} candidate encodings to try.\n")

    try:
        success, notifications = await run_sweep(device, candidates, args.verbose)
    except Exception as exc:  # noqa: BLE001
        print(f"\nSweep aborted: {type(exc).__name__}: {exc}")
        return 1

    print("\n" + "=" * 70)
    if notifications:
        print("Notifications captured (these reveal the response framing):")
        for label, payload in notifications:
            print(f"  {label}: {hexdump(payload)}")
    else:
        print("No notifications were received at any point.")
        print("If nothing here worked, the PIN is probably not written as a")
        print("bare value - capture the real exchange with an Android HCI")
        print("snoop log while using the official app.")
    print("=" * 70)

    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
