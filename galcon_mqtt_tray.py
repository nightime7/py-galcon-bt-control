"""Windows notification-area host for the Galcon MQTT bridge."""

import argparse
import asyncio
import json
import os
import sys
import threading
import tkinter as tk
import winreg
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageTk

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
STARTUP_VALUE = "GalconGL6100MqttTray"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_SHORTCUT_NAME = "Galcon MQTT Bridge.lnk"


def legacy_startup_shortcuts():
    paths = []
    for env_name in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env_name)
        if base:
            paths.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" /
                         "Programs" / "Startup" / STARTUP_SHORTCUT_NAME)
    return paths


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def startup_enabled():
    if any(path.exists() for path in legacy_startup_shortcuts()):
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, STARTUP_VALUE)
            return value == startup_command()
    except (FileNotFoundError, OSError):
        return False


def set_startup_enabled(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ,
                              startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE)
            except FileNotFoundError:
                pass
    if not enabled:
        failures = []
        for path in legacy_startup_shortcuts():
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        if failures:
            raise OSError("Could not remove the legacy Startup shortcut. "
                          "Install the latest MSI or remove it as an "
                          "administrator:\n" + "\n".join(failures))


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
    parser.add_argument("--reconnect-delay", type=float)
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
    args.reconnect_delay = (args.reconnect_delay
                            if args.reconnect_delay is not None else 10.0)
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
        self._log_entries = deque(maxlen=2000)
        self._log_sequence = 0
        self._log_lock = threading.Lock()
        self._log_window_open = False
        self._log_focus_requested = threading.Event()
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
            pystray.MenuItem(self._next_run_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show Log...", self.show_log, default=True),
            pystray.MenuItem("Configure MQTT...", self.configure_mqtt),
            pystray.MenuItem("Start with Windows", self.toggle_startup,
                             checked=lambda _item: startup_enabled()),
            pystray.MenuItem("Quit", self.quit),
        )

    def _mqtt_label(self, _item):
        return "MQTT: connected" if self.bridge and self.bridge.mqtt_connected.is_set() else "MQTT: disconnected"

    def _ble_label(self, _item):
        state = self.bridge.ble_state if self.bridge else "disconnected"
        return f"Controller: {state}"

    def _zones_label(self, _item):
        if not self.bridge or not self.bridge.active_zones:
            return "Zones: idle"
        zones = ", ".join(str(zone) for zone in self.bridge.active_zones)
        return f"Zones: {zones} running"

    def _next_run_label(self, _item):
        if not self.bridge:
            return "Next: unavailable"
        upcoming = []
        for zone, record in self.bridge.programs.items():
            next_run = self.bridge._next_program_run(record)
            if next_run is not None:
                duration = record[1] * 60 + record[2]
                upcoming.append((next_run, zone, duration))
        if not upcoming:
            return "Next: none scheduled"
        next_run, zone, duration = min(upcoming)
        return (f"Next: Zone {zone} - {next_run:%Y-%m-%d %H:%M} - "
                f"{duration} min")

    async def _run_bridge(self):
        self.loop = asyncio.get_running_loop()
        self.bridge = GalconMqttBridge(self.args)
        self.bridge.state_change_callback = self._refresh_menu
        self.bridge.log_callback = self._capture_log
        try:
            await self.bridge.start()
        except Exception as exc:  # noqa: BLE001
            self.bridge_error = exc
            self._capture_log(f"MQTT bridge stopped: {type(exc).__name__}: {exc}")
        finally:
            await self.bridge.stop()

    def _bridge_thread(self):
        asyncio.run(self._run_bridge())

    def _capture_log(self, line):
        with self._log_lock:
            self._log_sequence += 1
            self._log_entries.append((self._log_sequence, line))

    def show_log(self, _icon=None, _item=None):
        with self._log_lock:
            if self._log_window_open:
                self._log_focus_requested.set()
                return
            self._log_window_open = True
        threading.Thread(target=self._log_window_thread,
                         name="galcon-log", daemon=True).start()

    def _log_window_thread(self):
        try:
            root = tk.Tk()
            root.title("Galcon MQTT Bridge Log")
            root.geometry("900x520")
            root.minsize(620, 320)
            root.update_idletasks()
            root.iconbitmap(default=str(ICON_PATH))
            icon_image = Image.open(ICON_PATH).convert("RGBA")
            icon_photo = ImageTk.PhotoImage(icon_image, master=root)
            root.iconphoto(True, icon_photo)
            root.protocol("WM_DELETE_WINDOW", root.withdraw)

            frame = ttk.Frame(root, padding=8)
            frame.pack(fill="both", expand=True)
            scrollbar = ttk.Scrollbar(frame, orient="vertical")
            scrollbar.pack(side="right", fill="y")
            output = tk.Text(
                frame,
                wrap="none",
                state="disabled",
                font=("Consolas", 10),
                background="#101214",
                foreground="#e8e8e8",
                insertbackground="#e8e8e8",
                yscrollcommand=scrollbar.set,
            )
            output.pack(fill="both", expand=True)
            scrollbar.configure(command=output.yview)
            last_sequence = 0

            def refresh():
                nonlocal last_sequence
                with self._log_lock:
                    entries = [entry for entry in self._log_entries
                               if entry[0] > last_sequence]
                if entries:
                    output.configure(state="normal")
                    output.insert("end", "".join(f"{line}\n"
                                                  for _seq, line in entries))
                    output.configure(state="disabled")
                    output.see("end")
                    last_sequence = entries[-1][0]
                if self._log_focus_requested.is_set():
                    self._log_focus_requested.clear()
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                root.after(200, refresh)

            refresh()
            root.mainloop()
        finally:
            with self._log_lock:
                self._log_window_open = False

    def _refresh_menu(self):
        with self._menu_refresh_lock:
            if self._menu_refresh_timer:
                self._menu_refresh_timer.cancel()
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
        self.icon.stop()
        if self.bridge and self.loop:
            threading.Thread(target=self._stop_bridge,
                             name="galcon-mqtt-stop", daemon=True).start()

    def _stop_bridge(self):
        try:
            future = asyncio.run_coroutine_threadsafe(self.bridge.stop(), self.loop)
            future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001
            self.bridge_error = exc

    def toggle_startup(self, _icon=None, _item=None):
        enabled = not startup_enabled()
        try:
            set_startup_enabled(enabled)
        except OSError as exc:
            threading.Thread(target=self._show_startup_error, args=(str(exc),),
                             name="galcon-startup-error", daemon=True).start()
        self._refresh_menu()

    @staticmethod
    def _show_startup_error(message):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Startup setting", message, parent=root)
        root.destroy()

    def configure_mqtt(self, _icon=None, _item=None):
        threading.Thread(target=self._configuration_thread,
                         name="galcon-config", daemon=True).start()

    def _configuration_thread(self):
        root = tk.Tk()
        root.title("Galcon MQTT Configuration")
        root.resizable(False, False)
        values = load_mqtt_config()
        fields = (
            ("MQTT host", "mqtt_host", False),
            ("MQTT port", "mqtt_port", False),
            ("MQTT username", "mqtt_username", False),
            ("MQTT password", "mqtt_password", True),
            ("Client ID", "mqtt_client_id", False),
            ("HTTP host", "http_host", False),
            ("HTTP port (0=off)", "http_port", False),
            ("MQTT prefix", "prefix", False),
            ("Controller MAC", "mac", False),
            ("Scan seconds", "scan_time", False),
            ("Poll interval seconds (0=on demand)", "poll_interval", False),
            ("Idle grace seconds", "idle_grace", False),
            ("BLE connect timeout seconds", "ble_connect_timeout", False),
            ("MQTT timeout seconds", "mqtt_timeout", False),
            ("Reconnect delay seconds", "reconnect_delay", False),
        )
        frame = ttk.Frame(root, padding=14)
        frame.grid(sticky="nsew")
        variables = {}
        for row, (label, key, secret) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0,
                                              sticky="w", padx=(0, 12), pady=3)
            variable = tk.StringVar(value=str(values.get(key, "")))
            variables[key] = variable
            ttk.Entry(frame, textvariable=variable, width=34,
                      show="*" if secret else "").grid(row=row, column=1,
                                                        sticky="ew", pady=3)
        tls_var = tk.BooleanVar(value=bool(values.get("mqtt_tls", False)))
        initial_var = tk.BooleanVar(value=values.get("initial_refresh", True))
        keep_var = tk.BooleanVar(value=bool(values.get("keep_connected", False)))
        row = len(fields)
        ttk.Checkbutton(frame, text="Use MQTT TLS", variable=tls_var).grid(
            row=row, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="Initial status/program refresh",
                        variable=initial_var).grid(row=row + 1, column=1,
                                                   sticky="w", pady=3)
        ttk.Checkbutton(frame, text="Keep BLE connected between polls",
                        variable=keep_var).grid(row=row + 2, column=1,
                                                sticky="w", pady=3)

        def save():
            config = {key: variable.get().strip()
                      for key, variable in variables.items()}
            try:
                for key in ("mqtt_port", "http_port", "poll_interval", "idle_grace",
                            "ble_connect_timeout"):
                    config[key] = int(config[key])
                for key in ("scan_time", "mqtt_timeout", "reconnect_delay"):
                    config[key] = float(config[key])
                if not config["mqtt_host"]:
                    raise ValueError("MQTT host is required")
                if not 1 <= config["mqtt_port"] <= 65535:
                    raise ValueError("MQTT port must be 1-65535")
                if not 0 <= config["http_port"] <= 65535:
                    raise ValueError("HTTP port must be 0-65535")
                if config["poll_interval"] < 0 or config["idle_grace"] < 0:
                    raise ValueError("Poll interval and idle grace cannot be negative")
            except ValueError as exc:
                messagebox.showerror("Invalid configuration", str(exc), parent=root)
                return
            config["mqtt_tls"] = tls_var.get()
            config["initial_refresh"] = initial_var.get()
            config["keep_connected"] = keep_var.get()
            try:
                MQTT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                MQTT_CONFIG_PATH.write_text(json.dumps(config, indent=2),
                                            encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc), parent=root)
                return
            messagebox.showinfo("Saved", "Configuration saved. Restart the tray "
                                "app to apply the changes.", parent=root)
            root.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="left")
        root.mainloop()

    def run(self):
        self._stopping = False
        self._menu_refresh_timer = None
        self._menu_refresh_lock = threading.Lock()
        self._bridge_thread_obj = threading.Thread(
            target=self._bridge_thread, name="galcon-mqtt", daemon=True)
        self._bridge_thread_obj.start()
        self._refresh_menu()
        self.icon.run()


def main():
    TrayApp(build_args()).run()


if __name__ == "__main__":
    main()
