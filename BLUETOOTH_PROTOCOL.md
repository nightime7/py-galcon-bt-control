# Galcon GL6100 Bluetooth Protocol Reference

This document describes the reverse-engineered BLE protocol used by the Galcon
GL6100 / 6100BT DC4 irrigation controller. It is based on Android HCI captures,
live controller tests, and the behavior implemented in `control_galcon.py`.

This is not an official Galcon specification. Values may differ across hardware
or firmware revisions.

## Confidence Labels

| Label | Meaning |
|---|---|
| Confirmed | Matched isolated official-app captures or physical controller tests. |
| Observed | Seen in captures, but not isolated across enough values to prove every interpretation. |
| Compatibility | Accepted by the current decoder based on field patterns and tests. |
| Unknown | No reliable meaning has been established. Preserve the value when writing. |

## Transport

| Property | Value |
|---|---|
| BLE service | `20900100-bdee-493a-aa74-a8137c9d43f0` |
| ATT MTU | 23 bytes observed |
| Maximum protocol record | 20 bytes |
| Advertisement name | Begins with `GL6100` |
| Advertisement behavior | Infrequent; allow a 45-60 second scan window |
| Pairing | Initial controller pairing is performed with the official app |

Use characteristic UUIDs in normal BLE APIs. Handles below are useful when
reading raw HCI/ATT captures and may vary by firmware.

## GATT Characteristics

| Short UUID | Declaration handle | Value handle | Properties | Length | Purpose | Confidence |
|---|---:|---:|---|---:|---|---|
| `20900101` | `0x000d` | `0x000e` | Read, write, write without response, notify | 20 | Schedule read/write and command notifications | Confirmed |
| `20900102` | `0x0010` | `0x0011` | Read, notify | 20 | Runtime status | Confirmed |
| `20900103` | `0x0013` | `0x0014` | Read, write, write without response | 20 | Valve and device-wide commands | Confirmed |
| `20900104` | `0x0015` | `0x0016` | Read, write, write without response | 8 observed | Date/time synchronization | Observed |
| `20900105` | `0x0017` | `0x0018` | Read, write, write without response | 4 | Application-level PIN register | Confirmed |
| `20900106` | `0x0019` | `0x001a` | Read, write, write without response | 2 | Status poll and schedule selection | Confirmed |

All UUIDs use the suffix `-bdee-493a-aa74-a8137c9d43f0`.

## Safe Session Sequence

Use this order for a normal control session:

1. Scan for an advertisement whose name contains `GL6100`.
2. Connect and restrict service discovery to `20900100...` when supported.
3. Subscribe to notifications on `20900101` and `20900102`.
4. Write `02 00` to `20900106` with response requested.
5. Wait about 500 ms for the controller state to settle.
6. Perform status, valve, or schedule operations.
7. Disconnect when no more operations are needed.

A write with response is preferred. The implementation retries without response
when the platform rejects a response write.

> [!CAUTION]
> Never use `01 02` on `20900101` as a wake command. That characteristic is the
> schedule-save pipe. A short write can be interpreted as a partial zone record
> and has overwritten zone 1's duration in testing. Use `02 00` on `20900106`.

## PIN Register

The four-digit PIN is encoded as four raw digit values, not ASCII and not BCD.
It is an application-level register separate from BLE pairing.

| PIN | Payload to `20900105` |
|---|---|
| `1234` | `01 02 03 04` |
| `2178` | `02 01 07 08` |

The official app writes this register during its session. Current physical
control tests do not require a PIN write after the controller has been paired,
but firmware behavior may vary. Reading this register has returned both a PIN
and all zeros in different sessions; do not depend on it as credential storage.

## Valve Control

Valve commands are 20-byte writes to `20900103`.

### Open A Zone

| Byte | Bits/value | Operation |
|---:|---|---|
| 0 | `0x00` | Open-command discriminator. |
| 1 | `0x80 \| zone` | Set bit 7 and place zone number 1-4 in bits 0-2. |
| 2-3 | `0x00` | Reserved; write zero. |
| 4 | `minutes` | Automatic close duration in whole minutes, wire range 0-255. |
| 5-19 | `0x00` | Reserved; write zero. |

Formula:

```text
byte1 = 0x80 | zone
byte4 = duration_minutes & 0xff
```

| Zone | Byte 1 |
|---:|---:|
| 1 | `0x81` |
| 2 | `0x82` |
| 3 | `0x83` |
| 4 | `0x84` |

Example, open zone 2 for 7 minutes:

```text
00 82 00 00 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Operation order:

1. Complete the safe session sequence.
2. Write the 20-byte frame to `20900103`.
3. Wait approximately 1 second.
4. Poll status with `02 00` on `20900106`.
5. Read or await notification from `20900102` to confirm operation.

### Close A Zone

| Byte | Value | Operation |
|---:|---|---|
| 0 | Zone number `0x01`-`0x04` | Zone to close. |
| 1-19 | `0x00` | Write zero. |

Example, close zone 2:

```text
02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

The status frame may remain stale for about one second after the physical valve
closes. Poll every 500-1000 ms and do not treat the first active response as a
failed close.

## Runtime Status

Status is a 20-byte value from `20900102`. First write `02 00` to `20900106`.
Without that poll, the status characteristic commonly returns all zeros.
Notifications are more reliable than cached Windows GATT reads.

### Status Byte 0

| Pattern | Bit operation | Meaning | Confidence |
|---|---|---|---|
| `0xff` | Exact match | Idle | Confirmed |
| `0xf0`-`0xf3` | `(byte0 & 0x0f) + 1` | One active zone, zones 1-4 | Confirmed |
| `0x01`, `0x02`, `0x03`, etc. | High and low nibbles are zero-based zone indices | Two active zones | Confirmed |
| `0x0f`, `0x1f`, `0x2f`, `0x3f` | Lookup table | Transitional surviving zone 1-4 | Confirmed/Compatibility |
| Equal nibbles such as `0x22` | `(nibble + 1)` | One surviving active zone | Compatibility |
| Other | None | Unknown status; preserve/log raw frame | Unknown |

Two-zone decoding:

```text
first_zone  = (byte0 >> 4) + 1
second_zone = (byte0 & 0x0f) + 1
```

The nibble values must each be 0-3 and must differ.

### Remaining-Time Fields

| Active form | Minutes byte | Seconds byte | Formula |
|---|---:|---:|---|
| Single zone (`0xf0`-`0xf3`) | 2 | 3 | `byte2 * 60 + byte3` |
| Pair, first/high-nibble zone | 2 | 3 | `byte2 * 60 + byte3` |
| Pair, second/low-nibble zone | 5 | 6 | `byte5 * 60 + byte6` |
| Transitional (`0x0f`-`0x3f`) | 5 | 6 | `byte5 * 60 + byte6` |

Other status bytes are not decoded. In particular, byte 10 is not a confirmed
battery percentage.

### Status Poll Order

1. Write `02 00` to `20900106`.
2. Wait for a fresh `20900102` notification for up to about 1 second.
3. If no notification arrives, read `20900102`.
4. If all bytes are zero, wait about 400 ms and retry, up to three attempts.
5. Decode only a nonzero 20-byte frame.

## Date And Time

The official app writes an 8-byte local date/time frame to `20900104`:

| Byte | Value |
|---:|---|
| 0 | Century, plain binary (`20` decimal for years 2000-2099) |
| 1 | Year within century, plain binary |
| 2 | Month, 1-12 |
| 3 | Day, 1-31 |
| 4 | Hour, 0-23 |
| 5 | Minute, 0-59 |
| 6 | Second, 0-59 |
| 7 | Weekday; `6` was observed for Saturday |

Captured example for 2026-08-22 03:55:31:

```text
14 1a 08 16 03 37 1f 06
```

The field order is confirmed. The complete weekday numbering convention has
not been isolated, and the current applications do not send this frame.

## Program And Schedule Records

Each zone has one 20-byte schedule record. Read and write it through
`20900101`. Always use read-modify-write: unknown and mode-dependent bytes must
be preserved unless a transition rule below explicitly replaces them.

### Record Layout

| Byte | Bit/value encoding | Meaning | Confidence/write rule |
|---:|---|---|---|
| 0 | `1`-`4` | Zone number | Confirmed; must match selected zone. |
| 1 | Plain binary | Duration hours component | Confirmed. |
| 2 | Plain binary `0`-`59` | Duration minutes component | Confirmed. |
| 3 | `0x00` observed | Reserved | Preserve. |
| 4 | Bit field | Weekly days or cyclic mode | Confirmed; see bit table. |
| 5 | `0`-`23`, `0xff` unused | Start window 1 hour | Confirmed. |
| 6 | `0`-`59` | Start window 1 minute | Confirmed. |
| 7 | `0`-`23`, `0xff` unused | Start window 2 hour | Strongly supported. |
| 8 | `0`-`59` | Start window 2 minute | Strongly supported. |
| 9 | `0`-`23`, `0xff` unused | Start window 3 hour | Confirmed enable/disable; time encoding supported. |
| 10 | `0`-`59` | Start window 3 minute | Supported. |
| 11 | Mode-dependent; `0xff` in weekly baseline | Internal/fixed weekly field; once considered window 4 | Do not independently modify. |
| 12 | Mode-dependent; `0x00` in weekly baseline | Internal/fixed weekly field | Do not independently modify. |
| 13 | `0x80 + start_in_days` in cyclic; `0x80` weekly | Cyclic start offset | Confirmed. |
| 14 | `0xc0 + cadence_days` in cyclic; `0x00` weekly | Cyclic cadence | Confirmed. |
| 15-19 | `0x00` observed | Reserved | Preserve. |

The current GUI/MQTT compatibility model exposes four window pairs, including
bytes 11-12. Later official-app captures showed bytes 11-12 are fixed/internal
state during mode transitions and did not confirm a configurable fourth start
time. Protocol clients should treat only windows 1-3 as established and should
not write bytes 11-12 independently.

### Duration Encoding

```text
duration_hours   = total_minutes // 60
duration_minutes = total_minutes % 60
total_minutes    = byte1 * 60 + byte2
```

Example, 110 minutes (`1h50m`):

```text
byte1 = 01
byte2 = 32
```

One duration applies to all enabled start windows in the zone.

### Byte 4 Bit Operations

| Bit | Mask | Weekly mode (`bit7 = 0`) | Cyclic mode (`bit7 = 1`) |
|---:|---:|---|---|
| 0 | `0x01` | Sunday enabled | Clear/unused |
| 1 | `0x02` | Monday enabled | Clear/unused |
| 2 | `0x04` | Tuesday enabled | Clear/unused |
| 3 | `0x08` | Wednesday enabled | Clear/unused |
| 4 | `0x10` | Thursday enabled | Clear/unused |
| 5 | `0x20` | Friday enabled | Clear/unused |
| 6 | `0x40` | Saturday enabled | Clear/unused |
| 7 | `0x80` | Must be clear | Cyclic-mode selector |

Weekly mask construction:

```text
mask = 0
mask |= 1 << weekday_index       # enable a day
mask &= ~(1 << weekday_index)    # disable a day
```

Examples:

| Days/mode | Byte 4 |
|---|---:|
| Every day | `0x7f` |
| Weekdays, Monday-Friday | `0x3e` |
| Sunday and Saturday | `0x41` |
| Weekly with no active days | `0x00` |
| Cyclic mode | `0x80` |

### Window Encoding

For established window `n` from 1 through 3:

```text
hour_position = 5 + (n - 1) * 2
minute_position = hour_position + 1
```

| State | Hour byte | Minute byte |
|---|---:|---:|
| Enabled at `HH:MM` | `HH` (`0`-`23`) | `MM` (`0`-`59`) |
| Disabled | `0xff` | `0x00` |

Do not encode decimal text or BCD. For example, 15:30 is `0f 1e`.

### Read A Schedule

1. Complete the safe session sequence.
2. Write `01 <zone>` to `20900106`.
3. Wait about 300 ms.
4. Read the 20-byte value from `20900101`.
5. Accept it only if byte 0 equals the requested zone.
6. If byte 0 is stale or wrong, repeat steps 2-5 up to four times, waiting
   about 300 ms between attempts.

Zone 2 needs particular care because selector `01 02` resembles an old unsafe
command pattern. Validate byte 0 every time.

### Write A Schedule

1. Read the current zone record using the sequence above.
2. Copy all 20 bytes.
3. Modify only confirmed fields.
4. Apply the relevant weekly/cyclic transition rule.
5. Write the complete 20-byte record to `20900101`.
6. Wait approximately 500-600 ms.
7. Re-read the selected zone record.
8. Confirm bytes 0-14 match the requested values.
9. Retry confirmation because the controller can apply changes asynchronously.

Do not write a newly constructed partial record. The controller may silently
reject inconsistent mode fields or retain unknown bytes that have runtime
meaning.

### Weekly To Cyclic

1. Set byte 4 to `0x80`.
2. Set window 1 in bytes 5-6.
3. Set byte 13 to `0x80 + start_in_days`.
4. Set byte 14 to `0xc0 + cadence_days`.
5. Preserve other bytes from the read record.
6. Write and re-read to confirm.

No additional reset of bytes 7-10 was required in physical testing.

Example: 12-minute duration, start at 09:15, start in 2 days, every 3 days:

```text
02 00 0c 00 80 09 0f ff 00 ff 00 00 00 82 c3 00 00 00 00 00
```

### Cyclic To Weekly

This transition has a strict consistency requirement. In the same 20-byte
write that clears bit 7 of byte 4:

1. Set byte 4 to the seven-bit weekday mask.
2. Set bytes 11-14 exactly to `ff 00 80 00`.
3. Apply duration and window 1-3 changes.
4. Write the complete record.
5. Re-read and compare. The controller silently rejects an inconsistent frame.

Confirmed weekly all-days baseline fragment:

```text
... 7f ... ff 00 80 00 ...
```

A failed frame that retained cyclic values in bytes 13-14 was silently reverted
by the controller, even though the BLE write itself reported success.

## Device-Wide Settings

These are 20-byte writes to `20900103`. They appear to be write-only; no
confirmed read-back mechanism is known.

### Seasonal Adjustment

| Byte | Value |
|---:|---|
| 0-1 | `00 00` |
| 2 | `0x02` feature selector |
| 3-6 | `0x00` |
| 7 | Percentage as a raw byte |
| 8-19 | `0x00` |

Example, 110%:

```text
00 00 02 00 00 00 00 6e 00 00 00 00 00 00 00 00 00 00 00 00
```

The adjustment applies to all programmed durations at runtime; it does not
change each zone's schedule record. Valid device limits have not been fully
established.

### Rain Off

| Byte | Value |
|---:|---|
| 0-1 | `00 00` |
| 2 | `0x01` feature selector |
| 3-5 | `0x00` |
| 6 | Suspension duration in days; `0` clears it |
| 7-19 | `0x00` |

Example, suspend programs for 3 days:

```text
00 00 01 00 00 00 03 00 00 00 00 00 00 00 00 00 00 00 00 00
```

## Notifications, Timing, And Caching

| Situation | Required handling |
|---|---|
| Status read returns zeros | Poll `20900106` with `02 00`, then retry. |
| Windows returns old status | Prefer a fresh `20900102` notification over a cached read. |
| Schedule byte 0 is wrong | Re-select the zone, wait 300 ms, and re-read. |
| Status remains active after close | Wait about 1 second and poll again. |
| Schedule write reports success but values revert | Record failed device validation; re-check mode-dependent bytes. |
| Device is not found | Scan 45-60 seconds and close the official app, which can hold the connection. |
| Write with response fails | Retry the same complete payload without response. |

## Known Unknowns And Limitations

- Bytes 11-12 in schedule records are mode-dependent internal fields. A fourth
  configurable window has not been confirmed by an isolated capture.
- Schedule bytes 3 and 15-19 have no established meaning.
- The status frame's battery field, if one exists, is unknown.
- Seasonal adjustment and Rain Off have no confirmed read-back command.
- The complete weekday numbering convention for `20900104` is not confirmed.
- PIN enforcement may vary by pairing/session state.
- Handles can vary; use UUIDs unless decoding a capture from the tested unit.

## Captured Reference Frames

| Operation | Frame |
|---|---|
| Open zone 1, 4 min | `00 81 00 00 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| Close zone 1 | `01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| Open zone 2, 7 min | `00 82 00 00 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| Open zone 3, 2 min | `00 83 00 00 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| Rain Off, 3 days | `00 00 01 00 00 00 03 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| Seasonal adjustment, 110% | `00 00 02 00 00 00 00 6e 00 00 00 00 00 00 00 00 00 00 00 00` |
| Weekly zone 2, 1h, all days, 07:00 | `02 01 00 00 7f 07 00 ff 00 ff 00 ff 00 80 00 00 00 00 00 00` |
| Cyclic zone 2, 12 min, 09:15, +2d/3d | `02 00 0c 00 80 09 0f ff 00 ff 00 00 00 82 c3 00 00 00 00 00` |

## Implementation Mapping

| Protocol operation | Current implementation |
|---|---|
| PIN encoding | `pin_to_bytes()` |
| Open/close frames | `build_open_payload()`, `build_close_payload()` |
| Seasonal/Rain Off | `build_seasonal_payload()`, `build_rainoff_payload()` |
| Status decode | `decode_active_zones()` |
| Schedule read | `read_schedule()` |
| Schedule mutation | `modify_schedule()` |
| Schedule write | `write_schedule()` |
| Safe wake/status poll | `wake_controller()`, `read_status()` |

See `research/parse_snoop.py` for decoding ATT traffic from Android HCI snoop
captures.
