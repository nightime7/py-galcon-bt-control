"""Windows notification-area host for the Galcon MQTT bridge."""

import argparse
import asyncio
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image

from galcon_mqtt import (
    DEFAULT_BLE_CONNECT_TIMEOUT,
    DEFAULT_IDLE_GRACE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PREFIX,
    GalconMqttBridge,
    MQTT_CONFIG_PATH,
    load_mqtt_config,
)

ROOT = (Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).parent)
ICON_PATH = ROOT / "galcon.ico"


def build_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int)
    parser.add_argument("--mqtt-username")
    parser.add_argument("--mqtt-password")
    parser.add_argument("--mqtt-tls", action="store_true", default=None)
    parser.add_argument("--mqtt-client-id")
    parser.add_argument("--mqtt-timeout", type=float)
    parser.add_argument("--http-host")
    parser.add_argument("--http-port", type=int)
    parser.add_argument("--prefix")
    parser.add_argument("--mac")
    parser.add_argument("--scan-time", type=float)
    parser.add_argument("--poll-interval", type=float)
    parser.add_argument("--keep-connected", action="store_true", default=None)
    parser.add_argument("--idle-grace", type=float)
    parser.add_argument("--ble-connect-timeout", type=float)
    parser.add_argument("--no-initial-refresh", action="store_false",
                        dest="initial_refresh", default=None)
    args = parser.parse_args(argv)
    config = load_mqtt_config()
    for name in vars(args):
        if getattr(args, name) is None and name in config:
            setattr(args, name, config[name])
    args.mqtt_host = args.mqtt_host or ""
    args.mqtt_port = args.mqtt_port if args.mqtt_port is not None else 1883
    args.mqtt_username = args.mqtt_username or None
    args.mqtt_password = args.mqtt_password or None
    args.mqtt_tls = bool(args.mqtt_tls)
    args.mqtt_client_id = args.mqtt_client_id or "galcon-gl6100-bridge"
    args.mqtt_timeout = args.mqtt_timeout if args.mqtt_timeout is not None else 30.0
    args.http_host = args.http_host or "127.0.0.1"
    args.http_port = args.http_port if args.http_port is not None else 0
    args.prefix = args.prefix or DEFAULT_PREFIX
    args.scan_time = args.scan_time if args.scan_time is not None else 60.0
    args.poll_interval = (args.poll_interval
                          if args.poll_interval is not None
                          else DEFAULT_POLL_INTERVAL)
    args.keep_connected = bool(args.keep_connected)
    args.idle_grace = (args.idle_grace
                       if args.idle_grace is not None else DEFAULT_IDLE_GRACE)
    args.ble_connect_timeout = (args.ble_connect_timeout
                                if args.ble_connect_timeout is not None
                                else DEFAULT_BLE_CONNECT_TIMEOUT)
    args.initial_refresh = (args.initial_refresh
                            if args.initial_refresh is not None else True)
    args.reconnect_delay = 10.0
    if not args.mqtt_host:
        parser.error("--mqtt-host is required or must be set in "
                     f"{MQTT_CONFIG_PATH.name}")
    return args


class TrayApp:
    def __init__(self, args):
        self.args = args
        self.bridge = None
        self.loop = None
        self.bridge_error = None
        self.icon = pystray.Icon(
            "galcon-gl6100",
            Image.open(ICON_PATH),
            "Galcon GL6100",
            self._menu(),
        )

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(self._mqtt_label, None, enabled=False),
            pystray.MenuItem(self._ble_label, None, enabled=False),
            pystray.MenuItem(self._zones_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )

    def _mqtt_label(self, _item):
        return "MQTT: connected" if self.bridge and self.bridge.mqtt_connected.is_set() else "MQTT: disconnected"

    def _ble_label(self, _item):
        connected = self.bridge and self.bridge.client and self.bridge.client.is_connected
        return "Controller: connected" if connected else "Controller: disconnected"

    def _zones_label(self, _item):
        if not self.bridge or not self.bridge.active_zones:
            return "Zones: idle"
        zones = ", ".join(str(zone) for zone in self.bridge.active_zones)
        return f"Zones: {zones} running"

    async def _run_bridge(self):
        self.loop = asyncio.get_running_loop()
        self.bridge = GalconMqttBridge(self.args)
        try:
            await self.bridge.start()
        except Exception as exc:  # noqa: BLE001
            self.bridge_error = exc
        finally:
            await self.bridge.stop()

    def _bridge_thread(self):
        asyncio.run(self._run_bridge())

    def _refresh_menu(self):
        self.icon.menu = self._menu()
        self.icon.update_menu()
        if self.bridge_error:
            self.icon.title = f"Galcon GL6100 - {self.bridge_error}"
        else:
            self.icon.title = "Galcon GL6100"
        if not self._stopping:
            self._menu_refresh_timer = threading.Timer(2.0, self._refresh_menu)
            self._menu_refresh_timer.daemon = True
            self._menu_refresh_timer.start()

    def quit(self, _icon=None, _item=None):
        self._stopping = True
        if self._menu_refresh_timer:
            self._menu_refresh_timer.cancel()
        if self.bridge and self.loop:
            future = asyncio.run_coroutine_threadsafe(self.bridge.stop(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.icon.stop()

    def run(self):
        self._stopping = False
        self._menu_refresh_timer = None
        self._bridge_thread_obj = threading.Thread(
            target=self._bridge_thread, name="galcon-mqtt", daemon=True)
        self._bridge_thread_obj.start()
        self._refresh_menu()
        self.icon.run()


def main():
    TrayApp(build_args()).run()


if __name__ == "__main__":
    main()
