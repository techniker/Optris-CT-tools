#!/usr/bin/env python3
# Optris CT | Infrared temperature sensor readout (burst or polling)
# Publish values to MQTT and optionally display locally (curses UI)
#
# <tec att sixtopia.net> Bjoern Heller
#

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

import serial
from serial import SerialException

import paho.mqtt.client as mqtt

try:
    import curses
except ImportError:
    curses = None


# ─────────────────────────────────────────────────────────────
# Protocol basics (per Optris CT commands doc)
# Temperatures: (raw - 1000) / 10  [°C]
# Fractions (emissivity/transmission): raw / 1000  [-]
# Burst mode frames: AA AA + payload (2 bytes per configured half-byte entry)
# ─────────────────────────────────────────────────────────────

SYNC = b"\xAA\xAA"

# Polling commands (1 byte cmd, 2 bytes response)
POLL_COMMANDS = {
    "process_temperature": bytes([0x01]),   # target temp
    "head_temperature":    bytes([0x02]),
    "box_temperature":     bytes([0x03]),
    "actual_temperature":  bytes([0x81]),   # current target temp
    "emissivity":          bytes([0x04]),
    "transmission":        bytes([0x05]),
}

# Burst half-byte mapping (configured on device)
BURST_HALFBYTE_TO_KEY = {
    1: "process_temperature",
    2: "head_temperature",
    3: "box_temperature",
    4: "actual_temperature",
    5: "emissivity",
    6: "transmission",
}

TEMP_KEYS = {
    "process_temperature",
    "head_temperature",
    "box_temperature",
    "actual_temperature",
}

FRAC_KEYS = {
    "emissivity",
    "transmission",
}


def decode_temp_u16(raw: int) -> float:
    return (raw - 1000) / 10.0


def decode_frac_u16(raw: int) -> float:
    return raw / 1000.0


def u16_be(b: bytes) -> int:
    return (b[0] << 8) | b[1]


def clamp_str(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


@dataclass
class SharedState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    latest: Dict[str, float] = field(default_factory=dict)
    last_update_ts: float = 0.0
    last_frame_ok: bool = False
    frames_ok: int = 0
    frames_bad: int = 0
    bytes_in: int = 0
    mqtt_ok: bool = False
    mqtt_last_err: str = ""
    serial_last_err: str = ""
    mode: str = "burst"


class OptrisCT:
    def __init__(self, ser: serial.Serial, state: SharedState, multidrop_addr: Optional[int] = None):
        self.ser = ser
        self.state = state
        self.addr_prefix = self._addr_prefix(multidrop_addr)

        # serial access lock (important if you ever mix polling + burst control)
        self.io_lock = threading.Lock()

    @staticmethod
    def _addr_prefix(multidrop_addr: Optional[int]) -> bytes:
        # RS232/USB typically responds to any address; RS485 uses prefix B0+addr.
        if multidrop_addr is None:
            return b""
        if not (1 <= multidrop_addr <= 79):
            raise ValueError("multidrop address must be in 1..79")
        return bytes([0xB0 + multidrop_addr])

    def _write(self, payload: bytes) -> None:
        with self.io_lock:
            self.ser.write(self.addr_prefix + payload)

    def _read_exact(self, n: int, timeout_s: float) -> Optional[bytes]:
        # Implement our own readexactly with timeout to avoid hanging forever.
        deadline = time.monotonic() + timeout_s
        out = bytearray()
        while len(out) < n and time.monotonic() < deadline:
            chunk = self.ser.read(n - len(out))
            if chunk:
                out += chunk
            else:
                # short sleep prevents busy loop when timeout is small
                time.sleep(0.001)
        if len(out) == n:
            return bytes(out)
        return None

    def poll_once(self, per_command_timeout_s: float = 0.2) -> Dict[str, float]:
        data: Dict[str, float] = {}
        for key, cmd in POLL_COMMANDS.items():
            try:
                self._write(cmd)
                resp = self._read_exact(2, per_command_timeout_s)
                if not resp:
                    continue
                raw = u16_be(resp)
                if key in TEMP_KEYS:
                    data[key] = decode_temp_u16(raw)
                else:
                    data[key] = decode_frac_u16(raw)
            except SerialException:
                raise
            except Exception:
                # Bad data shouldn't crash; just skip this field
                continue
        return data

    # ─────────────────────────────────────────────
    # Burst configuration helpers
    # Burst string: 8 half-bytes packed into 4 bytes
    # half-byte values: 1..6 used, 0 terminates
    # ─────────────────────────────────────────────
    @staticmethod
    def pack_burst_string(halfbytes: List[int]) -> bytes:
        hb = list(halfbytes[:8])
        # Ensure termination (0) exists and pad to 8
        if 0 not in hb:
            hb.append(0)
        hb = hb[:8]
        hb += [0] * (8 - len(hb))

        packed = bytearray()
        for i in range(0, 8, 2):
            hi = hb[i] & 0x0F
            lo = hb[i + 1] & 0x0F
            packed.append((hi << 4) | lo)
        return bytes(packed)  # 4 bytes

    def set_burst_string(self, halfbytes: List[int]) -> None:
        b = self.pack_burst_string(halfbytes)
        # 0x51 + 4 bytes payload
        self._write(bytes([0x51]) + b)

    def start_burst(self) -> None:
        # 0x52 0x01
        self._write(bytes([0x52, 0x01]))

    def stop_burst(self) -> None:
        # 0x52 0x00
        self._write(bytes([0x52, 0x00]))


class BurstReader(threading.Thread):
    def __init__(
        self,
        ser: serial.Serial,
        state: SharedState,
        burst_halfbytes: List[int],
        stop_evt: threading.Event,
        max_buffer: int = 8192,
    ):
        super().__init__(daemon=True)
        self.ser = ser
        self.state = state
        self.stop_evt = stop_evt
        self.max_buffer = max_buffer

        # Build burst key order from configured halfbytes until first 0
        keys: List[str] = []
        for hb in burst_halfbytes:
            if hb == 0:
                break
            k = BURST_HALFBYTE_TO_KEY.get(hb)
            if k:
                keys.append(k)
        self.keys = keys
        self.payload_len = 2 * len(self.keys)

        self.buf = bytearray()

    def run(self) -> None:
        # Configure serial read timeout so read() returns regularly
        if self.ser.timeout is None or self.ser.timeout > 0.2:
            self.ser.timeout = 0.2

        try:
            while not self.stop_evt.is_set():
                try:
                    chunk = self.ser.read(512)
                except SerialException as e:
                    with self.state.lock:
                        self.state.serial_last_err = f"{type(e).__name__}: {e}"
                    time.sleep(0.2)
                    continue

                if not chunk:
                    continue

                self.buf += chunk
                with self.state.lock:
                    self.state.bytes_in += len(chunk)

                # Bound buffer (avoid unbounded growth if sync is lost)
                if len(self.buf) > self.max_buffer:
                    # keep the tail where a sync might still appear
                    self.buf = self.buf[-self.max_buffer :]

                # Parse as many frames as possible
                self._parse_frames()
        except Exception as e:
            # Never crash the whole program due to an unexpected reader exception
            with self.state.lock:
                self.state.serial_last_err = f"Reader crashed: {type(e).__name__}: {e}"

    def _parse_frames(self) -> None:
        # We search for SYNC, then require full payload after it.
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                # No sync in buffer; keep only last byte (in case next is AA)
                if len(self.buf) > 1:
                    self.buf = self.buf[-1:]
                return

            # Discard everything before sync
            if i > 0:
                del self.buf[:i]

            # Need SYNC (2) + payload_len
            if len(self.buf) < 2 + self.payload_len:
                return

            # Extract payload and advance buffer
            payload = bytes(self.buf[2 : 2 + self.payload_len])
            del self.buf[: 2 + self.payload_len]

            decoded = self._decode_payload(payload)
            if decoded is None:
                with self.state.lock:
                    self.state.frames_bad += 1
                    self.state.last_frame_ok = False
                # Continue scanning; we already advanced by frame length.
                continue

            with self.state.lock:
                self.state.latest.update(decoded)
                self.state.last_update_ts = time.time()
                self.state.frames_ok += 1
                self.state.last_frame_ok = True

    def _decode_payload(self, payload: bytes) -> Optional[Dict[str, float]]:
        if len(payload) != self.payload_len:
            return None

        out: Dict[str, float] = {}
        try:
            for idx, key in enumerate(self.keys):
                b = payload[2 * idx : 2 * idx + 2]
                raw = u16_be(b)

                # Decode by type
                if key in TEMP_KEYS:
                    val = decode_temp_u16(raw)
                    # Basic sanity gate: reject clearly broken frames
                    # (keeps GUI from flickering wildly on misalignment)
                    if val < -100.0 or val > 2000.0:
                        return None
                    out[key] = val
                elif key in FRAC_KEYS:
                    val = decode_frac_u16(raw)
                    if val < 0.0 or val > 1.2:
                        return None
                    out[key] = val
                else:
                    # unknown key => ignore
                    pass
        except Exception:
            return None
        return out


class PollingReader(threading.Thread):
    def __init__(
        self,
        device: OptrisCT,
        state: SharedState,
        stop_evt: threading.Event,
        interval_s: float,
    ):
        super().__init__(daemon=True)
        self.device = device
        self.state = state
        self.stop_evt = stop_evt
        self.interval_s = max(0.02, float(interval_s))

    def run(self) -> None:
        while not self.stop_evt.is_set():
            try:
                data = self.device.poll_once(per_command_timeout_s=0.2)
                if data:
                    with self.state.lock:
                        self.state.latest.update(data)
                        self.state.last_update_ts = time.time()
                        self.state.frames_ok += 1
                        self.state.last_frame_ok = True
                else:
                    with self.state.lock:
                        self.state.frames_bad += 1
                        self.state.last_frame_ok = False
            except SerialException as e:
                with self.state.lock:
                    self.state.serial_last_err = f"{type(e).__name__}: {e}"
            except Exception as e:
                with self.state.lock:
                    self.state.serial_last_err = f"{type(e).__name__}: {e}"

            time.sleep(self.interval_s)


class MqttPublisher(threading.Thread):
    def __init__(
        self,
        state: SharedState,
        stop_evt: threading.Event,
        broker: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
        topic_base: str,
        publish_interval_s: float,
        publish_json: bool,
        publish_per_field: bool,
        retain: bool,
        client_id: str,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.stop_evt = stop_evt

        self.broker = broker
        self.port = port
        self.username = username
        self.password = password

        self.topic_base = topic_base.rstrip("/")
        self.publish_interval_s = max(0.05, float(publish_interval_s))
        self.publish_json = publish_json
        self.publish_per_field = publish_per_field
        self.retain = retain

        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if self.username is not None and self.password is not None:
            self.client.username_pw_set(self.username, self.password)

        # keep callbacks small and safe
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        # paho will do exponential-ish backoff if configured
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._last_pub_ts = 0.0
        self._last_payload: Optional[Tuple[Tuple[Tuple[str, float], ...], float]] = None

    def _on_connect(self, client, userdata, flags, rc):
        ok = (rc == 0)
        with self.state.lock:
            self.state.mqtt_ok = ok
            self.state.mqtt_last_err = "" if ok else f"connect rc={rc}"

    def _on_disconnect(self, client, userdata, rc):
        with self.state.lock:
            self.state.mqtt_ok = False
            if rc != 0:
                self.state.mqtt_last_err = f"disconnect rc={rc}"

    def run(self) -> None:
        try:
            self.client.connect(self.broker, self.port, keepalive=30)
            self.client.loop_start()
        except Exception as e:
            with self.state.lock:
                self.state.mqtt_ok = False
                self.state.mqtt_last_err = f"{type(e).__name__}: {e}"

        while not self.stop_evt.is_set():
            now = time.monotonic()
            if now - self._last_pub_ts >= self.publish_interval_s:
                snap = self._snapshot()
                if snap is not None:
                    self._publish_snapshot(snap)
                self._last_pub_ts = now
            time.sleep(0.02)

        try:
            self.client.loop_stop()
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass

    def _snapshot(self) -> Optional[Dict[str, float]]:
        with self.state.lock:
            if not self.state.latest:
                return None
            # copy
            return dict(self.state.latest)

    def _publish_snapshot(self, data: Dict[str, float]) -> None:
        # Reduce traffic: avoid publishing identical values every time
        # (use rounded float keys to avoid tiny jitter)
        stable_items = tuple(sorted((k, round(v, 3)) for k, v in data.items()))
        payload_sig = (stable_items, time.time())

        # always publish if changed since last publish
        if self._last_payload is not None and self._last_payload[0] == stable_items:
            return
        self._last_payload = payload_sig

        try:
            if self.publish_json:
                msg = json.dumps(
                    {
                        "ts": time.time(),
                        "values": {k: float(v) for k, v in data.items()},
                    },
                    separators=(",", ":"),
                )
                self.client.publish(f"{self.topic_base}/json", msg, qos=0, retain=self.retain)

            if self.publish_per_field:
                for k, v in data.items():
                    # publish raw numeric
                    self.client.publish(f"{self.topic_base}/{k}", f"{v:.3f}", qos=0, retain=self.retain)

        except Exception as e:
            with self.state.lock:
                self.state.mqtt_ok = False
                self.state.mqtt_last_err = f"{type(e).__name__}: {e}"


def curses_ui(state: SharedState, stop_evt: threading.Event, title: str) -> int:
    if curses is None:
        print("curses not available on this system. Use --ui plain or install curses.")
        return 2

    def _draw(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        while not stop_evt.is_set():
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                stop_evt.set()
                break

            h, w = stdscr.getmaxyx()
            stdscr.erase()

            # Header
            header = f"{title} (q = quit)"
            stdscr.addstr(0, 0, clamp_str(header, w - 1), curses.A_REVERSE)

            # Snapshot state
            with state.lock:
                data = dict(state.latest)
                last_ts = state.last_update_ts
                frames_ok = state.frames_ok
                frames_bad = state.frames_bad
                bytes_in = state.bytes_in
                mqtt_ok = state.mqtt_ok
                mqtt_err = state.mqtt_last_err
                serial_err = state.serial_last_err
                mode = state.mode

            now = time.time()
            age = now - last_ts if last_ts > 0 else 1e9
            stale = age > 2.0

            # Main display block
            y = 2
            proc = data.get("process_temperature")
            act = data.get("actual_temperature")

            def fmt_temp(v: Optional[float]) -> str:
                return "N/A" if v is None else f"{v:.1f} °C"

            def fmt_frac(v: Optional[float]) -> str:
                return "N/A" if v is None else f"{v:.3f}"

            stdscr.addstr(y, 0, clamp_str("Process temperature:", w - 1), curses.A_BOLD)
            y += 1
            left = fmt_temp(proc)
            right = fmt_temp(act)
            line = f"{left:<20}    {right:>20}"
            attr = curses.A_DIM if stale else curses.A_NORMAL
            stdscr.addstr(y, 0, clamp_str(line, w - 1), attr)
            y += 2

            # Table
            stdscr.addstr(y, 0, clamp_str("Parameter", max(0, w - 1)), curses.A_BOLD)
            y += 1

            rows = [
                ("Actual temp [°C]", data.get("actual_temperature"), "temp"),
                ("Process temp [°C]", data.get("process_temperature"), "temp"),
                ("Head temp [°C]", data.get("head_temperature"), "temp"),
                ("Box temp [°C]", data.get("box_temperature"), "temp"),
                ("Emissivity [-]", data.get("emissivity"), "frac"),
                ("Transmission [-]", data.get("transmission"), "frac"),
            ]

            for name, val, typ in rows:
                if y >= h - 6:
                    break
                sval = fmt_temp(val) if typ == "temp" else fmt_frac(val)
                line = f"{name:<22} {sval:>12}"
                stdscr.addstr(y, 0, clamp_str(line, w - 1))
                y += 1

            # Footer / diagnostics
            y = h - 5
            if y < 0:
                y = 0

            status = f"Mode: {mode} | frames ok/bad: {frames_ok}/{frames_bad} | bytes: {bytes_in} | age: {age:.1f}s"
            stdscr.addstr(y, 0, clamp_str(status, w - 1), curses.A_DIM)
            y += 1

            mline = f"MQTT: {'OK' if mqtt_ok else 'DOWN'}"
            if mqtt_err:
                mline += f" | {mqtt_err}"
            stdscr.addstr(y, 0, clamp_str(mline, w - 1), curses.A_DIM)
            y += 1

            sline = "Serial: OK" if not serial_err else f"Serial: {serial_err}"
            stdscr.addstr(y, 0, clamp_str(sline, w - 1), curses.A_DIM)

            stdscr.refresh()

    return curses.wrapper(_draw)


def plain_ui_loop(state: SharedState, stop_evt: threading.Event, interval_s: float = 1.0) -> int:
    try:
        while not stop_evt.is_set():
            with state.lock:
                data = dict(state.latest)
                age = (time.time() - state.last_update_ts) if state.last_update_ts else None
                mode = state.mode
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mode={mode} age={age:.1f}s data={data}" if age is not None else f"mode={mode} data={data}")
            time.sleep(interval_s)
        return 0
    except KeyboardInterrupt:
        stop_evt.set()
        return 0


def parse_burst_list(s: str) -> List[int]:
    # Accept "1,4,2,3,5,6" or "142356" (comma optional)
    s = s.strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
    else:
        parts = list(s)
    out: List[int] = []
    for p in parts:
        if p == "":
            continue
        v = int(p, 0)
        if not (0 <= v <= 15):
            raise ValueError("burst half-bytes must be 0..15")
        out.append(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Optris CT reader with MQTT and curses UI.")
    ap.add_argument("--serial", required=True, help="Serial port (e.g. /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    ap.add_argument("--mode", choices=["burst", "poll"], default="burst", help="Read mode")
    ap.add_argument("--burst", default="1,4,2,3,5,6", help="Burst half-byte list (default: 1,4,2,3,5,6)")
    ap.add_argument("--set-burst", action="store_true", help="Send burst string to device before starting burst mode")

    ap.add_argument("--mqtt-broker", default="mqtt.yourbroker.net")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-username", default=None)
    ap.add_argument("--mqtt-password", default=None)
    ap.add_argument("--mqtt-topic", default="/optris/ct01", help="Base topic (default: /optris/ct01)")
    ap.add_argument("--mqtt-client-id", default="optris-ct01")
    ap.add_argument("--publish-interval", type=float, default=0.2, help="MQTT publish interval (s)")
    ap.add_argument("--publish-json", action="store_true", help="Publish JSON snapshot to <topic>/json")
    ap.add_argument("--publish-per-field", action="store_true", help="Publish each field to <topic>/<key>")
    ap.add_argument("--retain", action="store_true", help="MQTT retain flag")

    ap.add_argument("--poll-interval", type=float, default=0.2, help="Polling interval (s) for --mode poll")
    ap.add_argument("--ui", choices=["curses", "plain", "none"], default="curses", help="UI mode")
    ap.add_argument("--multidrop", type=int, default=None, help="RS485 multidrop address (1..79)")

    args = ap.parse_args()

    burst_halfbytes = parse_burst_list(args.burst)

    stop_evt = threading.Event()
    state = SharedState(mode=args.mode)

    def _sig_handler(signum, frame):
        stop_evt.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    # Serial open
    try:
        ser = serial.Serial(
            args.serial,
            args.baud,
            timeout=0.2,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
    except Exception as e:
        print(f"Failed to open serial {args.serial}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    device = OptrisCT(ser, state, multidrop_addr=args.multidrop)

    # Configure/Start mode
    reader: Optional[threading.Thread] = None
    try:
        if args.mode == "burst":
            if args.set_burst:
                device.set_burst_string(burst_halfbytes)
                time.sleep(0.05)
            device.start_burst()
            reader = BurstReader(ser, state, burst_halfbytes, stop_evt)
            reader.start()
        else:
            reader = PollingReader(device, state, stop_evt, interval_s=args.poll_interval)
            reader.start()
    except Exception as e:
        with state.lock:
            state.serial_last_err = f"{type(e).__name__}: {e}"

    # MQTT thread (only if any publish option is enabled)
    mqtt_thread: Optional[MqttPublisher] = None
    if args.publish_json or args.publish_per_field:
        mqtt_thread = MqttPublisher(
            state=state,
            stop_evt=stop_evt,
            broker=args.mqtt_broker,
            port=args.mqtt_port,
            username=args.mqtt_username,
            password=args.mqtt_password,
            topic_base=args.mqtt_topic,
            publish_interval_s=args.publish_interval,
            publish_json=args.publish_json,
            publish_per_field=args.publish_per_field,
            retain=args.retain,
            client_id=args.mqtt_client_id,
        )
        mqtt_thread.start()

    # UI
    title = "Optris CT | Infrared temperature sensor readout"
    rc = 0
    try:
        if args.ui == "curses":
            rc = curses_ui(state, stop_evt, title=title)
        elif args.ui == "plain":
            rc = plain_ui_loop(state, stop_evt, interval_s=1.0)
        else:
            # no UI, just run until signal
            while not stop_evt.is_set():
                time.sleep(0.2)
            rc = 0
    finally:
        # Clean stop: stop burst mode if running
        try:
            if args.mode == "burst":
                device.stop_burst()
        except Exception:
            pass
        try:
            stop_evt.set()
        except Exception:
            pass
        try:
            if mqtt_thread:
                mqtt_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if reader:
                reader.join(timeout=1.0)
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
