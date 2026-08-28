"""Headless MQTT bridge for Home Assistant and a Galcon GL6100 controller.

The bridge keeps one BLE connection open, publishes Home Assistant MQTT
Discovery entities, polls status/programs, and accepts MQTT commands.

Run:
    python galcon_mqtt.py --mqtt-host homeassistant.local

State topics:
    galcon_gl6100/status
    galcon_gl6100/zone/1/state
    galcon_gl6100/zone/1/remaining
    galcon_gl6100/zone/1/program

Command topics:
    galcon_gl6100/zone/1/set       ON, OFF, or OPEN:5
    galcon_gl6100/zone/1/program/set  JSON schedule record/edit
    galcon_gl6100/device/seasonal/set  100
    galcon_gl6100/device/rainoff/set  0
    galcon_gl6100/refresh           any payload
"""

import argparse
import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from control_galcon import (
    APP_VERSION,
    CHAR_COMMAND,
    CHAR_POLL,
    CHAR_STATUS,
    CHAR_VALVE,
    GITHUB_REPO,
    REPAIR_HINT,
    SERVICE_UUID,
    STATUS_POLL,
    build_close_payload,
    build_open_payload,
    build_rainoff_payload,
    build_seasonal_payload,
    decode_active_zones,
    find_device,
    load_saved_mac,
    load_saved_pin,
    modify_schedule,
    read_schedule,
    write_char,
    write_schedule,
)

DEFAULT_PREFIX = "galcon_gl6100"
ZONE_COUNT = 4
DEFAULT_POLL_INTERVAL = 0
DEFAULT_IDLE_GRACE = 120
DEFAULT_BLE_CONNECT_TIMEOUT = 60
MQTT_CONFIG_PATH = Path(__file__).with_name("galcon_mqtt.json")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_tuple(value):
    return tuple(int(part) if part.isdigit() else 0
                 for part in str(value).lstrip("v").split("."))


def load_mqtt_config(path=MQTT_CONFIG_PATH):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class GalconMqttBridge:
    def __init__(self, args):
        self.args = args
        self.loop = asyncio.get_running_loop()
        self.mqtt = None
        self.mqtt_connected = asyncio.Event()
        self.client = None
        self.device = None
        self.status = bytes([0xff]) + bytes(19)
        self.programs = {}
        self.last_status_at = 0.0
        self.active_zone = None
        self.active_zones = []
        self.remaining_by_zone = {}
        self.remaining_seconds = 0
        self.command_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.status_event = asyncio.Event()
        self.http_server = None
        self.last_device = None
        self.idle_disconnect_task = None

    @property
    def prefix(self):
        return self.args.prefix.rstrip("/")

    def log(self, message):
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)

    def topic(self, suffix):
        return f"{self.prefix}/{suffix}"

    async def start(self):
        self._start_mqtt()
        try:
            await asyncio.wait_for(self.mqtt_connected.wait(), self.args.mqtt_timeout)
        except asyncio.TimeoutError as exc:
            if self.mqtt:
                self.mqtt.loop_stop()
                self.mqtt.disconnect()
            raise RuntimeError(
                f"MQTT broker did not respond within {self.args.mqtt_timeout:.0f}s. "
                f"Check --mqtt-host, --mqtt-port, credentials, and TLS settings.") from exc
        self._publish_discovery()
        if self.args.http_port:
            self.http_server = await asyncio.start_server(
                self._handle_http, self.args.http_host, self.args.http_port)
            self.log(f"HTTP API listening on {self.args.http_host}:{self.args.http_port}")
        await self._ble_loop()

    async def _handle_http(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            method, target, _version = request_line.decode("ascii").split()
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, value = line.decode("ascii").split(":", 1)
                headers[key.lower()] = value.strip()
            body = b""
            length = int(headers.get("content-length", "0"))
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
            status, response = await self._http_request(method, target, body)
        except Exception as exc:  # noqa: BLE001
            status, response = 400, {"error": str(exc)}
        encoded = json.dumps(response).encode("utf-8")
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request",
                  404: "Not Found", 503: "Service Unavailable"}.get(status, "Error")
        writer.write((f"HTTP/1.1 {status} {reason}\r\n"
                      "Content-Type: application/json\r\n"
                      "Cache-Control: no-store\r\n"
                      f"Content-Length: {len(encoded)}\r\n"
                      "Connection: close\r\n\r\n").encode("ascii") + encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _http_request(self, method, target, body):
        path = urlsplit(target).path.rstrip("/") or "/"
        if method == "GET" and path == "/api/status":
            return 200, self._status_json()
        if method == "GET" and path == "/api/programs":
            return 200, {str(zone): self._program_json(record)
                         for zone, record in self.programs.items()}
        if method == "POST" and path == "/api/refresh":
            asyncio.create_task(self._handle_mqtt(
                self.topic("refresh"), b"refresh"))
            return 202, {"accepted": True}
        if method == "POST" and path.startswith("/api/zone/") and path.endswith("/set"):
            zone = int(path.split("/")[3])
            command = json.loads(body.decode("utf-8")) if body else {}
            value = command.get("command", "") if isinstance(command, dict) else command
            asyncio.create_task(self._handle_mqtt(
                self.topic(f"zone/{zone}/set"), str(value).encode()))
            return 202, {"accepted": True, "zone": zone}
        if method == "POST" and path == "/api/device/seasonal":
            value = int(json.loads(body.decode("utf-8")).get("value"))
            asyncio.create_task(self._handle_mqtt(
                self.topic("device/seasonal/set"), str(value).encode()))
            return 202, {"accepted": True, "value": value}
        if method == "POST" and path == "/api/device/rainoff":
            value = int(json.loads(body.decode("utf-8")).get("value"))
            asyncio.create_task(self._handle_mqtt(
                self.topic("device/rainoff/set"), str(value).encode()))
            return 202, {"accepted": True, "value": value}
        return 404, {"error": "not found"}

    def _status_json(self):
        elapsed = int(time.monotonic() - self.last_status_at)
        remaining = max(0, self.remaining_seconds - elapsed)
        remaining_by_zone = {
            str(zone): max(0, seconds - elapsed)
            for zone, seconds in self.remaining_by_zone.items()
        }
        return {"status": "idle" if not self.active_zones else "running",
                "active_zone": self.active_zone or 0,
                "active_zones": self.active_zones,
                "remaining_seconds": remaining,
                "remaining_by_zone": remaining_by_zone,
                "ble": self.client is not None and self.client.is_connected,
                "updated_at": utc_now()}

    def _program_json(self, record):
        days = record[4]
        return {"duration_minutes": record[1] * 60 + record[2],
                "mode": "cyclic" if days & 0x80 else "weekly",
                "days_mask": days & 0x7f,
                "start_hour": record[5], "start_minute": record[6]}

    def _start_mqtt(self):
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                client_id=self.args.mqtt_client_id)
        if self.args.mqtt_username:
            self.mqtt.username_pw_set(self.args.mqtt_username,
                                      self.args.mqtt_password)
        if self.args.mqtt_tls:
            self.mqtt.tls_set()
        self.mqtt.will_set(self.topic("availability"), "offline", qos=1, retain=True)
        self.mqtt.on_connect = self._on_mqtt_connect
        self.mqtt.on_message = self._on_mqtt_message
        self.mqtt.connect_async(self.args.mqtt_host, self.args.mqtt_port,
                                keepalive=60)
        self.mqtt.loop_start()

    def _on_mqtt_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            self.log(f"MQTT connection failed: {reason_code}")
            return
        self.log(f"MQTT connected to {self.args.mqtt_host}:{self.args.mqtt_port}")
        self.mqtt_connected.set()
        self.mqtt.subscribe(self.topic("zone/+/set"), qos=1)
        self.mqtt.subscribe(self.topic("zone/+/program/set"), qos=1)
        self.mqtt.subscribe(self.topic("device/seasonal/set"), qos=1)
        self.mqtt.subscribe(self.topic("device/rainoff/set"), qos=1)
        self.mqtt.subscribe(self.topic("refresh"), qos=1)
        self.mqtt.publish(self.topic("availability"), "online", qos=1, retain=True)

    def _on_mqtt_message(self, _client, _userdata, message):
        future = asyncio.run_coroutine_threadsafe(
            self._handle_mqtt(message.topic, message.payload), self.loop)
        future.add_done_callback(self._log_future_error)

    def _log_future_error(self, future):
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self.log(f"MQTT command failed: {type(exc).__name__}: {exc}")
            self._publish("error", str(exc))

    async def _ble_loop(self):
        if self.args.poll_interval <= 0:
            await self.stop_event.wait()
            return
        while not self.stop_event.is_set():
            try:
                await self._connect_ble()
                if self.args.keep_connected:
                    await self._connected_loop()
                else:
                    await self._disconnect_ble()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log(f"BLE session failed: {type(exc).__name__}: {exc}")
                self._publish("error", f"{type(exc).__name__}: {exc}")
                self._publish("ble", "disconnected")
            finally:
                await self._disconnect_ble()
            await asyncio.sleep(self.args.poll_interval)

    async def _connect_ble(self, load_programs=True):
        if self.client and self.client.is_connected:
            return
        self._publish("ble", "scanning")
        mac = self.args.mac or load_saved_mac()
        if self.last_device is not None:
            try:
                self.device = self.last_device
                self._publish("ble", "connecting")
                self.client = __import__("bleak", fromlist=["BleakClient"]).BleakClient(
                    self.device, timeout=self.args.ble_connect_timeout,
                    services=[SERVICE_UUID],
                    winrt=dict(use_cached_services=True))
                await self.client.connect()
            except Exception:  # noqa: BLE001
                await self._disconnect_ble()
                self.client = None

        if self.client is None:
            self.device = await find_device(self.args.scan_time, mac=mac)
            if self.device is None:
                raise RuntimeError("Controller not found; press a controller button to wake it")
            self.last_device = self.device
            self._publish("ble", "connecting")
            last_error = None
            for attempt in range(2):
                self.client = __import__("bleak", fromlist=["BleakClient"]).BleakClient(
                    self.device, timeout=self.args.ble_connect_timeout,
                    services=[SERVICE_UUID],
                    winrt=dict(use_cached_services=True))
                try:
                    await self.client.connect()
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    await self._disconnect_ble()
                    if attempt == 0:
                        self.log("BLE connect timed out/failed; retrying "
                                 "the discovered device...")
            else:
                raise last_error
        await self.client.start_notify(CHAR_STATUS, self._on_status_notify)
        self._publish("ble", "connected")
        self.log("BLE connected")
        await self._poll_status()
        if load_programs:
            await self._poll_programs()

    async def _disconnect_ble(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.log(f"BLE disconnect failed: {exc}")

    def _reset_idle_disconnect(self):
        if self.idle_disconnect_task is not None:
            self.idle_disconnect_task.cancel()
            self.idle_disconnect_task = None

    def _arm_idle_disconnect(self):
        if self.args.keep_connected:
            return
        self._reset_idle_disconnect()
        self.idle_disconnect_task = asyncio.create_task(
            self._disconnect_after_idle())

    async def _disconnect_after_idle(self):
        try:
            await asyncio.sleep(self.args.idle_grace)
            if self.client and self.client.is_connected:
                self.log(f"BLE idle for {self.args.idle_grace:.0f}s; disconnecting")
                await self._disconnect_ble()
        except asyncio.CancelledError:
            pass

    async def _connected_loop(self):
        while self.client and self.client.is_connected and not self.stop_event.is_set():
            await self._poll_status()
            await asyncio.sleep(self.args.poll_interval)

    async def _ensure_ble_connected(self, load_programs=False):
        if self.client and self.client.is_connected:
            return
        await self._connect_ble(load_programs=load_programs)

    def _on_status_notify(self, _sender, data):
        self.status = bytes(data)
        self.status_event.set()
        self._publish_status(self.status)

    async def _poll_status(self):
        async with self.command_lock:
            self.status_event.clear()
            await self.client.write_gatt_char(CHAR_POLL, STATUS_POLL, response=True)
            try:
                await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
                value = self.status
            except asyncio.TimeoutError:
                value = bytes(await self.client.read_gatt_char(CHAR_STATUS))
            if value is not None and any(value):
                self.status = value
        self._publish_status(self.status)

    def _publish_status(self, value):
        if not value or not any(value):
            return
        zone_times = decode_active_zones(value)
        if zone_times == []:
            self.active_zone = None
            self.active_zones = []
            self.remaining_by_zone = {}
            self.remaining_seconds = 0
        elif zone_times is not None:
            self.remaining_by_zone = dict(zone_times)
            self.active_zones = [zone for zone, seconds
                                 in self.remaining_by_zone.items()
                                 if seconds > 0]
            if self.active_zones:
                self.active_zone = self.active_zones[0]
                self.remaining_seconds = self.remaining_by_zone[self.active_zone]
            else:
                self.active_zone = None
                self.remaining_seconds = 0
        else:
            self.log(f"Ignoring invalid status frame zone byte 0x{value[0]:02x}")
            self._publish("status", "unknown")
            self._publish("active_zone", 0)
            self._publish("active_zones", "[]")
            self._publish("error", f"Invalid status frame: 0x{value[0]:02x}")
            return
        self.last_status_at = time.monotonic()
        active = self.active_zone
        self._publish("status", "idle" if not self.active_zones else "running")
        self._publish("active_zone", active or 0)
        self._publish("active_zones", json.dumps(self.active_zones))
        for zone in range(1, ZONE_COUNT + 1):
            zone_remaining = self.remaining_by_zone.get(zone, 0)
            is_active = zone in self.active_zones and zone_remaining > 0
            self._publish(f"zone/{zone}/state", "ON" if is_active else "OFF")
            self._publish(f"zone/{zone}/remaining", zone_remaining
                          if is_active else 0)
        self._publish("last_update", utc_now())

    async def _poll_programs(self):
        async with self.command_lock:
            for zone in range(1, ZONE_COUNT + 1):
                record = await read_schedule(self.client, zone, display=False)
                if record:
                    self.programs[zone] = record
                    self._publish_program(zone, record)

    async def _handle_mqtt(self, topic, payload):
        text = payload.decode("utf-8").strip()
        if not self.args.keep_connected:
            self._reset_idle_disconnect()
        try:
            async with self.request_lock:
                await self._ensure_ble_connected(
                    load_programs=topic == self.topic("refresh")
                    or topic.endswith("/program/set"))
                if topic == self.topic("refresh"):
                    await self._poll_status()
                    await self._poll_programs()
                    return
                if topic == self.topic("device/seasonal/set"):
                    value = int(text)
                    if not 0 <= value <= 250:
                        raise ValueError("seasonal adjustment must be 0-250")
                    async with self.command_lock:
                        await write_char(self.client, CHAR_VALVE,
                                         build_seasonal_payload(value),
                                         f"SEASONAL {value}%")
                    self._publish("device/seasonal", value)
                    return
                if topic == self.topic("device/rainoff/set"):
                    value = int(text)
                    if not 0 <= value <= 255:
                        raise ValueError("rain-off days must be 0-255")
                    async with self.command_lock:
                        await write_char(self.client, CHAR_VALVE,
                                         build_rainoff_payload(value),
                                         f"RAIN OFF {value} days")
                    self._publish("device/rainoff", value)
                    return
                parts = topic.split("/")
                if len(parts) >= 3 and parts[-1] == "set" and parts[-2].isdigit():
                    zone = int(parts[-2])
                    if parts[-3] == "zone":
                        await self._set_zone(zone, text)
                        return
                if len(parts) >= 4 and parts[-2:] == ["program", "set"]:
                    zone = int(parts[-3])
                    await self._set_program(zone, text)
        finally:
            if not self.args.keep_connected and not self.stop_event.is_set():
                self._arm_idle_disconnect()

    async def _set_zone(self, zone, value):
        if not 1 <= zone <= ZONE_COUNT:
            raise ValueError("zone must be 1-4")
        if value.upper() in ("OFF", "0", "CLOSE"):
            payload = build_close_payload(zone)
        else:
            minutes = 1
            if ":" in value:
                command, raw_minutes = value.split(":", 1)
                if command.upper() not in ("ON", "OPEN"):
                    raise ValueError("zone command must be ON, OFF, OPEN:n, or CLOSE")
                minutes = int(raw_minutes)
            elif value.upper() not in ("ON", "OPEN", "1"):
                raise ValueError("zone command must be ON, OFF, OPEN:n, or CLOSE")
            payload = build_open_payload(zone, minutes)
        async with self.command_lock:
            await write_char(self.client, CHAR_VALVE, payload, f"ZONE {zone} {value}")
        await asyncio.sleep(1.0)
        await self._poll_status()

    async def _set_program(self, zone, text):
        if not 1 <= zone <= ZONE_COUNT:
            raise ValueError("zone must be 1-4")
        try:
            changes = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"program command must be JSON: {exc}") from exc
        if not isinstance(changes, dict):
            raise ValueError("program command must be a JSON object")
        record = self.programs.get(zone)
        if record is None:
            record = await read_schedule(self.client, zone, display=False)
        if record is None:
            raise RuntimeError(f"could not read zone {zone} program")
        allowed = {
            "duration": "duration_minutes",
            "hour": "hour",
            "minute": "minute",
            "days": "days_mask",
            "cadence": "cadence_days",
            "start_in": "start_in_days",
        }
        kwargs = {allowed[key]: value for key, value in changes.items()
                  if key in allowed}
        updated = modify_schedule(record, **kwargs)
        async with self.command_lock:
            if not await write_schedule(self.client, zone, updated):
                raise RuntimeError(REPAIR_HINT)
            self.programs[zone] = updated
            self._publish_program(zone, updated)

    def _publish_program(self, zone, record):
        days = record[4]
        payload = {
            "zone": zone,
            "duration_minutes": record[1] * 60 + record[2],
            "mode": "cyclic" if days & 0x80 else "weekly",
            "days_mask": days & 0x7f,
            "start_hour": record[5],
            "start_minute": record[6],
            "windows": [
                {"enabled": record[pos] != 0xff,
                 "hour": None if record[pos] == 0xff else record[pos],
                 "minute": None if record[pos] == 0xff else record[pos + 1]}
                for pos in (5, 7, 9, 11)
            ],
        }
        if days & 0x80:
            payload["start_in_days"] = max(0, record[13] - 0x80)
            payload["cadence_days"] = max(0, record[14] - 0xc0)
        self._publish(f"zone/{zone}/program", json.dumps(payload))

    def _publish_discovery(self):
        device = {
            "identifiers": [self.prefix],
            "name": "Galcon GL6100",
            "manufacturer": "Galcon",
            "model": "GL6100",
            "sw_version": APP_VERSION,
            "configuration_url": f"https://github.com/{GITHUB_REPO}",
        }
        self._discovery("binary_sensor", "connection", "Connection", "ble",
                None, {"payload_on": "connected",
                       "payload_off": "disconnected"}, device)
        self._discovery("sensor", "status", "Status", "status", "status",
                        {}, device)
        self._discovery("sensor", "active_zones", "Active zones", "active_zones",
                None, {"icon": "mdi:valve"}, device)
        self._discovery("sensor", "last_update", "Last update", "last_update",
                        "last_update", {}, device)
        for zone in range(1, ZONE_COUNT + 1):
            zd = {**device, "identifiers": [f"{self.prefix}_zone_{zone}"],
                  "name": f"Galcon GL6100 Zone {zone}"}
            self._discovery("switch", f"zone_{zone}", f"Zone {zone}",
                            f"zone/{zone}/state", f"zone/{zone}/set",
                            {}, zd, payload_on="ON", payload_off="OFF")
            self._discovery("sensor", f"zone_{zone}_remaining",
                            f"Zone {zone} remaining", f"zone/{zone}/remaining",
                            f"zone/{zone}/remaining", {"unit_of_measurement": "s"}, zd)
            self._discovery("sensor", f"zone_{zone}_program",
                            f"Zone {zone} program", f"zone/{zone}/program",
                            f"zone/{zone}/program", {"icon": "mdi:calendar-clock"}, zd)
        self._discovery("number", "seasonal", "Seasonal adjustment",
                        "device/seasonal", "device/seasonal/set",
                        {"min": 0, "max": 250, "step": 1, "unit_of_measurement": "%"}, device)
        self._discovery("number", "rainoff", "Rain off",
                        "device/rainoff", "device/rainoff/set",
                        {"min": 0, "max": 255, "step": 1,
                         "unit_of_measurement": "d"}, device)
        # Clear the old single-zone entity from brokers that retained it.
        self.mqtt.publish(
            f"homeassistant/sensor/{self.prefix}/active_zone/config",
            "", qos=1, retain=True)

    def _discovery(self, component, object_id, name, state_suffix,
                   command_suffix, extra, device, **kwargs):
        config = {
            "name": name,
            "unique_id": f"{self.prefix}_{object_id}",
            "state_topic": self.topic(state_suffix),
            "availability_topic": self.topic("availability"),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            **extra,
        }
        if command_suffix:
            config["command_topic"] = self.topic(command_suffix)
        config.update(kwargs)
        self.mqtt.publish(
            f"homeassistant/{component}/{self.prefix}/{object_id}/config",
            json.dumps(config), qos=1, retain=True)

    def _publish(self, suffix, payload):
        if self.mqtt is not None:
            self.mqtt.publish(self.topic(suffix), str(payload), qos=1, retain=True)

    async def stop(self):
        self.stop_event.set()
        idle_task = self.idle_disconnect_task
        self._reset_idle_disconnect()
        if idle_task is not None:
            try:
                await idle_task
            except asyncio.CancelledError:
                pass
        await self._disconnect_ble()
        if self.http_server:
            self.http_server.close()
            await self.http_server.wait_closed()
        if self.mqtt:
            self.mqtt.publish(self.topic("availability"), "offline", qos=1, retain=True)
            self.mqtt.loop_stop()
            self.mqtt.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int)
    parser.add_argument("--mqtt-username")
    parser.add_argument("--mqtt-password")
    parser.add_argument("--mqtt-tls", action="store_true", default=None)
    parser.add_argument("--mqtt-client-id")
    parser.add_argument("--mqtt-timeout", type=float)
    parser.add_argument("--http-host")
    parser.add_argument("--http-port", type=int,
                        help="Optional local HTTP API port; 0 disables it")
    parser.add_argument("--prefix")
    parser.add_argument("--mac")
    parser.add_argument("--scan-time", type=float)
    parser.add_argument("--poll-interval", type=float,
                        help="Enable background BLE polling at this many "
                            "seconds. Disabled by default; set 0 for "
                            "command-only on-demand connections.")
    parser.add_argument("--keep-connected", action="store_true", default=None,
                        help="Keep BLE connected between polls/commands. "
                            "Off by default to reduce controller battery use.")
    parser.add_argument("--idle-grace", type=float,
                        help="Seconds to keep BLE connected after a command "
                             "for follow-up commands; default 120")
    parser.add_argument("--ble-connect-timeout", type=float,
                        help="Seconds allowed for Windows/Bleak BLE connect; "
                             "default 60")
    parser.add_argument("--reconnect-delay", type=float,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load_mqtt_config()
    for name in ("mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password",
                 "mqtt_tls", "mqtt_client_id", "mqtt_timeout", "http_host",
                 "http_port", "prefix", "mac", "scan_time", "poll_interval",
                 "keep_connected", "idle_grace", "ble_connect_timeout",
                 "reconnect_delay"):
        if getattr(args, name) is None and name in config:
            setattr(args, name, config[name])
    args.mqtt_host = args.mqtt_host or ""
    args.mqtt_port = args.mqtt_port if args.mqtt_port is not None else 1883
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
                       if args.idle_grace is not None
                       else DEFAULT_IDLE_GRACE)
    if args.idle_grace < 0:
        parser.error("--idle-grace cannot be negative")
    args.ble_connect_timeout = (args.ble_connect_timeout
                                if args.ble_connect_timeout is not None
                                else DEFAULT_BLE_CONNECT_TIMEOUT)
    if args.ble_connect_timeout <= 0:
        parser.error("--ble-connect-timeout must be positive")
    args.reconnect_delay = (args.reconnect_delay
                            if args.reconnect_delay is not None else 10.0)
    if not args.mqtt_host:
        parser.error("--mqtt-host is required (or set mqtt_host in "
                     "galcon_mqtt.json)")

    async def runner():
        bridge = GalconMqttBridge(args)
        try:
            await bridge.start()
        finally:
            await bridge.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("\nStopped.")
    except RuntimeError as exc:
        print(f"MQTT bridge startup failed: {exc}")
        return 1


if __name__ == "__main__":
    main()
