"""
Minimal btsnoop_hci.log parser focused on Bluetooth LE ATT traffic to/from
the Galcon valve, so we don't need Wireshark to read the capture.

btsnoop file format (RFC 1761-ish, used by Android/BlueZ):
    file header (16 bytes):
        identification pattern "btsnoop\0"  (8 bytes)
        version number (4 bytes, big endian) = 1
        datalink type   (4 bytes, big endian) = 1002 for H4
    then a sequence of records:
        original length   (4 bytes BE)
        included length   (4 bytes BE)
        flags             (4 bytes BE)  bit0: 0=sent,1=received
        drops             (4 bytes BE)
        timestamp usecs   (8 bytes BE)  microseconds since 2000-01-01, offset
        packet data       (included length bytes)

The packet data is an HCI H4 frame: first byte is the packet type
    0x01 HCI Command
    0x02 ACL Data (this is what carries L2CAP/ATT for BLE)
    0x03 SCO Data
    0x04 HCI Event

We only care about ACL Data packets (0x02) containing L2CAP CID 0x0004
(ATT channel), and specifically ATT opcodes:
    0x01 Error Response
    0x0b Write Response
    0x12 Write Request
    0x13 Handle Value Confirmation
    0x1b Handle Value Notification
    0x1d Handle Value Indication
    0x52 Write Command (no response)
    0x0a Read Request
    0x0b Read Response

Usage
-----
    python parse_snoop.py bugreport\\FS\\data\\log\\bt\\btsnoop_hci.log
    python parse_snoop.py <path> --handle 0x000d
    python parse_snoop.py <path> --all-att
    python parse_snoop.py <path> --all-l2cap
    python parse_snoop.py <path> --hci-events
    python parse_snoop.py <path> --advertising
"""

import argparse
import struct
import sys
from datetime import datetime, timedelta, timezone

BTSNOOP_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

ATT_OPCODES = {
    0x01: "Error Response",
    0x02: "Exchange MTU Request",
    0x03: "Exchange MTU Response",
    0x04: "Find Information Request",
    0x05: "Find Information Response",
    0x06: "Find By Type Value Request",
    0x07: "Find By Type Value Response",
    0x08: "Read By Type Request",
    0x09: "Read By Type Response",
    0x0a: "Read Request",
    0x0b: "Read Response",
    0x0c: "Read Blob Request",
    0x0d: "Read Blob Response",
    0x0e: "Read Multiple Request",
    0x0f: "Read Multiple Response",
    0x10: "Read by Group Type Request",
    0x11: "Read by Group Type Response",
    0x12: "Write Request",
    0x13: "Write Response",
    0x16: "Prepare Write Request",
    0x17: "Prepare Write Response",
    0x18: "Execute Write Request",
    0x19: "Execute Write Response",
    0x1b: "Handle Value Notification",
    0x1d: "Handle Value Indication",
    0x1e: "Handle Value Confirmation",
    0x52: "Write Command",
    0xd2: "Signed Write Command",
}

WRITE_LIKE = {0x12, 0x52, 0xd2}
NOTIFY_LIKE = {0x1b, 0x1d}
READ_RESPONSE_LIKE = {0x0b, 0x0d}


def hexdump(data: bytes) -> str:
    if not data:
        return "<empty>"
    h = " ".join(f"{b:02x}" for b in data)
    a = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{h}  |{a}|"


def iter_records(path):
    with open(path, "rb") as f:
        header = f.read(16)
        if len(header) < 16 or header[:8] != b"btsnoop\x00":
            raise ValueError("Not a btsnoop file (bad magic)")
        version, datalink = struct.unpack(">II", header[8:16])

        while True:
            rec_header = f.read(24)
            if len(rec_header) < 24:
                break
            orig_len, incl_len, flags, drops, ts64 = struct.unpack(
                ">IIIIq", rec_header)
            data = f.read(incl_len)
            if len(data) < incl_len:
                break
            direction = "RX" if (flags & 0x01) else "TX"
            timestamp = BTSNOOP_EPOCH + timedelta(microseconds=ts64)
            yield timestamp, direction, data


def parse_acl_att(data: bytes):
    """
    data is the H4-framed packet (first byte = packet type).
    Returns (att_opcode, att_handle_or_none, att_payload) or None if this
    packet is not an ATT PDU on an ACL link.
    """
    if not data:
        return None
    pkt_type = data[0]
    if pkt_type != 0x02:  # only ACL Data carries L2CAP/ATT
        return None

    parsed = parse_acl_l2cap(data)
    if parsed is None:
        return None
    _handle, cid, att = parsed
    if cid != 0x0004:
        return None
    if not att:
        return None

    opcode = att[0]
    handle = None
    payload = att[1:]
    if opcode in WRITE_LIKE or opcode in NOTIFY_LIKE and len(att) >= 3:
        handle = struct.unpack_from("<H", att, 1)[0]
        payload = att[3:]
    elif opcode in READ_RESPONSE_LIKE:
        payload = att[1:]

    return opcode, handle, payload


def parse_acl_l2cap(data: bytes):
    """Return (ACL handle, L2CAP CID, payload) for an ACL packet."""
    if not data or data[0] != 0x02:
        return None

    body = data[1:]
    if len(body) < 4:
        return None
    # ACL header: handle+flags (2 bytes LE), data total length (2 bytes LE)
    handle_flags, acl_len = struct.unpack_from("<HH", body, 0)
    l2cap = body[4:4 + acl_len]
    if len(l2cap) < 4:
        return None
    # L2CAP header: length (2 bytes LE), channel id (2 bytes LE)
    l2_len, cid = struct.unpack_from("<HH", l2cap, 0)
    if len(l2cap) < 4 + l2_len:
        return None
    att = l2cap[4:4 + l2_len]
    return handle_flags & 0x0fff, cid, att


def parse_le_advertising(data: bytes):
    """Return advertising reports from an HCI LE Meta Event."""
    if not data or len(data) < 4 or data[0:2] != b"\x04\x3e":
        return []

    payload = data[3:]
    if len(payload) < 2:
        return []
    subevent, report_count = payload[:2]
    if subevent not in (0x02, 0x0d):
        return []

    reports = []
    offset = 2
    for _ in range(report_count):
        if subevent == 0x02:
            if len(payload) < offset + 11:
                break
            event_type = payload[offset]
            address = payload[offset + 2:offset + 8]
            data_length = payload[offset + 8]
            data_start = offset + 9
            data_end = data_start + data_length
            if len(payload) < data_end + 1:
                break
            reports.append((event_type, address, payload[data_start:data_end]))
            offset = data_end + 1
        else:
            if len(payload) < offset + 26:
                break
            event_type = int.from_bytes(payload[offset:offset + 2], "little")
            address = payload[offset + 3:offset + 9]
            data_length = payload[offset + 23]
            data_start = offset + 24
            data_end = data_start + data_length
            if len(payload) < data_end:
                break
            reports.append((event_type, address, payload[data_start:data_end]))
            offset = data_end
    return reports


def main():
    parser = argparse.ArgumentParser(
        description="Extract ATT traffic from a btsnoop_hci.log")
    parser.add_argument("path", help="Path to btsnoop_hci.log")
    parser.add_argument("--handle", help="Filter to this ATT handle, e.g. 0x000d")
    parser.add_argument("--all-att", action="store_true",
                        help="Show every ATT PDU, not just writes/notifies")
    parser.add_argument("--all-l2cap", action="store_true",
                        help="Show non-ATT L2CAP packets, including SMP")
    parser.add_argument("--hci-events", action="store_true",
                        help="Show HCI event packets, including LE events")
    parser.add_argument("--advertising", action="store_true",
                        help="Show decoded LE advertising reports")
    args = parser.parse_args()

    handle_filter = int(args.handle, 0) if args.handle else None

    count = 0
    shown = 0
    for timestamp, direction, data in iter_records(args.path):
        count += 1
        if args.hci_events and data and data[0] == 0x04:
            event_code = data[1] if len(data) > 1 else None
            payload = data[3:] if len(data) > 2 else b""
            ts_s = timestamp.strftime("%H:%M:%S.%f")[:-3]
            event_s = (f"0x{event_code:02x}" if event_code is not None
                       else "<none>")
            print(f"[{ts_s}] {direction} HCI event {event_s:<6} "
                  f"{hexdump(payload)}")
            shown += 1
            continue

        if args.advertising:
            for event_type, address, payload in parse_le_advertising(data):
                ts_s = timestamp.strftime("%H:%M:%S.%f")[:-3]
                address_s = ":".join(f"{b:02x}" for b in address)
                print(f"[{ts_s}] {direction} LE advertising "
                      f"event=0x{event_type:04x} address={address_s} "
                      f"{hexdump(payload)}")
                shown += 1
            if parse_le_advertising(data):
                continue

        if args.all_l2cap:
            l2cap = parse_acl_l2cap(data)
            if l2cap is not None and l2cap[1] != 0x0004:
                handle, cid, payload = l2cap
                ts_s = timestamp.strftime("%H:%M:%S.%f")[:-3]
                print(f"[{ts_s}] {direction} L2CAP handle=0x{handle:04x} "
                      f"cid=0x{cid:04x}  {hexdump(payload)}")
                shown += 1
                continue

        parsed = parse_acl_att(data)
        if parsed is None:
            continue
        opcode, handle, payload = parsed

        if handle_filter is not None and handle != handle_filter:
            continue

        interesting = args.all_att or opcode in (
            WRITE_LIKE | NOTIFY_LIKE | {0x01}
        )
        if not interesting:
            continue

        shown += 1
        name = ATT_OPCODES.get(opcode, f"0x{opcode:02x}")
        handle_s = f"handle=0x{handle:04x}" if handle is not None else ""
        ts_s = timestamp.strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts_s}] {direction} {name:<28} {handle_s}  "
              f"{hexdump(payload)}")

    print(f"\n{count} total packets scanned, {shown} ATT PDU(s) shown.")
    if shown == 0:
        print("\nNo matching ATT traffic found. Possible causes:")
        print("  * The Galcon app session did not happen while logging was")
        print("    active (log is a ring buffer - capture close to the app")
        print("    session).")
        print("  * Wrong file / no BLE activity in this capture window.")
        print("  * Try --all-att to see every ATT PDU without filtering.")


if __name__ == "__main__":
    sys.exit(main() or 0)
