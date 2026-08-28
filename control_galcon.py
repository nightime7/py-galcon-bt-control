"""
Galcon GL6100 / 6100BT DC4 valve control.

Protocol notes
--------------
The controller exposes one vendor GATT service with these characteristics:

    UUID                                   properties          purpose
    20900101-bdee-493a-aa74-a8137c9d43f0   write+notify+read   command / schedule read-write
    20900102-bdee-493a-aa74-a8137c9d43f0   read+notify         status
    20900103-bdee-493a-aa74-a8137c9d43f0   write+read          valve control / device-wide settings
    20900104-bdee-493a-aa74-a8137c9d43f0   write+read          date/time sync
    20900105-bdee-493a-aa74-a8137c9d43f0   write+read          PIN (world-readable/writable)
    20900106-bdee-493a-aa74-a8137c9d43f0   write+read          status poll / per-zone schedule select

VALVE CONTROL (20900103) is a 20-byte frame:

    OPEN:  byte0 = 0x00 (fixed)
           byte1 = 0x80 | zone   (zone 1 -> 0x81, zone 2 -> 0x82, zone 3 -> 0x83, zone 4 -> 0x84)
           byte4 = duration in whole minutes
           remaining bytes 0x00
    CLOSE: byte0 = zone (1-4)
           remaining bytes 0x00

STATUS (20900102) is a 20-byte frame. byte0 is 0xff while idle. Active
firmware variants use 0xf0-0xf3 for one zone. For two zones, each zero-based
zone index occupies one nibble of byte0; bytes 2-3 and 5-6 hold the remaining
time for the first and second nibbles respectively. Reading 20900102 returns
all zeros unless 02 00 was recently written to 20900106 first.

STATUS POLL / KEEPALIVE (20900106): write 02 00 immediately before reading
20900102, or the status characteristic reads back all zeros even while a
zone is genuinely running.

DATE/TIME SYNC (20900104): an 8-byte frame
[century][year][month][day][hour][minute][second][weekday].

PIN (20900105): a 4-byte register holding the PIN as raw digit bytes (PIN
"1234" -> 01 02 03 04). This application-level register is separate from
BLE pairing; the BLE pairing PIN is entered through the official Galcon app.

SCHEDULE / PROGRAM RECORDS
---------------------------
Each zone has a 20-byte schedule record, read and written through 20900101.

Do not send short frames such as `01 02` to 20900101 as a wake/keepalive
poke: 20900101 is also the schedule-save characteristic, and a short frame
can be interpreted as a partial schedule record (zone=1, duration
hours=2), silently rewriting zone 1's duration to 120 minutes. Use the
20900106 status poll (02 00) as the harmless wake/refresh poke instead.

To READ zone N's schedule: write `01 0N` to 20900106, then read 20900101.
The first byte of the reply echoes N.

To WRITE (save) a zone's schedule: read the current 20-byte record, modify
only the fields being changed, then write the full 20-byte record back to
20900101.

Field layout:

    byte0        : zone index (1-4), echoed back on read
    byte1        : duration, HOURS component
    byte2        : duration, MINUTES component (0-59)
                   total duration in minutes = byte1*60 + byte2
    byte3        : unused (0x00)
    byte4        : mode/day-of-week byte -
                     bit7 = 0 -> WEEKLY mode; bits 0-6 = day-of-week
                       bitmask, Sunday-first (bit0=Sun ... bit6=Sat)
                     bit7 = 1 -> CYCLIC mode; bits 0-6 unused/zeroed
    byte5        : window 1 START HOUR (0-23, plain binary, not BCD)
    byte6        : window 1 START MINUTE (0-59, plain binary, not BCD)
    byte7        : window 2 hour (0xff = unused)
    byte8        : window 2 minute
    byte9        : window 3 hour (0xff = unused)
    byte10       : window 3 minute
    byte11       : window 4 hour (0xff = unused)
    byte12       : window 4 minute
    byte13       : CYCLIC mode: 0x80 + start_in_days
                   WEEKLY mode: fixed 0x80
    byte14       : CYCLIC mode: 0xC0 + cadence_days
                   WEEKLY mode: fixed 0x00
    byte15-19    : unused (0x00)

Duration (byte1/byte2) and window 1 start time (byte5/byte6) use the same
encoding in both WEEKLY and CYCLIC modes. Windows 2-4 are read/written using
the same hour/minute encoding as window 1.

Switching a zone from CYCLIC back to WEEKLY mode requires resetting bytes
11-14 to their weekly baseline (0xff, 0x00, 0x80, 0x00) in the same write
that clears bit7 of byte4; otherwise the device rejects the write entirely
and the zone's stored schedule is left unchanged. modify_schedule() handles
this automatically.

CAUTION: modify_schedule()'s --set-days always clears bit7 (forces WEEKLY
mode). Do not use --set-days on a zone that might currently be in CYCLIC
mode without checking first via --read-schedule, or it will silently
switch that zone back to weekly scheduling.

Usage
-----
    python control_galcon.py --set-pin 1234
    python control_galcon.py --status
    python control_galcon.py --programs
    python control_galcon.py --enroll
    python control_galcon.py --open 1 --minutes 5
    python control_galcon.py --close 1
    python control_galcon.py --interactive
    python control_galcon.py --read-schedule 1
    python control_galcon.py --read-schedule 1 --set-duration 15 --write-schedule
    python control_galcon.py --raw 0081000004000000000000000000000000000000

First-time setup: run --set-pin once (see galcon_device.example.json for the
config file this writes to). --set-mac is optional and only speeds up/
disambiguates scanning; the controller is found by advertised name either way.
"""

import argparse
import asyncio
import json
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

# Single source of truth for the version; setup_msi.py and the GUI read this.
APP_VERSION = "1.2.0"
GITHUB_REPO = "nightime7/py-galcon-bt-control"

# The GL6100 advertises under this name regardless of which physical unit
# it is, so matching on it alone is enough to find the controller. A saved
# MAC address (see load_saved_mac/save_mac below) is only an optional
# optimization to skip ambiguity if multiple matching devices are nearby.
NAME_HINT = "gl6100"

# Device-specific data (PIN, MAC address) lives in this gitignored file, not
# in source control. Run with --set-pin/--set-mac once to create it, or copy
# galcon_device.example.json to galcon_device.json and fill in your values.
CONFIG_PATH = Path(__file__).with_name("galcon_device.json")

REPAIR_HINT = (
    "This usually means the Windows/BLE bond with the controller is gone. "
    "Open the Galcon app, unpair and re-pair with the controller (watch for "
    "the PIN on its display), then re-run this command."
)

# All vendor characteristics live under this one service. Passing this to
# BleakClient(services=...) restricts Windows' GATT discovery to just this
# service instead of enumerating every service/characteristic/descriptor on
# the device, which is the main source of the delay between "found" and
# "connected" on Windows.
SERVICE_UUID = "20900100-bdee-493a-aa74-a8137c9d43f0"

CHAR_COMMAND = "20900101-bdee-493a-aa74-a8137c9d43f0"
CHAR_STATUS = "20900102-bdee-493a-aa74-a8137c9d43f0"
CHAR_VALVE = "20900103-bdee-493a-aa74-a8137c9d43f0"
CHAR_PIN = "20900105-bdee-493a-aa74-a8137c9d43f0"
CHAR_POLL = "20900106-bdee-493a-aa74-a8137c9d43f0"

STATUS_POLL = bytes([0x02, 0x00])

# After closing one member of a pair, the surviving zone can briefly use this
# compact single-zone form with its countdown in bytes 5-6.
TRANSITIONAL_ZONE_CODES = {0x0f: 1, 0x1f: 2, 0x2f: 3, 0x3f: 4}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def hexdump(data) -> str:
    if data is None:
        return "<none>"
    data = bytes(data)
    if not data:
        return "<empty>"
    h = " ".join(f"{b:02x}" for b in data)
    a = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{h}  |{a}|"


def pin_to_bytes(pin: str) -> bytes:
    """PIN 1234 -> b'\\x01\\x02\\x03\\x04' (raw digits, as the 9001BT expects)."""
    return bytes(int(c) for c in pin)


def decode_active_zones(value: bytes):
    """Return [(zone, remaining_seconds)] for a status frame, or None."""
    if not value or len(value) < 7:
        return None
    status_byte = value[0]
    if status_byte == 0xff:
        return []
    if status_byte & 0xf0 == 0xf0 and status_byte & 0x0f < 4:
        zone = (status_byte & 0x0f) + 1
        return [(zone, value[2] * 60 + value[3])]
    if status_byte in TRANSITIONAL_ZONE_CODES:
        zone = TRANSITIONAL_ZONE_CODES[status_byte]
        return [(zone, value[5] * 60 + value[6])]
    first_index, second_index = status_byte >> 4, status_byte & 0x0f
    if (first_index < 4 and second_index < 4
            and first_index != second_index):
        return [(first_index + 1, value[2] * 60 + value[3]),
                (second_index + 1, value[5] * 60 + value[6])]
    return None


def _load_device_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_device_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def load_saved_pin() -> str | None:
    """Read the persisted PIN from CONFIG_PATH, if any."""
    pin = _load_device_config().get("pin")
    if pin and pin.isdigit() and len(pin) == 4:
        return pin
    return None


def save_pin(pin: str) -> None:
    """Persist the PIN to CONFIG_PATH so future runs don't need --pin."""
    data = _load_device_config()
    data["pin"] = pin
    _save_device_config(data)
    print(f"[{ts()}] Saved PIN to {CONFIG_PATH}")


def load_saved_mac() -> str | None:
    """Read the persisted controller MAC address from CONFIG_PATH, if any."""
    mac = _load_device_config().get("mac")
    return mac or None


def save_mac(mac: str) -> None:
    """Persist the controller MAC address to CONFIG_PATH."""
    data = _load_device_config()
    data["mac"] = mac
    _save_device_config(data)
    print(f"[{ts()}] Saved MAC address to {CONFIG_PATH}")


def build_open_payload(zone: int, minutes: int) -> bytes:
    """
    Valve-open frame (see module docstring for the full protocol notes).

        byte0 = 0x00
        byte1 = 0x80 | zone
        byte4 = minutes
    """
    frame = bytearray(20)
    frame[0] = 0x00
    frame[1] = 0x80 | zone
    frame[4] = minutes
    return bytes(frame)


def build_close_payload(zone: int) -> bytes:
    """Valve-close frame: byte0 = zone, rest zero."""
    frame = bytearray(20)
    frame[0] = zone
    return bytes(frame)


def build_seasonal_payload(percent: int) -> bytes:
    """
    Device-wide seasonal adjustment frame (20900103):
        00 00 02 00 00 00 00 6e 00 00 00 00 00 00 00 00 00 00 00 00  (110%)
    byte2 = 0x02 (seasonal adjustment opcode), byte7 = percentage (raw
    byte, e.g. 110 for 110%). Applies to ALL zones' programmed durations.
    """
    frame = bytearray(20)
    frame[2] = 0x02
    frame[7] = percent & 0xFF
    return bytes(frame)


def build_rainoff_payload(days: int) -> bytes:
    """
    Device-wide "Rain Off" frame (20900103):
        00 00 01 00 00 00 03 00 00 00 00 00 00 00 00 00 00 00 00 00  (3 days)
        00 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  (cleared)
    byte2 = 0x01 (rain-off opcode), byte6 = number of days to suspend all
    programs (0 = clear/cancel rain-off).
    """
    frame = bytearray(20)
    frame[2] = 0x01
    frame[6] = days & 0xFF
    return bytes(frame)


DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday"]


async def read_schedule(client, zone: int, label_prefix="", debug=False,
                        display=True):
    """
    Query zone N's 20-byte schedule record.

    Write 01 0N to CHAR_POLL (20900106), then read CHAR_COMMAND (20900101).
    Reply byte0 echoes N.

    GOTCHA: zone 2's select opcode (01 02) is byte-identical to the old
    wake/info opcode some scripts sent over CHAR_COMMAND. That is unsafe
    because CHAR_COMMAND is also the schedule-save characteristic. Retry
    with a short settle delay until byte0 echoes the zone.
    """
    value = None
    for attempt in range(4):
        try:
            await client.write_gatt_char(CHAR_POLL, bytes([0x01, zone]),
                                         response=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts()}] zone {zone} select failed: {exc}")
            return None

        await asyncio.sleep(0.3)

        try:
            value = bytes(await client.read_gatt_char(CHAR_COMMAND))
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts()}] zone {zone} schedule read failed: {exc}")
            return None

        if value and value[0] == zone:
            break
        if attempt < 3:
            if debug:
                print(f"[{ts()}] zone {zone} read returned stale/wrong data "
                      f"(byte0={value[0]:#04x}), retrying...")
            await asyncio.sleep(0.3)

    if debug:
        print(f"[{ts()}] {label_prefix}zone {zone} schedule: {hexdump(value)}")
    if not value or value[0] != zone:
        print(f"[{ts()}] {label_prefix}zone {zone} schedule read failed.")
        print(f"[{ts()}] {REPAIR_HINT}")
    if display and len(value) >= 8 and value[0] == zone:
        days = value[4]
        cyclic = bool(days & 0x80)
        active = [DAY_NAMES[b] for b in range(7) if days & (1 << b)]
        total_minutes = value[1] * 60 + value[2]
        print(f"       duration    : {value[1]}h{value[2]:02d}m "
              f"({total_minutes} min total)")
        if cyclic:
            print(f"       mode        : CYCLIC (byte4={days:#04x}); "
                  f"start-in={value[13]-0x80} days, "
                  f"cadence=every {value[14]-0xC0} days")
        else:
            print(f"       mode        : WEEKLY, days={days:#04x} "
                  f"({', '.join(active) or 'none'})")
        print(f"       window1     : {value[5]:02d}:{value[6]:02d}")
        print(f"       window2     : {value[7]:02d}:{value[8]:02d}")
        print(f"       window3     : {value[9]:02d}:{value[10]:02d}")
    return value


def _format_days(days: int) -> str:
    if days & 0x80:
        return "cyclic"
    short_names = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
    active = [short_names[b] for b in range(7) if days & (1 << b)]
    return ",".join(active) if active else "off"


def _format_window(hour: int, minute: int) -> str:
    if hour == 0xff:
        return "off"
    if hour > 23 or minute > 59:
        return f"{hour:02x}:{minute:02x}"
    return f"{hour:02d}:{minute:02d}"


def _schedule_row(zone: int, record: bytes) -> list[str]:
    if not record or len(record) < 13 or record[0] != zone:
        return [str(zone), "read failed", "", "", "", "", ""]

    duration = record[1] * 60 + record[2]
    days = record[4]
    if days & 0x80:
        mode = f"cyclic +{record[13] - 0x80}d/{record[14] - 0xC0}d"
    else:
        mode = _format_days(days)
    windows = [_format_window(record[pos], record[pos + 1])
               for pos in (5, 7, 9, 11)]
    return [str(zone), f"{duration}m", mode, *windows]


def print_program_windows(records: dict[int, bytes]):
    headers = ["Zone", "Duration", "Days/Mode", "Window 1", "Window 2",
               "Window 3", "Window 4"]
    rows = [_schedule_row(zone, records.get(zone)) for zone in range(1, 5)]
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    def line(parts):
        return "  ".join(part.ljust(width)
                         for part, width in zip(parts, widths))

    print(line(headers))
    print(line(["-" * width for width in widths]))
    for row in rows:
        print(line(row))


async def read_program_windows(client, debug=False):
    records = {}
    for zone in range(1, 5):
        records[zone] = await read_schedule(client, zone, debug=debug,
                                            display=False)
    print_program_windows(records)
    return records


def modify_schedule(record: bytes, duration_minutes=None, hour=None,
                    minute=None, days_mask=None, cadence_days=None,
                    start_in_days=None):
    """
    Return a modified copy of a 20-byte schedule record with only the
    given fields changed, following the app's read-modify-write pattern.

    duration_minutes is the TOTAL duration in minutes (e.g. 110 for
    "1:50"), and is split into byte1 (hours) and byte2 (minutes 0-59) to
    match the on-wire format.

    cadence_days/start_in_days are CYCLIC-mode-only fields:
        byte13 = 0x80 + start_in_days
        byte14 = 0xC0 + cadence_days

    When days_mask is passed with bit7 CLEAR (entering/staying WEEKLY
    mode), bytes 11-14 are reset to their fixed weekly baseline (0xff,
    0x00, 0x80, 0x00) BEFORE applying any explicit cadence_days/
    start_in_days overrides. This matters: writing a weekly byte4 while
    leaving stale cyclic values in byte13/14 causes the device to reject
    the entire write and revert to its last valid stored state. If
    days_mask has bit7 SET (entering/staying CYCLIC), bytes 11-14 are
    left untouched.
    """
    out = bytearray(record)
    if duration_minutes is not None:
        out[1] = (duration_minutes // 60) & 0xFF
        out[2] = (duration_minutes % 60) & 0xFF
    if days_mask is not None:
        out[4] = days_mask & 0xFF
        if not (days_mask & 0x80):
            # Entering/staying WEEKLY: clear cyclic remnants, or the
            # device rejects the write outright.
            out[11] = 0xff
            out[12] = 0x00
            out[13] = 0x80
            out[14] = 0x00
    if hour is not None:
        out[5] = hour & 0xFF
    if minute is not None:
        out[6] = minute & 0xFF
    if start_in_days is not None:
        out[13] = (0x80 + start_in_days) & 0xFF
    if cadence_days is not None:
        out[14] = (0xC0 + cadence_days) & 0xFF
    return bytes(out)


async def write_schedule(client, zone: int, record: bytes, debug=False):
    """Save a (modified) 20-byte schedule record back to 20900101."""
    return await write_char(client, CHAR_COMMAND, record,
                            f"SAVE zone {zone} schedule", debug=debug)


async def find_device(scan_time: float, mac: str | None = None):
    """The GL6100 advertises infrequently, so scan patiently.

    Matching is always done by name (NAME_HINT); a saved MAC address is only
    used to disambiguate if more than one matching device is nearby.
    """
    print(f"[{ts()}] Scanning up to {scan_time:.0f}s for the valve...")
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
        print("       WARNING: weak signal; the link may drop mid-session.")
    return holder["dev"]


class _ConnectedClientCM:
    """Wraps an already-connected BleakClient so it can be used with
    `async with`, matching the interface of a freshly-constructed one."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc_info):
        await self.client.disconnect()


async def quick_connect(mac: str, timeout: float, disconnected_callback=None):
    """
    Connect directly by MAC address instead of running our own BleakScanner
    first.

    MEASURED FINDING: this does NOT reliably skip the wait for an
    advertisement - bleak/WinRT still needs to see one internally to resolve
    the address, bounded by `timeout`. On a cold start (no recent scan) this
    can simply fail after `timeout` seconds with BleakDeviceNotFoundError,
    adding pure overhead before the caller falls back to find_device().  It
    only helps when the device was already seen very recently (e.g. by a
    scan earlier in the same process). Opt-in only; see --quick-connect.
    """
    client = BleakClient(mac, timeout=timeout,
                         disconnected_callback=disconnected_callback,
                         services=[SERVICE_UUID],
                         winrt=dict(use_cached_services=True))
    await client.connect()
    return client


async def write_char(client, uuid, payload, label, debug=False):
    """Write, falling back to write-without-response."""
    last = None
    for with_response in (True, False):
        try:
            await client.write_gatt_char(uuid, payload, response=with_response)
            if debug:
                print(f"[{ts()}] {label}: wrote {hexdump(payload)}"
                      f"{'' if with_response else ' (no response)'}")
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
    print(f"[{ts()}] {label}: WRITE FAILED: {type(last).__name__}: {last}")
    print(f"[{ts()}] {REPAIR_HINT}")
    return False


async def wake_controller(client, debug=False):
    """Use the non-schedule status poll as a harmless keepalive/wake poke."""
    try:
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        if debug:
            print(f"[{ts()}] wake poll: wrote {hexdump(STATUS_POLL)}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] wake poll failed: {exc}")
        return False


async def enroll_controller(client, pin_timeout=30.0, debug=False):
    """Run the application-level enrollment exchange."""
    await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
    if debug:
        print(f"[{ts()}] enrollment probe: wrote {hexdump(STATUS_POLL)} to "
              "20900106")
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

    return await write_char(client, CHAR_PIN, pin_to_bytes(pin),
                            "enrollment PIN", debug=debug)


class StatusCache:
    """
    Holds the most recently NOTIFY-delivered 20900102 status frame.

    Windows/bleak can serve read_gatt_char() from a stale cached value once
    notifications are subscribed on a characteristic, instead of doing a
    fresh read - so status polling should prefer the live notification data
    over an explicit read.
    """

    def __init__(self):
        self.value = None
        self._event = asyncio.Event()

    def update(self, value):
        self.value = value
        self._event.set()

    async def wait_fresh(self, timeout=1.0):
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.value


async def read_status(client, label="status", debug=False, status_cache=None):
    # The app always writes 02 00 to 20900106 immediately before reading
    # status - without this poke, 20900102 reads back all zeros even while
    # a zone is genuinely running.
    value = None
    for attempt in range(3):
        try:
            await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts()}] status poll write failed: {exc}")

        value = None
        if status_cache is not None:
            value = await status_cache.wait_fresh(timeout=1.0)
        if value is None:
            try:
                value = bytes(await client.read_gatt_char(CHAR_STATUS))
            except Exception as exc:  # noqa: BLE001
                print(f"[{ts()}] {label}: read failed: {exc}")
                return None
        if any(value):
            break
        if attempt < 2:
            # A fresh write right before this read can race the device's
            # internal state update, producing a transient all-zero frame
            # that isn't a real authorization failure - give it one more try.
            await asyncio.sleep(0.4)

    if debug:
        print(f"[{ts()}] {label}: {hexdump(value)}")
    if not any(value):
        print("       (all zeros - device did not authorize this session)")
        print(f"[{ts()}] {REPAIR_HINT}")
    else:
        decode_status(value, debug=debug)
    return value


def decode_status(value: bytes, debug=False):
    """
    Print a human-friendly breakdown of the 20900102 status frame.

    byte0 is 0xff while idle. Active frames seen from different controller
    firmware revisions use 0xf0-0xf3 for one zone. In compact multi-zone
    frames, the high and low nibbles are zero-based zone indices, with timers
    in bytes 2-3 and 5-6. During pair shutdown, 0x0f, 0x1f, 0x2f, and 0x3f
    identify surviving zones 1-4 respectively, with time at bytes 5-6.

    bytes[2:3] count down in whole seconds while active; bytes[2:4]
    together are [minutes][seconds] remaining.

    byte10 is not a reliable battery indicator - it changes too quickly
    between short runs to be a battery percentage. The actual battery
    indicator byte, if any, is unidentified.
    """
    if len(value) < 20:
        return
    
    status_byte = value[0]
    zone_times = decode_active_zones(value)
    active_zones = None if zone_times is None else [zone for zone, _ in zone_times]

    if active_zones == []:
        status_str = "IDLE"
    elif active_zones is not None:
        status_str = "ZONES " + ",".join(map(str, active_zones)) + " RUNNING"
    else:
        status_str = f"ACTIVE (unknown status byte: {status_byte:#04x})"
    
    # Print formatted output with proper alignment
    print(f"       ╔{'═' * 40}╗")
    print(f"       ║ STATUS: {status_str:<31}║")
    print(f"       ╠{'═' * 40}╣")
    
    if zone_times:
        for zone, total_seconds in zone_times:
            mins, secs = divmod(total_seconds, 60)
            time_str = f"Zone {zone}: {mins:2d}m {secs:02d}s ({total_seconds:3d}s)"
            print(f"       ║ {time_str:<39}║")
    
    print(f"       ╚{'═' * 40}╝")
    
    if debug:
        print(f"       [DEBUG] Raw hex: {hexdump(value)}")
        print(f"       [DEBUG] byte0={value[0]:#04x}, bytes[2:4]={value[2]:#04x} {value[3]:#04x}")
        print(f"       [DEBUG] byte10={value[10]:#04x} (not a confirmed battery indicator)")


async def read_battery(client):
    """Read and display all 20 bytes of status with position labels."""
    try:
        await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] battery poll write failed: {exc}")

    try:
        value = bytes(await client.read_gatt_char(CHAR_STATUS))
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] battery read failed: {exc}")
        return None

    print(f"[{ts()}] Battery/Status frame (20 bytes):")
    print(f"       Full hex: {hexdump(value)}")
    if not any(value):
        print("       (all zeros - device did not authorize this session)")
        print(f"[{ts()}] {REPAIR_HINT}")
    print("\n       Position breakdown (all values in hex):")
    for i, b in enumerate(value):
        print(f"         byte{i:2d}: 0x{b:02x}  ({b:3d} decimal)")
    return value


async def close_zone(client, zone, debug=False, status_cache=None):
    """
    Close a zone and poll status until the device reflects it as closed.

    A single status read taken right after the CLOSE write can still show
    the zone as running - the device needs a moment to update - so poll a
    few times before reporting the final state.
    """
    ok = await write_char(client, CHAR_VALVE, build_close_payload(zone),
                          f"CLOSE zone {zone}", debug=debug)
    if not ok:
        return None

    value = None
    for attempt in range(10):
        await asyncio.sleep(0.5 if attempt else 1.0)
        try:
            await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts()}] status read failed: {exc}")
            break
        value = None
        if status_cache is not None:
            value = await status_cache.wait_fresh(timeout=1.0)
            if value is None:
                continue
        else:
            try:
                value = bytes(await client.read_gatt_char(CHAR_STATUS))
            except Exception as exc:  # noqa: BLE001
                print(f"[{ts()}] status read failed: {exc}")
                break
        if not value or value[0] == 0xff or (value[0] & 0x0f) + 1 != zone:
            break

    if value is None:
        print(f"[{ts()}] No fresh status received after close; try 'status' again.")
        return None

    if debug:
        print(f"[{ts()}] status after close: {hexdump(value)}")
    if not any(value):
        print("       (all zeros - device did not authorize this session)")
        print(f"[{ts()}] {REPAIR_HINT}")
    else:
        decode_status(value, debug=debug)
    return value


INTERACTIVE_HELP = "\n".join((
    "Commands:",
    "  open <zone> [minutes]         Open a zone (defaults from --zone/--minutes)",
    "  close <zone>                  Close a zone",
    "  status                        Read current status",
    "  programs / windows            Show all zone program windows",
    "  battery                       Read raw status/battery bytes",
    "  schedule <zone>                Read a zone's schedule",
    "  schedule <zone> key=value ...  Modify a zone's schedule, then save it.",
    "                                  keys: duration (total minutes), hour,",
    "                                  minute, days (bitmask, e.g. 0x7f for all",
    "                                  days, or 0x80|N for cyclic mode), cadence",
    "                                  (cyclic: run every N days), start-in",
    "                                  (cyclic: first run in N days)",
    "  seasonal <percent>            Set device-wide seasonal adjustment",
    "  rainoff <days>                 Set device-wide rain-off (0 clears it)",
    "  help                           Show this help",
    "  quit / exit                    End the session",
    "",
))


def _stdin_reader(line_queue):
    """Background thread feeding stdin lines to the interactive loop."""
    while True:
        try:
            line = input()
        except EOFError:
            line_queue.put(None)
            return
        line_queue.put(line)


_IDLE_TIMEOUT = object()  # sentinel distinguishing a timeout from a blank line


async def _next_command(line_queue, idle_timeout):
    """Wait up to idle_timeout seconds for the next stdin line."""
    try:
        return await asyncio.to_thread(line_queue.get, True, idle_timeout)
    except queue.Empty:
        return _IDLE_TIMEOUT


def _parse_schedule_kv(tokens):
    """Parse key=value tokens for the 'schedule' command into modify_schedule kwargs."""
    kv = {}
    for token in tokens:
        if "=" not in token:
            print(f"ignoring unrecognized token: {token}")
            continue
        key, _, val = token.partition("=")
        kv[key.strip().lower()] = val.strip()

    kwargs = {}
    try:
        if "duration" in kv:
            kwargs["duration_minutes"] = int(kv["duration"])
        if "hour" in kv:
            kwargs["hour"] = int(kv["hour"])
        if "minute" in kv:
            kwargs["minute"] = int(kv["minute"])
        if "days" in kv:
            kwargs["days_mask"] = int(kv["days"], 0)
        if "cadence" in kv:
            kwargs["cadence_days"] = int(kv["cadence"])
        if "start-in" in kv:
            kwargs["start_in_days"] = int(kv["start-in"])
    except ValueError as exc:
        raise ValueError(f"invalid value: {exc}") from exc
    return kwargs


async def interactive_session(client, args):
    """
    Keep this BLE connection open and take repeated commands from stdin,
    mirroring how the official app avoids reconnecting between actions.

    Type 'help' for the full command list. The session ends after
    idle_timeout seconds of no input (minimum 60s).
    """
    idle_timeout = max(args.idle_timeout, 60.0)
    line_queue = queue.Queue()
    threading.Thread(target=_stdin_reader, args=(line_queue,), daemon=True).start()

    print("Connected. Type 'help' for the list of commands.")
    while True:
        print("> ", end="", flush=True)
        line = await _next_command(line_queue, idle_timeout)
        if line is None:
            break
        if line is _IDLE_TIMEOUT:
            print(f"\n[{ts()}] No input for {idle_timeout:.0f}s; ending session.")
            break

        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()

        if cmd in ("quit", "exit"):
            break

        elif cmd in ("help", "?"):
            print(INTERACTIVE_HELP)

        elif cmd == "open":
            zone = int(parts[1]) if len(parts) > 1 else args.zone
            minutes = int(parts[2]) if len(parts) > 2 else args.minutes
            if not 1 <= zone <= 4:
                print("zone must be 1-4")
                continue
            payload = build_open_payload(zone, minutes)
            await write_char(client, CHAR_VALVE, payload,
                             f"OPEN zone {zone} for {minutes} min", debug=args.debug)
            await asyncio.sleep(1.0)
            await read_status(client, "status after open", debug=args.debug,
                              status_cache=args.status_cache)

        elif cmd == "close":
            zone = int(parts[1]) if len(parts) > 1 else args.zone
            if not 1 <= zone <= 4:
                print("zone must be 1-4")
                continue
            await close_zone(client, zone, debug=args.debug,
                             status_cache=args.status_cache)

        elif cmd == "status":
            await read_status(client, "status", debug=args.debug,
                              status_cache=args.status_cache)

        elif cmd in ("programs", "windows"):
            await read_program_windows(client, debug=args.debug)

        elif cmd == "battery":
            await read_battery(client)

        elif cmd == "schedule":
            if len(parts) < 2:
                print("Usage: schedule <zone> [key=value ...]")
                continue
            try:
                zone = int(parts[1])
            except ValueError:
                print("zone must be 1-4")
                continue
            if not 1 <= zone <= 4:
                print("zone must be 1-4")
                continue
            record = await read_schedule(client, zone, debug=args.debug)
            if record is None or len(parts) == 2:
                continue
            try:
                kwargs = _parse_schedule_kv(parts[2:])
            except ValueError as exc:
                print(exc)
                continue
            if not kwargs:
                continue
            new_record = modify_schedule(record, **kwargs)
            await write_schedule(client, zone, new_record, debug=args.debug)
            await asyncio.sleep(0.5)
            await read_schedule(client, zone, label_prefix="confirm: ",
                               debug=args.debug)

        elif cmd == "seasonal":
            if len(parts) < 2:
                print("Usage: seasonal <percent>")
                continue
            try:
                percent = int(parts[1])
            except ValueError:
                print("percent must be an integer")
                continue
            payload = build_seasonal_payload(percent)
            await write_char(client, CHAR_VALVE, payload,
                             f"SEASONAL ADJUSTMENT {percent}%", debug=args.debug)

        elif cmd == "rainoff":
            if len(parts) < 2:
                print("Usage: rainoff <days> (0 clears it)")
                continue
            try:
                days = int(parts[1])
            except ValueError:
                print("days must be an integer")
                continue
            payload = build_rainoff_payload(days)
            await write_char(client, CHAR_VALVE, payload,
                             f"RAIN OFF {days} days", debug=args.debug)

        else:
            print("Unknown command. Type 'help' for the list of commands.")

    print(f"[{ts()}] Interactive session ended.")
    return 0


async def run(args):
    disconnected = asyncio.Event()

    def on_disconnect(_c):
        print(f"\n[{ts()}] *** peer disconnected ***")
        disconnected.set()

    # NOTE: connecting directly by MAC still requires Windows to see a fresh
    # advertisement internally - measured to NOT reliably skip that wait, and
    # it can waste --quick-timeout seconds before falling back on a cold
    # start. So this is opt-in only (--quick-connect), not the default path.
    client_cm = None
    if args.mac and args.quick_connect:
        try:
            print(f"[{ts()}] Trying quick connect to {args.mac} "
                  "(skipping scan)...")
            quick_client = await quick_connect(args.mac, args.quick_timeout,
                                               disconnected_callback=on_disconnect)
            print(f"[{ts()}] Quick connect succeeded (skipped scan).")
            client_cm = _ConnectedClientCM(quick_client)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts()}] Quick connect failed: {type(exc).__name__}: "
                  f"{exc}. Falling back to scan.")

    if client_cm is None:
        device = await find_device(args.scan_time, mac=args.mac)
        if device is None:
            print("Valve not found. It advertises infrequently - try again, or "
                  "press a button on the unit to wake it.")
            return 1
        client_cm = BleakClient(device, timeout=30.0,
                                disconnected_callback=on_disconnect,
                                services=[SERVICE_UUID],
                                winrt=dict(use_cached_services=True))

    status_cache = StatusCache()
    args.status_cache = status_cache

    def on_notify(sender, data):
        if args.debug:
            print(f"[{ts()}] NOTIFY {sender}: {hexdump(bytes(data))}")

    def on_status_notify(sender, data):
        if args.debug:
            print(f"[{ts()}] NOTIFY {sender}: {hexdump(bytes(data))}")
        status_cache.update(bytes(data))

    try:
        async with client_cm as client:
            print(f"[{ts()}] Connected.")

            for uuid, callback in ((CHAR_COMMAND, on_notify),
                                   (CHAR_STATUS, on_status_notify)):
                try:
                    await client.start_notify(uuid, callback)
                except Exception as exc:  # noqa: BLE001
                    print(f"[{ts()}] notify subscribe failed on {uuid[:8]}: {exc}")

            if args.enroll:
                success = await enroll_controller(client, args.pin_timeout,
                                                  debug=args.debug)
                return 0 if success else 1

            # 1. Wake/refresh the device with the non-schedule status poll.
            # Do not write 01 02 to CHAR_COMMAND here: that characteristic is
            # also used for schedule records, and 01 02 can be interpreted as
            # zone 1 duration = 2h00m on this controller.
            await wake_controller(client, debug=args.debug)
            await asyncio.sleep(0.5)

            # 2. Optional application-level PIN register write. This is separate
            # from BLE Passkey Entry pairing and is not needed for normal control.
            if args.send_pin:
                if not args.pin:
                    print(f"[{ts()}] --send-pin needs --pin")
                else:
                    await write_char(client, CHAR_PIN, pin_to_bytes(args.pin), "pin",
                                     debug=args.debug)
                    await asyncio.sleep(0.5)

            if args.debug:
                try:
                    stored = bytes(await client.read_gatt_char(CHAR_PIN))
                    print(f"[{ts()}] PIN register 20900105 = {hexdump(stored)}"
                          f"  -> PIN {''.join(str(b) for b in stored)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[{ts()}] could not read PIN register: {exc}")

            if args.interactive:
                return await interactive_session(client, args)

            if args.read_schedule or args.write_schedule:
                zone = args.read_schedule or args.write_schedule
                record = await read_schedule(client, zone, debug=args.debug)
                if record is None:
                    return 1

                if args.write_schedule:
                    if not any(v is not None for v in
                              (args.set_duration, args.set_hour, args.set_minute,
                               args.set_days, args.set_cadence, args.set_start_in)):
                        print(f"[{ts()}] --write-schedule needs at least one of "
                              f"--set-duration / --set-hour / --set-minute / "
                              f"--set-days / --set-cadence / --set-start-in")
                        return 1
                    new_record = modify_schedule(
                        record, duration_minutes=args.set_duration,
                        hour=args.set_hour, minute=args.set_minute,
                        days_mask=args.set_days, cadence_days=args.set_cadence,
                        start_in_days=args.set_start_in)
                    await write_schedule(client, zone, new_record, debug=args.debug)
                    await asyncio.sleep(0.5)
                    await read_schedule(client, zone, label_prefix="confirm: ",
                                       debug=args.debug)
                return 0

            if args.raw is not None:
                payload = bytes.fromhex(args.raw)
                await write_char(client, CHAR_VALVE, payload, "RAW", debug=args.debug)
                await asyncio.sleep(1.0)
                await read_status(client, "status after raw", debug=args.debug,
                                  status_cache=args.status_cache)
                if args.hold:
                    await asyncio.sleep(args.hold)
                    await read_status(client, "status during hold", debug=args.debug,
                                      status_cache=args.status_cache)
                return 0

            if args.status:
                if args.hold:
                    await asyncio.sleep(args.hold)
                    await read_status(client, "status after hold", debug=args.debug,
                                      status_cache=args.status_cache)
                else:
                    await read_status(client, "status", debug=args.debug,
                                      status_cache=args.status_cache)
                return 0

            if args.programs:
                await read_program_windows(client, debug=args.debug)
                return 0

            if args.battery:
                await read_battery(client)
                return 0

            if args.set_seasonal is not None:
                payload = build_seasonal_payload(args.set_seasonal)
                await write_char(client, CHAR_VALVE, payload,
                                 f"SEASONAL ADJUSTMENT {args.set_seasonal}%",
                                 debug=args.debug)
                return 0

            if args.set_rainoff is not None:
                payload = build_rainoff_payload(args.set_rainoff)
                await write_char(client, CHAR_VALVE, payload,
                                 f"RAIN OFF {args.set_rainoff} days",
                                 debug=args.debug)
                return 0

            # 3. Drive the valve.
            if args.open:
                payload = build_open_payload(args.zone, args.minutes)
                await write_char(client, CHAR_VALVE, payload,
                                 f"OPEN zone {args.zone} for {args.minutes} min",
                                 debug=args.debug)
                await asyncio.sleep(1.0)
                await read_status(client, "status after open", debug=args.debug,
                                  status_cache=args.status_cache)

                if args.hold:
                    print(f"[{ts()}] Holding {args.hold}s...")
                    await asyncio.sleep(args.hold)
                    await read_status(client, "status during hold", debug=args.debug,
                                      status_cache=args.status_cache)

            if args.close or (args.open and args.hold):
                await close_zone(client, args.zone, debug=args.debug,
                                 status_cache=args.status_cache)
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] Connection/session failed: {type(exc).__name__}: {exc}")
        print(f"[{ts()}] {REPAIR_HINT}")
        return 1

    print(f"[{ts()}] Done.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Control a Galcon GL6100 valve")
    parser.add_argument("--pin", help="4-digit PIN (defaults to the value saved "
                                     "via --set-pin, if any)")
    parser.add_argument("--set-pin", metavar="PIN",
                        help=f"Persist a 4-digit PIN to {CONFIG_PATH} so "
                             "future runs don't need --pin")
    parser.add_argument("--mac", help="Controller BLE MAC address (defaults "
                                     "to the value saved via --set-mac, if "
                                     "any). Optional: scanning by name works "
                                     "without it.")
    parser.add_argument("--set-mac", metavar="MAC",
                        help=f"Persist the controller's MAC address to "
                             f"{CONFIG_PATH} to speed up/disambiguate scans")
    parser.add_argument("--send-pin", action="store_true",
                        help="Also write the PIN to 20900105. Not needed.")
    parser.add_argument("--zone", type=int, default=1, choices=(1, 2, 3, 4),
                        help="Zone number (1-4). Zone 4 follows the pattern "
                             "but was not physically tested.")
    parser.add_argument("--minutes", type=int, default=1,
                        help="Duration in whole minutes for --open")
    parser.add_argument("--open", nargs="?", type=int, const=0, default=None,
                        metavar="ZONE",
                        help="Open the zone. Optionally give a zone number "
                             "directly, e.g. --open 2, instead of --zone")
    parser.add_argument("--close", nargs="?", type=int, const=0, default=None,
                        metavar="ZONE",
                        help="Close the zone. Optionally give a zone number "
                             "directly, e.g. --close 2, instead of --zone")
    parser.add_argument("--status", action="store_true",
                        help="Wake and read status only")
    parser.add_argument("--programs", "--windows", action="store_true",
                        dest="programs",
                        help="Read all zones and show their four programmed "
                             "windows in a table")
    parser.add_argument("--enroll", action="store_true",
                        help="Run application-level enrollment: wait for the "
                             "displayed PIN and submit it")
    parser.add_argument("--battery", action="store_true",
                        help="Wake and read all status bytes with position "
                             "labels to help identify battery byte")
    parser.add_argument("--interactive", action="store_true",
                        help="Keep the connection open and accept repeated "
                             "commands from stdin instead of reconnecting "
                             "for each one. Type 'help' in the session for "
                             "the full command list.")
    parser.add_argument("--idle-timeout", type=float, default=120.0,
                        help="Seconds of no input before an --interactive "
                             "session ends (minimum 60)")
    parser.add_argument("--debug", action="store_true",
                        help="Show raw hex dumps and debug details")
    parser.add_argument("--set-seasonal", type=int, metavar="PERCENT",
                        help="Set device-wide seasonal adjustment percent "
                             "(e.g. 110 for 110%%). Applies to all zones.")
    parser.add_argument("--set-rainoff", type=int, metavar="DAYS",
                        help="Set device-wide Rain Off duration in days "
                             "(0 clears/cancels it). Suspends all "
                             "programs.")
    parser.add_argument("--raw", help="Hex payload to write to 20900103, "
                                     "e.g. 00010000000000")
    parser.add_argument("--read-schedule", type=int, choices=(1, 2, 3, 4),
                        metavar="ZONE",
                        help="Read and print zone N's schedule record")
    parser.add_argument("--write-schedule", type=int, choices=(1, 2, 3, 4),
                        metavar="ZONE",
                        help="Read zone N's schedule, apply --set-* changes, "
                             "save, then re-read to confirm")
    parser.add_argument("--set-duration", type=int, metavar="MINUTES",
                        help="New TOTAL duration in minutes, e.g. 110 for "
                             "1h50m (split into hours/minutes bytes "
                             "automatically)")
    parser.add_argument("--set-hour", type=int, metavar="HOUR",
                        help="New start hour, 0-23")
    parser.add_argument("--set-minute", type=int, metavar="MINUTE",
                        help="New start minute, 0-59")
    parser.add_argument("--set-days", type=lambda s: int(s, 0),
                        metavar="BITMASK",
                        help="New day-of-week bitmask, e.g. 0x7f for all "
                             "days. Mapping (Sunday-first): "
                             "bit0=Sun bit1=Mon bit2=Tue bit3=Wed bit4=Thu "
                             "bit5=Fri bit6=Sat. Bit7 is the weekly/cyclic "
                             "mode flag - pass 0x80 to enter cyclic mode.")
    parser.add_argument("--set-cadence", type=int, metavar="DAYS",
                        help="CYCLIC mode: run every N days (byte14 = "
                             "0xC0 + N)")
    parser.add_argument("--set-start-in", type=int, metavar="DAYS",
                        help="CYCLIC mode: first run in N days (byte13 = "
                             "0x80 + N)")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="Seconds to stay open before closing")
    parser.add_argument("--pin-timeout", type=float, default=30.0,
                        help="Seconds to wait for the displayed enrollment PIN")
    parser.add_argument("--scan-time", type=float, default=60.0)
    parser.add_argument("--quick-connect", action="store_true",
                        help="Try connecting directly by MAC before "
                             "scanning. Only useful if the controller was "
                             "seen very recently; often just wastes "
                             "--quick-timeout seconds on a cold start. Off "
                             "by default.")
    parser.add_argument("--quick-timeout", type=float, default=12.0,
                        help="Seconds to try --quick-connect before falling "
                             "back to a full scan.")
    args = parser.parse_args()

    # --open/--close accept an optional zone number directly; fold it into
    # --zone and reduce them back to plain booleans.
    for flag in ("open", "close"):
        value = getattr(args, flag)
        if value is not None and value != 0:
            args.zone = value
        setattr(args, flag, value is not None)
    if not 1 <= args.zone <= 4:
        parser.error("zone must be 1-4")

    if args.set_pin is not None:
        if not (args.set_pin.isdigit() and len(args.set_pin) == 4):
            parser.error("--set-pin must be exactly 4 digits")
        save_pin(args.set_pin)
        if not any((args.open, args.close, args.status, args.programs,
                args.enroll, args.battery, args.raw,
                args.read_schedule, args.write_schedule,
                    args.set_seasonal is not None, args.set_rainoff is not None)):
            return 0

    if args.set_mac is not None:
        save_mac(args.set_mac)
        if not any((args.open, args.close, args.status, args.programs,
                args.enroll, args.battery, args.raw,
                args.read_schedule, args.write_schedule,
                    args.set_seasonal is not None, args.set_rainoff is not None)):
            return 0

    if not args.pin:
        args.pin = load_saved_pin()

    if not args.mac:
        args.mac = load_saved_mac()

    if args.pin and not (args.pin.isdigit() and len(args.pin) == 4):
        parser.error("--pin must be exactly 4 digits")
    if args.idle_timeout < 60:
        parser.error("--idle-timeout must be at least 60 seconds")
    if not (args.open or args.close or args.status or args.programs
            or args.enroll or args.battery or args.raw or args.read_schedule
            or args.write_schedule or args.interactive
            or args.set_seasonal is not None or args.set_rainoff is not None):
        parser.error("choose one of --open, --close, --status, --battery, --raw, "
                     "--programs, --read-schedule, --write-schedule, "
                     "--interactive, --set-seasonal or --set-rainoff")

    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
