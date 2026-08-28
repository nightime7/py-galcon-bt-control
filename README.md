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
- `galcon_mqtt.py` - a headless MQTT bridge with Home Assistant MQTT
    Discovery, persistent BLE connection/reconnect, status polling, zone
    controls, and program/device setting topics.

## Install

### Option 1: MSI installer (recommended)

Download the MSI for your architecture from the
[Releases](https://github.com/nightime7/py-galcon-bt-control/releases) page
and run it. Python does **not** need to be installed - the MSI bundles its
own interpreter and all dependencies.

The installer adds Start Menu and Desktop shortcuts for the GUI, and also
installs `galcon-cli.exe` and `galcon-mqtt.exe` alongside it for command-line
and Home Assistant use.

The installer uses the standard Windows MSI flow, including a GPL-3.0 license
agreement page, an install-folder chooser, an all-users installation option,
and a Launch on Finish checkbox.

Pick `x64` for a normal Intel/AMD PC, or `arm64` for an ARM-based Windows
device (e.g. Snapdragon-powered laptops).

#### "Unknown publisher" warning

The MSI is **not code-signed**, so Windows will show an *Unknown publisher*
prompt (and possibly a SmartScreen "Windows protected your PC" screen). This
is expected. Code-signing certificates are issued by commercial CAs and are
not free, and a self-signed certificate would not help - it only suppresses
the warning on machines that have explicitly been told to trust it.

To proceed: click **More info** then **Run anyway**.

If you would rather not trust an unsigned installer, use
[Option 2](#option-2-run-from-source) and run from source instead - the code
is all here and readable.

To verify a download actually came from this repository, compare its hash
against the checksums published in the release notes:

```powershell
Get-FileHash .\GalconGL6100Control-1.1.0-x64.msi -Algorithm SHA256
```

### Option 2: Run from source

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

If you installed via the MSI, use the bundled CLI instead (run it from the
install folder, e.g. `C:\Program Files\Galcon GL6100 Control`):

```powershell
.\galcon-cli.exe --set-pin 1234
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

## Home Assistant / MQTT

Run the bridge on a Windows PC that has Bluetooth access to the controller and
network access to your MQTT broker:

```powershell
python galcon_mqtt.py --mqtt-host homeassistant.local
```

An optional local HTTP API can be enabled for REST-based Home Assistant
entities or diagnostics:

```powershell
python galcon_mqtt.py --mqtt-host homeassistant.local --http-host 0.0.0.0 --http-port 8765
```

It provides `GET /api/status`, `GET /api/programs`, `POST /api/refresh`,
`POST /api/zone/<zone>/set` with `{"command":"OPEN:5"}`, and device setting
POST endpoints. Keep the HTTP API on a trusted network; it has no built-in
authentication.

With the MSI, run `galcon-mqtt.exe` from the installation folder. Add
`--mqtt-username`, `--mqtt-password`, and `--mqtt-tls` when your broker
requires authentication or TLS. The default MQTT prefix is
`galcon_gl6100`; change it with `--prefix` if more than one bridge is used.

The bridge publishes Home Assistant MQTT Discovery entities automatically.
After it connects to the broker, Home Assistant will discover:

- Controller connection, status, active zone, and last-update sensors
- Four zone switches and remaining-time sensors
- Four zone program sensors containing JSON schedule data
- Seasonal-adjustment and rain-off number controls

The bridge also publishes retained `galcon_gl6100/availability` (`online` or
`offline`) separately from `galcon_gl6100/status` (`idle` or `running`), so
Home Assistant can distinguish a connected idle controller from a disconnected
one.

Useful MQTT commands:

```text
galcon_gl6100/zone/1/set              ON, OFF, OPEN:5, or CLOSE
galcon_gl6100/refresh                 any payload; repolls status/programs
galcon_gl6100/device/seasonal/set     100
galcon_gl6100/device/rainoff/set      0
galcon_gl6100/zone/1/program/set      {"duration": 15, "days": 127}
```

Program commands use JSON keys `duration`, `hour`, `minute`, `days`,
`cadence`, and `start_in`. The bridge keeps the BLE connection open, polls
status every 10 seconds, publishes retained state, and reconnects after a
link loss. MQTT messages are retained so Home Assistant can restore the last
known state while the bridge reconnects.

For unattended operation, create a Windows Task Scheduler task that runs
`galcon-mqtt.exe --mqtt-host <broker>` at user logon or system startup. Set
the task to run whether or not a user is logged on. The MQTT bridge is
headless and does not require the GUI to be open.

## Building the installer

```powershell
pip install cx_Freeze
python setup_msi.py bdist_msi
```

The MSI lands in `dist/`. cx_Freeze bundles the interpreter and the
architecture-specific native modules that `bleak` depends on, so it cannot
cross-compile: an x64 Python produces an x64 MSI, an ARM64 Python produces
an ARM64 MSI. The GitHub Actions workflow in `.github/workflows/build-msi.yml`
builds both on their respective runners when a `v*` tag is pushed.

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
