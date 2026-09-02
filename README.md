# Galcon GL6100 BLE Control

Unofficial Windows tools for controlling a Galcon GL6100 / 6100BT DC4
Bluetooth LE irrigation controller without using the official app for
day-to-day operation.

The BLE protocol was reverse-engineered from Android HCI captures. This
project is not affiliated with or endorsed by Galcon.

## What Is Included

- `GalconControlGUI.exe` / `galcon_gui.py`: desktop control, live status,
  countdowns, and weekly or cyclic program editing.
- `galcon-cli.exe` / `control_galcon.py`: command-line control and diagnostics.
- `GalconMqttTray.exe` / `galcon_mqtt_tray.py`: notification-area MQTT bridge
  with configuration, logs, and automatic startup controls.
- `galcon-mqtt.exe` / `galcon_mqtt.py`: headless MQTT bridge with Home
  Assistant MQTT Discovery.

## Install

### Windows Installer

Download the MSI for your architecture from
[Releases](https://github.com/nightime7/py-galcon-bt-control/releases):

- `x64`: most Intel and AMD Windows PCs
- `arm64`: ARM-based Windows devices such as Snapdragon laptops

The MSI includes Python and all dependencies. It installs the GUI, CLI, MQTT
bridge, tray application, and Start Menu shortcuts. Installing a newer version
upgrades the existing installation and preserves configuration in AppData.

The installer is not code-signed, so Windows may display an **Unknown
publisher** or SmartScreen warning. Verify the SHA-256 checksum against the
release notes before continuing:

```powershell
Get-FileHash .\GalconGL6100Control-<version>-x64.msi -Algorithm SHA256
```

### Run From Source

Requirements:

- Windows with Bluetooth LE support
- Python 3.10 or newer

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Controller Setup

First pair and configure the controller with the official Galcon app. This
project does not automate initial BLE enrollment.

Save the controller's four-digit PIN locally:

```powershell
python control_galcon.py --set-pin 1234
```

For an installed copy, run this from the installation directory, normally
`C:\Program Files\Galcon GL6100 Control`:

```powershell
.\galcon-cli.exe --set-pin 1234
```

The PIN and optional MAC address are stored in the gitignored
`galcon_device.json`. Scanning normally identifies the controller by its
advertised `GL6100...` name. To select a specific controller, save its MAC:

```powershell
python control_galcon.py --set-mac AA:BB:CC:DD:EE:FF
```

## Use The Controller

Start the installed **Galcon GL6100 Control** shortcut, or run from source:

```powershell
python galcon_gui.py
```

Common CLI commands:

```powershell
python control_galcon.py --status
python control_galcon.py --programs
python control_galcon.py --open 1 --minutes 5
python control_galcon.py --close 1
python control_galcon.py --interactive
```

Run `python control_galcon.py --help` for all commands.

## Home Assistant And MQTT

The bridge must run on a Windows PC with Bluetooth access to the controller
and network access to the MQTT broker.

### Installed Bridge

Start **Galcon MQTT Bridge** from the Start Menu. Right-click its tray icon to:

- Configure MQTT and controller connection settings
- View bridge activity and connection status
- Enable **Start with Windows**
- Quit the bridge

Restart the tray application after changing connection settings.

### Bridge From Source

Copy the example configuration and edit the broker settings:

```powershell
Copy-Item .\galcon_mqtt.example.json .\galcon_mqtt.json
python galcon_mqtt.py
```

You can also provide settings on the command line:

```powershell
python galcon_mqtt.py --mqtt-host homeassistant.local
```

Configuration is stored in `%APPDATA%\Galcon GL6100 Control` for installed
applications. Source runs also accept `galcon_mqtt.json` beside the scripts.
Command-line options override file settings. Do not commit configuration files
containing broker credentials.

### Home Assistant Entities

MQTT Discovery creates:

- Controller connection, status, active-zone, and last-update sensors
- A manual on/off switch and remaining-time sensor for each zone
- Program mode, next-run, and editable duration for each zone
- Enable, hour, and minute controls for four program windows per zone
- Seasonal-adjustment and rain-off controls
- A status refresh button

The default topic prefix is `galcon_gl6100`. Change it with `--prefix` when
running more than one bridge.

### MQTT Commands

```text
galcon_gl6100/zone/1/set              ON, OFF, OPEN:5, or CLOSE
galcon_gl6100/refresh                 any payload
galcon_gl6100/device/seasonal/set     100
galcon_gl6100/device/rainoff/set      0
galcon_gl6100/zone/1/program/set      {"duration": 15, "days": 127}
galcon_gl6100/zone/1/program/set      {"window": 2, "enabled": true, "hour": 14, "minute": 30}
```

Program JSON supports `duration`, `days`, `cadence`, and `start_in`. Use
`window` from 1 through 4 with `enabled`, `hour`, or `minute` to edit a
specific start window. Without `window`, `hour` and `minute` target window 1.

### Connection Behavior

By default, the bridge connects over BLE when a command or refresh is needed.
After a successful setting change it keeps the connection open for 120 seconds
to make follow-up commands faster, then disconnects to conserve controller
battery. Relevant settings are:

- `idle_grace`: seconds to retain the connection after a command; use `0` to
  disconnect immediately.
- `poll_interval`: background refresh interval in seconds; `0` disables it.
- `keep_connected`: retain the BLE connection between operations.
- `initial_refresh`: load status and programs when the bridge starts.
- `ble_connect_timeout`: connection timeout for weak BLE signals.

MQTT state messages are retained. Retained command messages are ignored so an
old command cannot run after the bridge reconnects.

## Optional HTTP API

The MQTT bridge can expose a small local API:

```powershell
python galcon_mqtt.py --mqtt-host homeassistant.local `
  --http-host 127.0.0.1 --http-port 8765
```

Endpoints include `GET /api/status`, `GET /api/programs`,
`POST /api/refresh`, `POST /api/zone/<zone>/set`, and device setting POST
endpoints. The API has no authentication; do not expose it outside a trusted
network or bind it publicly without a secured reverse proxy. Set `http_port`
to `0` to disable it.

## Build The Installer

```powershell
pip install cx_Freeze
python setup_msi.py bdist_msi
```

The MSI is written to `dist/`. cx_Freeze builds for the current Python
interpreter's architecture and cannot cross-compile between x64 and ARM64.
The release workflow builds both architectures when a `v*` tag is pushed.

## Development Notes

See [BLUETOOTH_PROTOCOL.md](BLUETOOTH_PROTOCOL.md) for GATT characteristics,
byte and bit layouts, operation sequences, captured frames, and known unknowns.
Scripts under `research/` were used for protocol investigation and are not
required for normal operation. Some send undocumented frames; read their
docstrings before running them.

The `20900101` characteristic is also the schedule-save pipe. Arbitrary short
writes can overwrite a saved program. The shipped applications avoid unsafe
writes.

## Disclaimer

Use this software at your own risk. The author is not responsible for property
damage, over-watering, or hardware failure resulting from its use.

## License

GPL-3.0. See `LICENSE`.
