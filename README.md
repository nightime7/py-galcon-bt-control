# Galcon GL6100 BLE Control

Unofficial, reverse-engineered Python tools to control a Galcon GL6100 /
6100BT DC4 Bluetooth LE irrigation valve controller from Windows, without
needing the official mobile app for day-to-day operation.

This project is not affiliated with or endorsed by Galcon. The BLE protocol
was reverse-engineered from HCI captures of the official Android app. See
the module docstring in `control_galcon.py` for full protocol notes.

## Features

- `control_galcon.py` - command-line control: open/close zones, read status,
  read/write weekly or cyclic irrigation programs, seasonal adjustment,
  rain-off, and a persistent `--interactive` session mode.
- `galcon_gui.py` - a desktop GUI (Tkinter) with live zone status,
  countdown timers, and a program editor (weekly/cyclic, per-window
  enable/disable, hour/minute dropdowns).

## Requirements

- Windows with Bluetooth LE support
- Python 3.10+
- `pip install -r requirements.txt`

## First-time setup

The controller's PIN (and, optionally, its MAC address) are device-specific
and are **not** committed to this repository. They are stored locally in
`galcon_device.json`, which is gitignored.

Create it by running:

```powershell
python control_galcon.py --set-pin 1234
```

This is the PIN shown on the controller's display the first time it was
paired with the official app. `--set-pin` only needs to be run once.

A MAC address is optional - scanning works by the controller's advertised
name (`GL6100...`) alone. If you want to pin/disambiguate a specific unit:

```powershell
python control_galcon.py --set-mac AA:BB:CC:DD:EE:FF
```

Alternatively, copy `galcon_device.example.json` to `galcon_device.json` and
fill in your own values by hand.

**Important:** this project does not perform BLE pairing/enrollment
automation. Pair the controller with the official Galcon app first (enter
the PIN it displays), then use these tools for day-to-day control.

## Usage

```powershell
python control_galcon.py --status
python control_galcon.py --programs
python control_galcon.py --open 1 --minutes 5
python control_galcon.py --close 1
python control_galcon.py --interactive
```

Run `python control_galcon.py --help` for the full command list.

For the GUI:

```powershell
python galcon_gui.py
```

## research/

`research/` contains one-off scripts used while reverse-engineering the BLE
protocol (opcode sweeps, raw GATT dumps, HCI snoop log parsing, an early
enrollment probe, etc.). They are not required to operate the controller and
are kept only as reference material. Several of them intentionally send
unusual/undocumented frames to the device - read each script's docstring
before running it.

## Safety note

`20900101` is used both as a wake/keepalive characteristic and as the
schedule-save pipe. Writing arbitrary short frames to it can unintentionally
overwrite a zone's saved program (this has been observed and is documented
in `control_galcon.py`). The shipped tools avoid this; be cautious if you
modify them.

## Disclaimer

This project is an independent, unofficial implementation and is NOT
affiliated with, authorized, maintained, sponsored, or endorsed by Galcon or
any of its affiliates or subsidiaries. Use this script at your own risk. The
author is not responsible for any damage to property, over-watering, or
hardware failure resulting from the use of this software.

## License

GPL-3.0. See `LICENSE`.
