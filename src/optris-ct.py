#
# Optris CT Infrared temperature sensor readout in burst mode
# Publish values to MQTT and display them locally
#
# <tec att sixtopia.net> Bjoern Heller
#

#!/usr/bin/env python3
import argparse
import json
import sys
import threading
import time
from typing import Dict, Any, Optional

import serial
from serial import Serial, SerialException
import paho.mqtt.client as mqtt

try:
    import curses
except ImportError:
    curses = None  # UI is optional


# ─────────────────────────────────────────────
# Burst configuration: [1,4,2,3,5,6]
# 1 = Target, 4 = Actual, 2 = Head, 3 = Box, 5 = Emissivity, 6 = Transmission
# ─────────────────────────────────────────────

BURST_ORDER = [
    "process_temperature",   # Target Temp
    "actual_temperature",    # Current target Temp
    "head_temperature",      # Head Temp
    "box_temperature",       # Box Temp
    "emissivity",            # Epsilon Emissivity
    "transmission",          # Transmission Value
]

NUM_VALUES = len(BURST_ORDER)
PAYLOAD_LEN = NUM_VALUES * 2  # 2 bytes per value


# ─────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────

def parse_temp(raw: bytes) -> float:
    """Temperature: (b1 * 256 + b2 - 1000) / 10 in °C."""
    if len(raw) != 2:
        raise ValueError("Need 2 bytes for temperature")
    b1, b2 = raw
    return (b1 * 256 + b2 - 1000) / 10.0


def parse_frac(raw: bytes) -> float:
    """Fraction: (b1 * 256 + b2) / 1000 (for emissivity / transmission)."""
    if len(raw) != 2:
        raise ValueError("Need 2 bytes for fraction")
    b1, b2 = raw
    return (b1 * 256 + b2) / 1000.0


def decode_payload(payload: bytes) -> Dict[str, float]:
    """Decode one burst payload into named values."""
    if len(payload) != PAYLOAD_LEN:
        raise ValueError(f"Payload length mismatch, expected {PAYLOAD_LEN}, got {len(payload)}")

    data: Dict[str, float] = {}
    offset = 0
    for name in BURST_ORDER:
        raw = payload[offset:offset + 2]
        offset += 2

        if name in ("process_temperature", "actual_temperature",
                    "head_temperature", "box_temperature"):
            data[name] = parse_temp(raw)
        else:
            data[name] = parse_frac(raw)

    return data


# ─────────────────────────────────────────────
# MQTT Manager (with reconnection)
# ─────────────────────────────────────────────

class MQTTManager:
    def __init__(self,
                 broker: str,
                 port: int,
                 username: str,
                 password: str,
                 client_id: str = "optris-ct-bm") -> None:
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password

        self.client = mqtt.Client(client_id=client_id)
        self.client.username_pw_set(self.username, self.password)

        self._loop_started = False
        self._connected_once = False
        self._last_attempt = 0.0
        self._backoff = 1.0  # sec, exponential backoff

        # Do an initial best-effort connection
        self._initial_connect()

    def _initial_connect(self) -> None:
        try:
            self.client.connect(self.broker, self.port)
            self._connected_once = True
            print(f"[MQTT] Connected to {self.broker}:{self.port}")
        except Exception as e:
            print(f"[MQTT] Initial connect to {self.broker}:{self.port} failed: {e}", file=sys.stderr)

        if not self._loop_started:
            self.client.loop_start()
            self._loop_started = True

    def ensure_connected(self) -> None:

        if self.client.is_connected():
            return

        now = time.monotonic()
        if now - self._last_attempt < self._backoff:
            return

        self._last_attempt = now
        try:
            if self._connected_once:
                # reconnect after a previous successful connect!
                self.client.reconnect()
            else:
                # first successful connect!
                self.client.connect(self.broker, self.port)
                self._connected_once = True

            print(f"[MQTT] Reconnected to {self.broker}:{self.port}")
            self._backoff = 1.0
        except Exception as e:
            print(f"[MQTT] Reconnect to {self.broker}:{self.port} failed: {e}", file=sys.stderr)
            # increase backoff up to a reasonable max (5 minutes)
            self._backoff = min(self._backoff * 2.0, 300.0)

    def publish_bundle(self, base_topic: str, data: Dict[str, Any]) -> None:

        base_topic = base_topic.rstrip("/")
        self.ensure_connected()
        if not self.client.is_connected():
            # silently drop if still disconnected..
            return

        for key, value in data.items():
            topic = f"{base_topic}/{key}"
            self.client.publish(topic, value, qos=0, retain=False)
        self.client.publish(base_topic, json.dumps(data), qos=0, retain=False)

    def stop(self) -> None:
        try:
            if self._loop_started:
                self.client.loop_stop()
        except Exception:
            pass


# ─────────────────────────────────────────────
# Serial Manager
# ─────────────────────────────────────────────

class SerialManager:
    def __init__(self,
                 port_name: str,
                 baudrate: int,
                 timeout: float = 0.2) -> None:
        self.port_name = port_name
        self.baudrate = baudrate
        self.timeout = timeout

        self.ser: Optional[Serial] = None
        self._last_attempt = 0.0
        self._backoff = 1.0  # sec

    def ensure_open(self) -> bool:

        if self.ser is not None and self.ser.is_open:
            return True

        now = time.monotonic()
        if now - self._last_attempt < self._backoff:
            return False

        self._last_attempt = now
        try:
            self.ser = Serial(
                self.port_name,
                self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.ser.reset_input_buffer()
            print(f"[Serial] Opened {self.port_name} @ {self.baudrate} Bd")
            self._backoff = 1.0
            return True
        except SerialException as e:
            print(f"[Serial] Failed to open {self.port_name}: {e}", file=sys.stderr)
            self.ser = None
            self._backoff = min(self._backoff * 2.0, 300.0)
            return False

    def read(self, size: int) -> bytes:

        if self.ser is None or not self.ser.is_open:
            raise SerialException("Serial port is not open")
        return self.ser.read(size)

    def close_on_error(self, err: Exception) -> None:

        print(f"[Serial] Error on {self.port_name}: {err}", file=sys.stderr)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        # increase backoff
        self._backoff = min(self._backoff * 2.0, 300.0)


# ─────────────────────────────────────────────
# Shared state for UI
# ─────────────────────────────────────────────

class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {}
        self.last_error: Optional[str] = None
        self.last_update_ns: Optional[int] = None
        self.running: bool = True


def format_float(val: Optional[float], unit: str = "") -> str:
    if val is None:
        return "N/A"
    s = f"{val:.1f}"
    if unit:
        s += f" {unit}"
    return s


# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────

def draw_UI(stdscr,
             state: SharedState,
             config: Dict[str, Any]) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    with state.lock:
        data = dict(state.data)
        last_error = state.last_error
        last_update_ns = state.last_update_ns

    # Color setup
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)   # Program title
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK) # Main values
        curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)  # Status is ok
        curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)    # Status is error

    # Title
    title = " Optris CT | Infrared temperature sensor readout (q = quit) "
    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(0, max(0, (max_x - len(title)) // 2), title)
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

    # Main temperature (process_temperature)
    main_temp = data.get("process_temperature")
    main_label = "Process temperature"
    main_val_str = format_float(main_temp, "°C")

    main_line = 2
    stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
    stdscr.addstr(main_line, 2, f"{main_label}:")
    val_x = max(2, (max_x // 2) - len(main_val_str) // 2)
    stdscr.addstr(main_line + 1, val_x, main_val_str)
    stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)

    # Table of values
    table_start = main_line + 3
    stdscr.attron(curses.A_BOLD)
    stdscr.addstr(table_start, 2, "Parameter")
    stdscr.addstr(table_start, 30, "Value")
    stdscr.attroff(curses.A_BOLD)
    row = table_start + 1

    param_defs = [
        ("actual_temperature", "Actual temp [°C]", "°C"),
        ("head_temperature",   "Head temp [°C]",   "°C"),
        ("box_temperature",    "Box temp [°C]",    "°C"),
        ("emissivity",         "Emissivity [-]",   ""),
        ("transmission",       "Transmission [-]", ""),
    ]

    for key, label, unit in param_defs:
        if row >= max_y - 5:
            break
        val = data.get(key)
        val_str = format_float(val, unit)
        stdscr.addstr(row, 2, f"{label:<24}")
        stdscr.addstr(row, 30, f"{val_str:<16}")
        row += 1

    # Config block
    cfg_start_col = max_x // 2
    cfg_row = table_start
    stdscr.attron(curses.A_BOLD)
    stdscr.addstr(cfg_row, cfg_start_col, "Configuration")
    stdscr.attroff(curses.A_BOLD)
    cfg_row += 1

    cfg_items = [
        ("Serial", f"{config['serial_port']} @ {config['baudrate']} Bd"),
        ("Mode",   "burst (passive stream)"),
        ("MQTT",   f"{config['mqtt_broker']}:{config['mqtt_port']}"),
        ("Topic",  config['mqtt_topic']),
    ]

    for label, val in cfg_items:
        if cfg_row >= max_y - 3:
            break
        stdscr.addstr(cfg_row, cfg_start_col, f"{label:<10}: {val}")
        cfg_row += 1

    # Status
    status_y = max_y - 2
    if last_error:
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        msg = f"Error: {last_error}"
    else:
        stdscr.attron(curses.color_pair(3))
        age_ms = "N/A"
        if last_update_ns is not None:
            age_ms = f"{(time.time_ns() - last_update_ns) / 1e3:.0f} µs"
        msg = f"Status: OK – last update {age_ms} ago"
    stdscr.addstr(status_y, 1, msg[:max_x - 2])
    stdscr.attroff(curses.color_pair(3) | curses.color_pair(4) | curses.A_BOLD)

    stdscr.noutrefresh()
    curses.doupdate()


def UI_loop(stdscr,
             state: SharedState,
             config: Dict[str, Any]) -> None:
    stdscr.nodelay(True)
    curses.curs_set(0)

    while state.running:
        draw_UI(stdscr, state, config)
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        if ch in (ord('q'), ord('Q')):
            state.running = False
            break
        time.sleep(0.05)


# ─────────────────────────────────────────────
# Burst reader loop
# ─────────────────────────────────────────────

def burst_reader_loop(serial_mgr: SerialManager,
                      mqtt_mgr: MQTTManager,
                      base_topic: str,
                      state: Optional[SharedState]) -> None:

    last_byte: Optional[int] = None
    base_topic = base_topic.rstrip("/")

    try:
        while True:
            if state is not None and not state.running:
                break

            # Ensure serial is open
            if not serial_mgr.ensure_open():
                if state is not None:
                    with state.lock:
                        state.last_error = f"Serial not available ({serial_mgr.port_name})"
                time.sleep(0.5)
                continue

            try:
                b = serial_mgr.read(1)
            except SerialException as e:
                serial_mgr.close_on_error(e)
                if state is not None:
                    with state.lock:
                        state.last_error = f"Serial error: {e}"
                time.sleep(0.5)
                last_byte = None
                continue

            if not b:
                # timeout hit
                continue

            val = b[0]

            # sync on AA AA
            if last_byte == 0xAA and val == 0xAA:
                try:
                    payload = serial_mgr.read(PAYLOAD_LEN)
                except SerialException as e:
                    serial_mgr.close_on_error(e)
                    if state is not None:
                        with state.lock:
                            state.last_error = f"Serial error: {e}"
                    time.sleep(0.5)
                    last_byte = None
                    continue

                if len(payload) != PAYLOAD_LEN:
                    last_byte = None
                    continue

                try:
                    data = decode_payload(payload)
                except Exception as e:
                    msg = f"Decode error: {e}"
                    print(f"[Burst] {msg}", file=sys.stderr)
                    if state is not None:
                        with state.lock:
                            state.last_error = msg
                    last_byte = None
                    continue

                # MQTT publish
                mqtt_mgr.publish_bundle(base_topic, data)

                # update state
                if state is not None:
                    # UI mode
                    with state.lock:
                        state.data = data
                        state.last_error = None
                        state.last_update_ns = time.time_ns()
                else:
                    # no UI
                    ts = time.strftime("%H:%M:%S")
                    print(
                        f"[{ts}] Tobj={data.get('process_temperature', float('nan')):.1f}°C, "
                        f"Tact={data.get('actual_temperature', float('nan')):.1f}°C, "
                        f"Thead={data.get('head_temperature', float('nan')):.1f}°C, "
                        f"Tbox={data.get('box_temperature', float('nan')):.1f}°C, "
                        f"eps={data.get('emissivity', float('nan')):.3f}, "
                        f"trans={data.get('transmission', float('nan')):.3f}"
                    )

                last_byte = None
            else:
                last_byte = val

    except KeyboardInterrupt:
        if state is not None:
            state.running = False
        print("\n[Burst] Stopped (KeyboardInterrupt).")


# ─────────────────────────────────────────────
# main()
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optris CT Infrared temperature sensor readout (burst mode → MQTT + optional UI)"
    )
    parser.add_argument("tty", type=str, help="e.g., /dev/ttyUSB0 or /dev/tty.usbserial-5")
    parser.add_argument("--baudrate", type=int, default=9600,
                        help="Serial baudrate (default: 9600)")
    parser.add_argument("--broker", type=str, default="mqtt.myserver.net",
                        help="MQTT broker hostname")
    parser.add_argument("--port", type=int, default=1883,
                        help="MQTT broker port (default: 1883)")
    parser.add_argument("--username", type=str, required=True,
                        help="MQTT username")
    parser.add_argument("--password", type=str, required=True,
                        help="MQTT password")
    parser.add_argument("--topic", type=str, default="/optris/ct01",
                        help="Base MQTT topic (default: /optris/ct01)")
    parser.add_argument("--client-id", type=str, default="optris-ct-burst",
                        help="MQTT client ID")
    parser.add_argument("--UI", action="store_true",
                        help="Enable curses UI in the terminal")
    args = parser.parse_args()

    if args.UI and curses is None:
        print("UI requested, but curses is not available in this environment.", file=sys.stderr)
        sys.exit(1)

    # Managers
    serial_mgr = SerialManager(args.tty, args.baudrate, timeout=0.2)
    mqtt_mgr = MQTTManager(args.broker, args.port,
                           args.username, args.password,
                           client_id=args.client_id)

    config = {
        "serial_port": args.tty,
        "baudrate": args.baudrate,
        "mqtt_broker": args.broker,
        "mqtt_port": args.port,
        "mqtt_topic": args.topic.rstrip("/"),
    }

    base_topic = args.topic.rstrip("/")

    try:
        if args.UI and curses is not None:
            state = SharedState()
            worker = threading.Thread(
                target=burst_reader_loop,
                args=(serial_mgr, mqtt_mgr, base_topic, state),
                daemon=True,
            )
            worker.start()
            curses.wrapper(UI_loop, state, config)
            state.running = False
            worker.join(timeout=1.0)
        else:
            # no UI, just run burst reader in foreground
            burst_reader_loop(serial_mgr, mqtt_mgr, base_topic, state=None)

    finally:
        mqtt_mgr.stop()


if __name__ == "__main__":
    main()
