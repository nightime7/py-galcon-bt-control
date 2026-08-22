import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from bleak import BleakClient, BleakScanner

from control_galcon import (
    CHAR_COMMAND,
    CHAR_PIN,
    CHAR_POLL,
    CHAR_STATUS,
    CHAR_VALVE,
    NAME_HINT,
    REPAIR_HINT,
    SERVICE_UUID,
    STATUS_POLL,
    build_close_payload,
    build_open_payload,
    build_rainoff_payload,
    build_seasonal_payload,
    hexdump,
    load_saved_mac,
    load_saved_pin,
    pin_to_bytes,
    read_schedule,
    save_mac,
    save_pin,
    ts,
    wake_controller,
    write_char,
    write_schedule,
)

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
WINDOW_POSITIONS = (5, 7, 9, 11)


async def find_device_for_gui(scan_time, log, mac=None):
    log(f"[{ts()}] Scanning up to {scan_time:.0f}s for the valve...")
    found = asyncio.Event()
    holder = {}

    def cb(device, adv):
        name = device.name or adv.local_name or ""
        if (mac and device.address.upper() == mac.upper()) or NAME_HINT in name.lower():
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

    log(f"[{ts()}] Found {holder['dev'].name} at {holder['rssi']} dBm")
    if holder["rssi"] < -80:
        log("Weak signal; the link may drop mid-session.")
    return holder["dev"]


class FreshStatusCache:
    def __init__(self):
        self.value = None
        self._event = asyncio.Event()

    def update(self, value):
        self.value = bytes(value)
        self._event.set()

    async def wait_fresh(self, timeout=1.0):
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.value


class GalconSession:
    def __init__(self, ui_queue):
        self.ui_queue = ui_queue
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.client = None
        self.status_cache = None
        self.connected = False
        self.debug = False
        # Cached from a prior successful scan in this process. Reconnecting
        # to the same device object is near-instant (measured ~50ms) versus
        # a fresh scan, so this is retried first on the next Connect click.
        self.last_device = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(self._done)
        return future

    def _done(self, future):
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("log", f"{type(exc).__name__}: {exc}"))
            self.ui_queue.put(("busy", False))

    def log(self, text):
        self.ui_queue.put(("log", text))

    async def connect(self, scan_time=60.0, send_pin=False, pin=None, debug=False,
                      mac=None):
        self.debug = debug
        self.ui_queue.put(("busy", True))

        def on_disconnect(_client):
            self.connected = False
            self.ui_queue.put(("connected", False))
            self.log(f"[{ts()}] Disconnected")

        device = self.last_device
        client = None
        if device is not None:
            self.log(f"[{ts()}] Reconnecting to previously found device...")
            try:
                client = BleakClient(device, timeout=15.0,
                                     disconnected_callback=on_disconnect,
                                     services=[SERVICE_UUID],
                                     winrt=dict(use_cached_services=True))
                await client.connect()
            except Exception as exc:  # noqa: BLE001
                self.log(f"[{ts()}] Reconnect failed ({exc}); scanning again.")
                client = None
                self.last_device = None

        if client is None:
            device = await find_device_for_gui(scan_time, self.log, mac=mac)
            if device is None:
                self.log("Valve not found. Press a button on the unit to wake it and try again.")
                self.ui_queue.put(("busy", False))
                return
            client = BleakClient(device, timeout=30.0,
                                 disconnected_callback=on_disconnect,
                                 services=[SERVICE_UUID],
                                 winrt=dict(use_cached_services=True))
            await client.connect()
            self.last_device = device

        self.client = client
        self.status_cache = FreshStatusCache()
        await self.client.start_notify(CHAR_STATUS, self._on_status_notify)
        try:
            await self.client.start_notify(CHAR_COMMAND, self._on_command_notify)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[{ts()}] Command notify subscribe failed: {exc}")

        self.connected = True
        self.ui_queue.put(("connected", True))
        self.log(f"[{ts()}] Connected")
        await wake_controller(self.client, debug=debug)
        await asyncio.sleep(0.5)
        if send_pin and pin:
            await write_char(self.client, CHAR_PIN, pin_to_bytes(pin), "pin",
                             debug=debug)
            await asyncio.sleep(0.5)
        await self.refresh_all()
        self.ui_queue.put(("busy", False))

    async def disconnect(self):
        self.ui_queue.put(("busy", True))
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.connected = False
        self.client = None
        self.status_cache = None
        self.ui_queue.put(("connected", False))
        self.ui_queue.put(("busy", False))

    def _on_status_notify(self, sender, data):
        value = bytes(data)
        if self.debug:
            self.log(f"[{ts()}] NOTIFY {sender}: {hexdump(value)}")
        if self.status_cache is not None:
            self.status_cache.update(value)
        self.ui_queue.put(("status", value))

    def _on_command_notify(self, sender, data):
        if self.debug:
            self.log(f"[{ts()}] NOTIFY {sender}: {hexdump(bytes(data))}")

    def _require_client(self):
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Not connected")
        return self.client

    async def refresh_all(self):
        await self.read_status()
        await self.read_programs()

    async def read_status(self):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
            value = None
            if self.status_cache is not None:
                value = await self.status_cache.wait_fresh(timeout=1.0)
            if value is None:
                value = bytes(await client.read_gatt_char(CHAR_STATUS))
            self.ui_queue.put(("status", value))
        finally:
            self.ui_queue.put(("busy", False))

    async def open_zone(self, zone, minutes):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            payload = build_open_payload(zone, minutes)
            ok = await write_char(client, CHAR_VALVE, payload,
                                  f"OPEN zone {zone} for {minutes} min",
                                  debug=self.debug)
            if not ok:
                self.log(REPAIR_HINT)
                return
            self.log(f"[{ts()}] Opened zone {zone} for {minutes} min")
            await asyncio.sleep(1.0)
            await self.read_status()
        finally:
            self.ui_queue.put(("busy", False))

    async def close_zone(self, zone):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            ok = await write_char(client, CHAR_VALVE, build_close_payload(zone),
                                  f"CLOSE zone {zone}", debug=self.debug)
            if not ok:
                self.log(REPAIR_HINT)
                return
            value = None
            for attempt in range(10):
                await asyncio.sleep(0.5 if attempt else 1.0)
                await client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
                if self.status_cache is not None:
                    value = await self.status_cache.wait_fresh(timeout=1.0)
                    if value is None:
                        continue
                else:
                    value = bytes(await client.read_gatt_char(CHAR_STATUS))
                if not value or value[0] == 0xff or (value[0] & 0x0f) + 1 != zone:
                    break
            if value is not None:
                self.ui_queue.put(("status", value))
            self.log(f"[{ts()}] Closed zone {zone}")
        finally:
            self.ui_queue.put(("busy", False))

    async def read_programs(self):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            records = {}
            for zone in range(1, 5):
                records[zone] = await read_schedule(client, zone, debug=self.debug,
                                                    display=False)
            self.ui_queue.put(("programs", records))
            self.log(f"[{ts()}] Program windows refreshed")
        finally:
            self.ui_queue.put(("busy", False))

    async def save_program(self, zone, record):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            ok = await write_schedule(client, zone, record, debug=self.debug)
            if not ok:
                self.log(REPAIR_HINT)
                return

            self.ui_queue.put(("programs_patch", {zone: record}))
            confirmed = None
            for _attempt in range(10):
                await asyncio.sleep(0.6)
                confirmed = await read_schedule(client, zone, debug=self.debug,
                                                display=False)
                if confirmed and confirmed[:15] == record[:15]:
                    self.ui_queue.put(("programs_patch", {zone: confirmed}))
                    self.log(f"[{ts()}] Saved zone {zone} program")
                    return

            if confirmed:
                requested = record[1] * 60 + record[2]
                actual = confirmed[1] * 60 + confirmed[2]
                self.log(f"[{ts()}] Zone {zone} save was not confirmed: "
                         f"requested {requested} min, controller reports "
                         f"{actual} min")
            else:
                self.log(f"[{ts()}] Zone {zone} save was written but could "
                         "not be confirmed by readback")
        finally:
            self.ui_queue.put(("busy", False))

    async def set_seasonal(self, percent):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            await write_char(client, CHAR_VALVE, build_seasonal_payload(percent),
                             f"SEASONAL ADJUSTMENT {percent}%", debug=self.debug)
            self.log(f"[{ts()}] Seasonal adjustment set to {percent}%")
        finally:
            self.ui_queue.put(("busy", False))

    async def set_rainoff(self, days):
        client = self._require_client()
        self.ui_queue.put(("busy", True))
        try:
            await write_char(client, CHAR_VALVE, build_rainoff_payload(days),
                             f"RAIN OFF {days} days", debug=self.debug)
            self.log(f"[{ts()}] Rain off set to {days} days")
        finally:
            self.ui_queue.put(("busy", False))


class GalconGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Galcon GL6100 Control")
        self.geometry("1240x760")
        self.minsize(1080, 660)
        self.ui_queue = queue.Queue()
        self.session = GalconSession(self.ui_queue)
        self.program_records = {}
        self.active_zone = None
        self.remaining_seconds = 0
        self.status_seen_at = 0.0
        self.connected = False
        self.busy_count = 0
        self.connected_controls = []
        self.day_checkbuttons = []
        self.cyclic_controls = []
        self.weekly_controls = []
        self.zone_widgets = {}
        self.program_form = {}
        self.day_vars = [tk.BooleanVar(value=False) for _ in range(7)]
        self.day_label_vars = []
        self.mode_var = tk.StringVar(value="weekly")
        self.cyclic_hour_var = tk.IntVar(value=6)
        self.cyclic_minute_var = tk.IntVar(value=0)
        self._build_style()
        self._build_ui()
        self._sync_day_labels()
        self._sync_mode_controls()
        self._set_connected(False)
        self.after(100, self._process_queue)
        self.after(250, self._tick_countdown)
        self.after(10000, self._periodic_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f1eb")
        style.configure("Panel.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("TLabel", background="#f4f1eb", foreground="#1f2933")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#1f2933")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18), background="#f4f1eb")
        style.configure("Status.TLabel", font=("Segoe UI", 10), background="#f4f1eb")
        style.configure("Zone.TLabelframe", background="#ffffff")
        style.configure("Zone.TLabelframe.Label", font=("Segoe UI Semibold", 11))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10))
        style.configure("Treeview", rowheight=28)

    def _connected_widget(self, widget):
        self.connected_controls.append(widget)
        return widget

    def _set_widget_state(self, widgets, state):
        for widget in widgets:
            widget.configure(state=state)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        top = ttk.Frame(self, padding=14)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(6, weight=1)
        ttk.Label(top, text="Galcon GL6100", style="Title.TLabel").grid(row=0, column=0, padx=(0, 18))
        ttk.Label(top, text="Scan").grid(row=0, column=1, sticky="e")
        self.scan_var = tk.DoubleVar(value=60.0)
        ttk.Spinbox(top, from_=5, to=180, width=8, textvariable=self.scan_var).grid(row=0, column=2, padx=6)
        ttk.Label(top, text="PIN").grid(row=0, column=3, sticky="e")
        self.pin_var = tk.StringVar(value=load_saved_pin() or "")
        ttk.Entry(top, width=10, textvariable=self.pin_var, show="*").grid(row=0, column=4, padx=6)
        self.send_pin_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="send PIN", variable=self.send_pin_var).grid(row=0, column=5, padx=6)
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="debug", variable=self.debug_var).grid(row=0, column=6, sticky="w", padx=6)
        self.connect_button = ttk.Button(top, text="Connect", style="Accent.TButton", command=self._connect)
        self.connect_button.grid(row=0, column=7, padx=6)
        self.disconnect_button = ttk.Button(top, text="Disconnect", command=self._disconnect, state="disabled")
        self.disconnect_button.grid(row=0, column=8, padx=6)
        ttk.Button(top, text="Save PIN", command=self._save_pin).grid(row=0, column=9, padx=(6, 0))

        top2 = ttk.Frame(self, padding=(14, 0, 14, 8))
        top2.grid(row=1, column=0, sticky="ew")
        ttk.Label(top2, text="MAC (optional)").grid(row=0, column=0, sticky="e")
        self.mac_var = tk.StringVar(value=load_saved_mac() or "")
        ttk.Entry(top2, width=20, textvariable=self.mac_var).grid(row=0, column=1, padx=6)
        ttk.Button(top2, text="Save MAC", command=self._save_mac).grid(row=0, column=2, padx=(0, 6))

        status_bar = ttk.Frame(self, padding=(14, 0, 14, 8))
        status_bar.grid(row=2, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        self.connection_label = ttk.Label(status_bar, text="Disconnected", style="Status.TLabel")
        self.connection_label.grid(row=0, column=0, sticky="w")
        self.busy_label = ttk.Label(status_bar, text="", style="Status.TLabel")
        self.busy_label.grid(row=0, column=1, sticky="e")

        body = ttk.Frame(self, padding=(14, 0, 14, 14))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=4)
        body.rowconfigure(1, weight=1)

        zones_frame = ttk.LabelFrame(body, text="Zones", style="Zone.TLabelframe", padding=6)
        zones_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))
        for index in range(4):
            zones_frame.columnconfigure(index, weight=1)
            self._build_zone_card(zones_frame, index + 1, index)

        actions = ttk.LabelFrame(body, text="Device", padding=10)
        actions.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        for col in range(6):
            actions.columnconfigure(col, weight=1)
        self._connected_widget(ttk.Button(actions, text="Refresh Status",
                          command=self._refresh_status)).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._connected_widget(ttk.Button(actions, text="Refresh Programs",
                          command=self._refresh_programs)).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._connected_widget(ttk.Button(actions, text="Refresh All",
                  command=self._refresh_all)).grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        ttk.Label(actions, text="Seasonal %").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.seasonal_var = tk.IntVar(value=100)
        ttk.Spinbox(actions, from_=0, to=250, textvariable=self.seasonal_var, width=9).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        self._connected_widget(ttk.Button(actions, text="Apply",
                          command=self._set_seasonal)).grid(row=1, column=2, padx=4, pady=4, sticky="ew")
        ttk.Label(actions, text="Rain off days").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.rainoff_var = tk.IntVar(value=0)
        ttk.Spinbox(actions, from_=0, to=99, textvariable=self.rainoff_var, width=9).grid(row=2, column=1, sticky="w", padx=4, pady=4)
        self._connected_widget(ttk.Button(actions, text="Apply",
                          command=self._set_rainoff)).grid(row=2, column=2, padx=4, pady=4, sticky="ew")
        self.log_text = tk.Text(actions, height=7, wrap="word", relief="solid", bd=1)
        self.log_text.grid(row=3, column=0, columnspan=6, sticky="nsew", pady=(10, 0))
        actions.rowconfigure(3, weight=1)

        programs = ttk.LabelFrame(body, text="Program Windows", padding=12)
        programs.grid(row=0, column=1, rowspan=2, sticky="nsew")
        programs.columnconfigure(0, weight=1)
        programs.rowconfigure(4, weight=1)

        selector = ttk.Frame(programs)
        selector.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        selector.columnconfigure(3, weight=1)
        ttk.Label(selector, text="Zone").grid(row=0, column=0, sticky="w")
        self.edit_zone_var = tk.IntVar(value=1)
        zone_select = ttk.Combobox(selector, width=8, state="readonly",
                                   values=(1, 2, 3, 4),
                                   textvariable=self.edit_zone_var)
        zone_select.grid(row=0, column=1, padx=(8, 16), sticky="w")
        zone_select.bind("<<ComboboxSelected>>", self._load_zone_from_spin)
        self._connected_widget(ttk.Button(selector, text="Refresh",
                          command=self._refresh_programs)).grid(row=0, column=2, padx=(0, 8))
        self._connected_widget(ttk.Button(selector, text="Save Zone Program",
                                          style="Accent.TButton",
                                          command=self._save_program)).grid(row=0, column=4, sticky="e")

        summary = ttk.Frame(programs)
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for col in range(6):
            summary.columnconfigure(col, weight=1)
        ttk.Label(summary, text="Duration (min)").grid(row=0, column=0, sticky="w")
        self.edit_duration_var = tk.IntVar(value=0)
        ttk.Spinbox(summary, from_=0, to=600, width=12,
                    textvariable=self.edit_duration_var).grid(row=0, column=1, sticky="w", padx=(8, 22))
        ttk.Radiobutton(summary, text="Weekly", variable=self.mode_var,
                        value="weekly",
                        command=self._sync_mode_controls).grid(row=0, column=2, sticky="w", padx=(12, 6))
        ttk.Radiobutton(summary, text="Cyclic", variable=self.mode_var,
                        value="cyclic",
                        command=self._sync_mode_controls).grid(row=0, column=3, sticky="w", padx=6)

        self.weekly_frame = ttk.LabelFrame(programs, text="Weekly Days", padding=8)
        self.weekly_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        for idx, name in enumerate(DAY_NAMES):
            self.weekly_frame.columnconfigure(idx, weight=1)
            label_var = tk.StringVar(value=f"  {name}")
            self.day_label_vars.append(label_var)
            checkbutton = tk.Checkbutton(self.weekly_frame, textvariable=label_var,
                                         variable=self.day_vars[idx],
                                         indicatoron=False, width=8,
                                         command=self._sync_day_labels,
                                         bg="#ffffff", selectcolor="#dbeafe",
                                         relief="raised", overrelief="ridge")
            checkbutton.grid(row=0, column=idx, padx=3, sticky="ew")
            self.day_checkbuttons.append(checkbutton)
            self.weekly_controls.append(checkbutton)

        self.cyclic_frame = ttk.LabelFrame(programs, text="Cyclic Schedule", padding=8)
        self.cyclic_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        for col in range(8):
            self.cyclic_frame.columnconfigure(col, weight=1)
        ttk.Label(self.cyclic_frame, text="Start offset (days)").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.start_in_var = tk.IntVar(value=0)
        start_in_spin = ttk.Combobox(self.cyclic_frame, width=8,
                                     state="readonly",
                                     values=tuple(range(0, 15)),
                                     textvariable=self.start_in_var)
        start_in_spin.grid(row=0, column=1, sticky="w", padx=(0, 18))
        self.cyclic_controls.append(start_in_spin)
        ttk.Label(self.cyclic_frame, text="Cadence (days)").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.cadence_var = tk.IntVar(value=1)
        cadence_spin = ttk.Combobox(self.cyclic_frame, width=8,
                                    state="readonly",
                                    values=tuple(range(1, 15)),
                                    textvariable=self.cadence_var)
        cadence_spin.grid(row=0, column=3, sticky="w", padx=(0, 18))
        self.cyclic_controls.append(cadence_spin)
        ttk.Label(self.cyclic_frame, text="Start hour").grid(row=0, column=4, sticky="w", padx=(0, 8))
        cyclic_hour = ttk.Combobox(self.cyclic_frame, width=8,
                                   state="readonly",
                                   values=tuple(range(0, 24)),
                                   textvariable=self.cyclic_hour_var)
        cyclic_hour.grid(row=0, column=5, sticky="w", padx=(0, 18))
        self.cyclic_controls.append(cyclic_hour)
        ttk.Label(self.cyclic_frame, text="Start minute").grid(row=0, column=6, sticky="w", padx=(0, 8))
        cyclic_minute = ttk.Combobox(self.cyclic_frame, width=8,
                                     state="readonly",
                                     values=tuple(range(0, 60, 5)),
                                     textvariable=self.cyclic_minute_var)
        cyclic_minute.grid(row=0, column=7, sticky="w")
        self.cyclic_controls.append(cyclic_minute)

        self.windows_frame = ttk.LabelFrame(programs, text="Weekly Windows", padding=8)
        self.windows_frame.grid(row=3, column=0, sticky="ew")
        self.windows_frame.columnconfigure(0, weight=0)
        self.windows_frame.columnconfigure(1, weight=0)
        self.windows_frame.columnconfigure(2, weight=0)
        self.windows_frame.columnconfigure(3, weight=1)
        ttk.Label(self.windows_frame, text="Window").grid(row=0, column=0, sticky="w", padx=4, pady=(0, 5))
        ttk.Label(self.windows_frame, text="Use").grid(row=0, column=1, sticky="w", padx=4, pady=(0, 5))
        ttk.Label(self.windows_frame, text="Skip").grid(row=0, column=2, sticky="w", padx=4, pady=(0, 5))
        ttk.Label(self.windows_frame, text="Time").grid(row=0, column=3, sticky="w", padx=(24, 4), pady=(0, 5))
        self.window_hour_vars = []
        self.window_minute_vars = []
        self.window_enabled_vars = []
        self.window_time_controls = []
        for idx in range(4):
            hour_var = tk.IntVar(value=6)
            minute_var = tk.IntVar(value=0)
            enabled_var = tk.BooleanVar(value=False)
            self.window_hour_vars.append(hour_var)
            self.window_minute_vars.append(minute_var)
            self.window_enabled_vars.append(enabled_var)
            ttk.Label(self.windows_frame, text=str(idx + 1)).grid(row=idx + 1, column=0, sticky="w", padx=4, pady=5)
            ttk.Radiobutton(self.windows_frame, variable=enabled_var, value=True,
                            command=lambda i=idx: self._sync_window_enabled(i)).grid(row=idx + 1, column=1, sticky="w", padx=4, pady=5)
            ttk.Radiobutton(self.windows_frame, variable=enabled_var, value=False,
                            command=lambda i=idx: self._sync_window_enabled(i)).grid(row=idx + 1, column=2, sticky="w", padx=4, pady=5)
            time_cell = ttk.Frame(self.windows_frame)
            time_cell.grid(row=idx + 1, column=3, sticky="w", padx=(24, 4), pady=5)
            hour_box = ttk.Combobox(time_cell, width=5, state="readonly",
                                    values=tuple(range(0, 24)),
                                    textvariable=hour_var)
            hour_box.pack(side="left")
            ttk.Label(time_cell, text=":").pack(side="left", padx=3)
            minute_box = ttk.Combobox(time_cell, width=5, state="readonly",
                                      values=tuple(range(0, 60, 5)),
                                      textvariable=minute_var)
            minute_box.pack(side="left")
            self.window_time_controls.append((hour_box, minute_box))

        self.program_status_var = tk.StringVar(value="Refresh programs to load zone data.")
        ttk.Label(programs, textvariable=self.program_status_var).grid(row=4, column=0, sticky="nw", pady=(12, 0))

    def _build_zone_card(self, parent, zone, column):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=7)
        frame.grid(row=0, column=column, sticky="nsew", padx=3)
        frame.columnconfigure(0, weight=1)
        canvas = tk.Canvas(frame, width=38, height=38, bg="#ffffff", highlightthickness=0)
        indicator = canvas.create_oval(7, 7, 31, 31, fill="#9ca3af", outline="")
        canvas.grid(row=0, column=0, pady=(0, 2))
        ttk.Label(frame, text=f"Zone {zone}", style="Panel.TLabel", font=("Segoe UI Semibold", 10)).grid(row=1, column=0)
        state_label = ttk.Label(frame, text="Idle", style="Panel.TLabel")
        state_label.grid(row=2, column=0, pady=(4, 0))
        countdown_label = ttk.Label(frame, text="--:--", style="Panel.TLabel", font=("Segoe UI Semibold", 12))
        countdown_label.grid(row=3, column=0, pady=(2, 5))
        minutes_var = tk.IntVar(value=1)
        row = ttk.Frame(frame, style="Panel.TFrame")
        row.grid(row=4, column=0, pady=(0, 5))
        ttk.Label(row, text="min", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=1, to=600, width=5, textvariable=minutes_var).pack(side="left", padx=3)
        button_row = ttk.Frame(frame, style="Panel.TFrame")
        button_row.grid(row=5, column=0, sticky="ew")
        open_button = self._connected_widget(ttk.Button(
            button_row, text="Open", command=lambda z=zone: self._open_zone(z)))
        open_button.pack(side="left", expand=True, fill="x", padx=(0, 3))
        close_button = self._connected_widget(ttk.Button(
            button_row, text="Close", command=lambda z=zone: self._close_zone(z)))
        close_button.pack(side="left", expand=True, fill="x", padx=(3, 0))
        self.zone_widgets[zone] = {
            "canvas": canvas,
            "indicator": indicator,
            "state": state_label,
            "countdown": countdown_label,
            "minutes": minutes_var,
        }

    def _set_connected(self, connected):
        self.connected = connected
        self.connection_label.configure(text="Connected" if connected else "Disconnected")
        self.connect_button.configure(state="disabled" if connected else "normal")
        self.disconnect_button.configure(state="normal" if connected else "disabled")
        self._set_widget_state(self.connected_controls,
                               "normal" if connected else "disabled")
        if not connected:
            self.active_zone = None
            self.remaining_seconds = 0
            self._render_zone_status()

    def _set_busy(self, busy):
        self.busy_count += 1 if busy else -1
        self.busy_count = max(0, self.busy_count)
        self.busy_label.configure(text="Working..." if self.busy_count else "")

    def _connect(self):
        pin = self.pin_var.get().strip()
        if pin and (not pin.isdigit() or len(pin) != 4):
            messagebox.showerror("Invalid PIN", "PIN must be exactly 4 digits.")
            return
        mac = self.mac_var.get().strip()
        self.session.submit(self.session.connect(
            scan_time=float(self.scan_var.get()),
            send_pin=self.send_pin_var.get(),
            pin=pin or None,
            debug=self.debug_var.get(),
            mac=mac or None,
        ))

    def _disconnect(self):
        self.session.submit(self.session.disconnect())

    def _save_pin(self):
        pin = self.pin_var.get().strip()
        if not (pin.isdigit() and len(pin) == 4):
            messagebox.showerror("Invalid PIN", "PIN must be exactly 4 digits.")
            return
        save_pin(pin)
        self._log(f"[{ts()}] PIN saved")

    def _save_mac(self):
        mac = self.mac_var.get().strip()
        if not mac:
            messagebox.showerror("Invalid MAC", "Enter a MAC address first.")
            return
        save_mac(mac)
        self._log(f"[{ts()}] MAC saved")

    def _refresh_status(self):
        self._connected_call(self.session.read_status())

    def _refresh_programs(self):
        self._connected_call(self.session.read_programs())

    def _refresh_all(self):
        self._connected_call(self.session.refresh_all())

    def _open_zone(self, zone):
        minutes = self.zone_widgets[zone]["minutes"].get()
        self._connected_call(self.session.open_zone(zone, int(minutes)))

    def _close_zone(self, zone):
        self._connected_call(self.session.close_zone(zone))

    def _set_seasonal(self):
        self._connected_call(self.session.set_seasonal(int(self.seasonal_var.get())))

    def _set_rainoff(self):
        self._connected_call(self.session.set_rainoff(int(self.rainoff_var.get())))

    def _connected_call(self, coro):
        if not self.connected:
            messagebox.showinfo("Not connected", "Connect to the controller first.")
            coro.close()
            return
        self.session.submit(coro)

    def _process_queue(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(payload)
            elif kind == "connected":
                self._set_connected(payload)
            elif kind == "busy":
                self._set_busy(payload)
            elif kind == "status":
                self._apply_status(payload)
            elif kind == "programs":
                self.program_records = payload
                self._render_programs()
            elif kind == "programs_patch":
                self.program_records.update(payload)
                self._render_programs()
        self.after(100, self._process_queue)

    def _log(self, text):
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _apply_status(self, value):
        if not value or not any(value):
            self._log(f"[{ts()}] Status unavailable. {REPAIR_HINT}")
            return
        if value[0] == 0xff:
            self.active_zone = None
            self.remaining_seconds = 0
        else:
            self.active_zone = (value[0] & 0x0f) + 1
            self.remaining_seconds = max(0, value[2] * 60 + value[3])
        self.status_seen_at = time.monotonic()
        self._render_zone_status()

    def _tick_countdown(self):
        self._render_zone_status()
        self.after(250, self._tick_countdown)

    def _current_remaining(self):
        if self.active_zone is None:
            return 0
        elapsed = time.monotonic() - self.status_seen_at
        return max(0, int(round(self.remaining_seconds - elapsed)))

    def _render_zone_status(self):
        remaining = self._current_remaining()
        for zone, widgets in self.zone_widgets.items():
            active = zone == self.active_zone and remaining > 0
            color = "#16a34a" if active else "#9ca3af"
            widgets["canvas"].itemconfigure(widgets["indicator"], fill=color)
            widgets["state"].configure(text="Running" if active else "Idle")
            widgets["countdown"].configure(text=self._format_duration(remaining) if active else "--:--")

    def _periodic_status(self):
        if self.connected and self.busy_count == 0:
            self.session.submit(self.session.read_status())
        self.after(10000, self._periodic_status)

    def _render_programs(self):
        self._load_program(int(self.edit_zone_var.get()))

    def _format_mode(self, record):
        days = record[4]
        if days & 0x80:
            return f"cyclic +{record[13] - 0x80}d / every {record[14] - 0xC0}d"
        active = [DAY_NAMES[idx] for idx in range(7) if days & (1 << idx)]
        return ",".join(active) if active else "off"

    def _format_window(self, hour, minute):
        if hour == 0xff:
            return "off"
        if hour > 23 or minute > 59:
            return f"{hour:02x}:{minute:02x}"
        return f"{hour:02d}:{minute:02d}"

    def _load_zone_from_spin(self, _event=None):
        self._load_program(int(self.edit_zone_var.get()))

    def _load_program(self, zone):
        record = self.program_records.get(zone)
        if not record:
            self.program_status_var.set(
                f"Zone {zone} has not been loaded yet. Click Refresh.")
            return
        self.edit_zone_var.set(zone)
        self.edit_duration_var.set(record[1] * 60 + record[2])
        days = record[4]
        self.mode_var.set("cyclic" if days & 0x80 else "weekly")
        for idx, var in enumerate(self.day_vars):
            var.set(bool(days & (1 << idx)))
        self.start_in_var.set(max(0, record[13] - 0x80))
        self.cadence_var.set(max(1, record[14] - 0xC0))
        if record[5] != 0xff:
            self.cyclic_hour_var.set(record[5])
            self.cyclic_minute_var.set(self._nearest_five(record[6]))
        for idx, pos in enumerate(WINDOW_POSITIONS):
            text = self._format_window(record[pos], record[pos + 1])
            enabled = text != "off"
            self.window_enabled_vars[idx].set(enabled)
            if enabled:
                self.window_hour_vars[idx].set(record[pos])
                self.window_minute_vars[idx].set(self._nearest_five(record[pos + 1]))
            else:
                self.window_hour_vars[idx].set(0)
                self.window_minute_vars[idx].set(0)
            self._sync_window_enabled(idx)
        self.program_status_var.set(f"Zone {zone}: {self._format_mode(record)}")
        self._sync_day_labels()
        self._sync_mode_controls()

    def _sync_day_labels(self):
        for idx, name in enumerate(DAY_NAMES):
            mark = "x" if self.day_vars[idx].get() else " "
            self.day_label_vars[idx].set(f"{mark} {name}")

    def _sync_window_enabled(self, idx):
        state = "readonly" if self.window_enabled_vars[idx].get() else "disabled"
        if not self.window_enabled_vars[idx].get():
            self.window_hour_vars[idx].set(0)
            self.window_minute_vars[idx].set(0)
        for control in self.window_time_controls[idx]:
            control.configure(state=state)

    def _nearest_five(self, value):
        return min(55, max(0, int(round(value / 5) * 5)))

    def _sync_mode_controls(self):
        cyclic = self.mode_var.get() == "cyclic"
        if cyclic:
            self.weekly_frame.grid_remove()
            self.windows_frame.grid_remove()
            self.cyclic_frame.grid()
        else:
            self.cyclic_frame.grid_remove()
            self.weekly_frame.grid()
            self.windows_frame.grid()
        self._set_widget_state(self.weekly_controls,
                               "disabled" if cyclic else "normal")
        self._set_widget_state(self.cyclic_controls,
                               "readonly" if cyclic else "disabled")

    def _save_program(self):
        zone = int(self.edit_zone_var.get())
        base = self.program_records.get(zone)
        if not base:
            messagebox.showinfo("No program loaded", "Refresh programs before saving this zone.")
            return
        try:
            record = self._build_program_record(base)
        except ValueError as exc:
            messagebox.showerror("Invalid program", str(exc))
            return
        self._connected_call(self.session.save_program(zone, record))

    def _build_program_record(self, base):
        record = bytearray(base)
        duration = int(self.edit_duration_var.get())
        if duration < 0 or duration > 600:
            raise ValueError("Duration must be between 0 and 600 minutes.")
        record[1] = (duration // 60) & 0xFF
        record[2] = (duration % 60) & 0xFF
        if self.mode_var.get() == "cyclic":
            record[4] = 0x80
            record[5] = int(self.cyclic_hour_var.get()) & 0xFF
            record[6] = int(self.cyclic_minute_var.get()) & 0xFF
            record[13] = (0x80 + int(self.start_in_var.get())) & 0xFF
            record[14] = (0xC0 + int(self.cadence_var.get())) & 0xFF
        else:
            days = 0
            for idx, var in enumerate(self.day_vars):
                if var.get():
                    days |= 1 << idx
            record[4] = days
            record[11] = 0xff
            record[12] = 0x00
            record[13] = 0x80
            record[14] = 0x00
        for idx, pos in enumerate(WINDOW_POSITIONS):
            if not self.window_enabled_vars[idx].get():
                record[pos] = 0xff
                record[pos + 1] = 0x00
                continue
            record[pos] = int(self.window_hour_vars[idx].get()) & 0xFF
            record[pos + 1] = int(self.window_minute_vars[idx].get()) & 0xFF
        return bytes(record)

    def _parse_time(self, text):
        parts = text.strip().split(":")
        if len(parts) != 2:
            raise ValueError("Windows must be HH:MM or off.")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise ValueError("Windows must be HH:MM or off.") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Window times must be between 00:00 and 23:59.")
        return hour, minute

    def _format_duration(self, seconds):
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def _on_close(self):
        if self.connected:
            self.session.submit(self.session.disconnect())
        self.destroy()


def main():
    app = GalconGui()
    app.mainloop()


if __name__ == "__main__":
    main()
