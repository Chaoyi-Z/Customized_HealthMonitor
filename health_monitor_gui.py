"""
ESP32 Health Monitor — Windows GUI (BLE edition)

Connects over Bluetooth Low Energy to a device advertising the name
"HealthMonitor". Subscribes to notifications on the custom DATA
characteristic and parses status packets like:
    HR=83,SPO2=98,AVG=85,VALID=1,CHARGE=charging[,WARMUP=42][,SENSOR=error]

Buttons:
  ⏻ Start / Stop Sampling   →  writes "START" / "STOP" to the CMD characteristic
  👁 Live View               →  show readings only
  ● Record / Stop Recording  →  also append to a CSV file (file dialog at start)

Run:
    pip install bleak
    python health_monitor_gui.py

Requires: Python 3.9+, Windows 10 1809+ (for stable Windows BLE stack).
"""

import asyncio
import csv
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError as e:
    raise SystemExit(
        "Missing dependency 'bleak'. Install with:\n"
        "    pip install bleak\n"
        f"({e})"
    )

# Optional: Lab Streaming Layer outlet so MonkeyLogic / LabRecorder / etc.
# can pull HR / SpO2 in real time with proper time-sync. If pylsl isn't
# installed, we just skip it silently — BLE still works for the GUI/CSV.
try:
    from pylsl import StreamInfo, StreamOutlet, local_clock
    LSL_AVAILABLE = True
except ImportError:
    LSL_AVAILABLE = False

# --- Match the firmware -----------------------------------------------------
DEVICE_NAME    = "HealthMonitor"
SERVICE_UUID   = "12345678-1234-1234-1234-123456789abc"
DATA_CHAR_UUID = "12345678-1234-1234-1234-123456789abd"
CMD_CHAR_UUID  = "12345678-1234-1234-1234-123456789abe"

LINK_TIMEOUT_S = 5.0
UI_REFRESH_MS  = 100
SCAN_TIMEOUT_S = 8.0

# --- LSL stream identifiers -------------------------------------------------
LSL_STREAM_NAME    = "HealthMonitor"
LSL_STREAM_TYPE    = "Physiological"
LSL_SOURCE_ID      = "esp32_healthmonitor_001"   # change if you run two units
LSL_NOMINAL_RATE   = 2.5                         # Hz, matches firmware
# Channel layout — 8 float32 channels (HR, SpO2, AVG, VALID, PI, TEMP, HRV, CHARGE)
LSL_CHANNELS = [
    ("HR",      "bpm"),     # -1 when invalid / no finger
    ("SpO2",    "%"),       # -1 when invalid
    ("HR_avg",  "bpm"),     # rolling average HR
    ("Valid",   "bool"),    # 0 or 1
    ("PI",      "%"),       # perfusion index, signal quality
    ("Temp",    "°C"),      # skin temperature from MAX30102
    ("HRV",     "ms"),      # RMSSD over recent IBIs
    ("Charge",  "code"),    # 0=battery, 1=charging, 2=full, -1=unknown
]
CHARGE_CODE = {"battery": 0, "charging": 1, "full": 2, "fault": -2}

CHARGE_DISPLAY = {
    "charging": ("🔌 Charging",    "#f9e2af"),
    "full":     ("✅ Full",         "#a6e3a1"),
    "battery":  ("🔋 On battery",  "#89b4fa"),
    "fault":    ("⚠ Charge fault", "#f38ba8"),
}


def parse_packet(text: str) -> dict:
    """Parse 'HR=83,SPO2=98,CHARGE=charging' into a dict.
    Ints stay ints; non-int fields stay strings."""
    out = {}
    for kv in text.strip().split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            key = k.strip().upper()
            val = v.strip()
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out


# ---------------------------------------------------------------------------
# BLE manager — runs an asyncio event loop in a background thread, surfaces
# notifications via a queue, and accepts command sends from the GUI thread.
# ---------------------------------------------------------------------------
class BLEManager:
    """Connect once, reconnect on drop, forward notifications to the GUI queue."""

    STATUS_SCANNING   = "scanning"
    STATUS_CONNECTING = "connecting"
    STATUS_CONNECTED  = "connected"
    STATUS_RECONNECT  = "reconnect"
    STATUS_ERROR      = "error"

    def __init__(self, packet_queue: "queue.Queue", status_queue: "queue.Queue"):
        self.packet_queue = packet_queue
        self.status_queue = status_queue
        self._stop_evt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BleakClient | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)  # nudge

    def send_command(self, cmd: str):
        """Thread-safe: schedule a CMD write on the asyncio loop."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._send(cmd), self._loop)

    async def _send(self, cmd: str):
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.write_gatt_char(
                    CMD_CHAR_UUID, cmd.encode("utf-8"), response=False
                )
            except BleakError:
                pass

    def _publish_status(self, status: str, detail: str = ""):
        self.status_queue.put((status, detail))

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self):
        while not self._stop_evt.is_set():
            try:
                self._publish_status(self.STATUS_SCANNING)
                device = await BleakScanner.find_device_by_name(
                    DEVICE_NAME, timeout=SCAN_TIMEOUT_S
                )
                if device is None:
                    self._publish_status(self.STATUS_SCANNING,
                                         f"'{DEVICE_NAME}' not found, retrying…")
                    await asyncio.sleep(1.5)
                    continue

                self._publish_status(self.STATUS_CONNECTING, device.address)

                def notify_handler(_ch, data: bytearray):
                    try:
                        text = bytes(data).decode("utf-8", errors="ignore")
                    except Exception:
                        return
                    parsed = parse_packet(text)
                    if parsed:
                        self.packet_queue.put((datetime.now().timestamp(), parsed))

                async with BleakClient(device) as client:
                    self._client = client
                    self._publish_status(self.STATUS_CONNECTED,
                                         f"{device.name}  {device.address}")
                    await client.start_notify(DATA_CHAR_UUID, notify_handler)

                    while not self._stop_evt.is_set() and client.is_connected:
                        await asyncio.sleep(0.5)

                self._client = None
                self._publish_status(self.STATUS_RECONNECT,
                                     "central disconnected, will retry")
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except BleakError as e:
                self._client = None
                self._publish_status(self.STATUS_ERROR, str(e))
                await asyncio.sleep(2.0)
            except Exception as e:
                self._client = None
                self._publish_status(self.STATUS_ERROR, repr(e))
                await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------
class HealthMonitorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ESP32 Health Monitor (BLE)")
        self.root.geometry("620x700")
        self.root.configure(bg="#1e1e2e")

        # State
        self.packet_queue: "queue.Queue" = queue.Queue()
        self.status_queue: "queue.Queue" = queue.Queue()
        self.last_packet_time: float = 0.0
        self.last_data: dict = {}
        self.ble_status: str = BLEManager.STATUS_SCANNING
        self.ble_detail: str = ""
        self.recording = False
        self.csv_file = None
        self.csv_writer = None
        self.csv_path: Path | None = None
        self.record_started_at: datetime | None = None
        self.records_written = 0
        self.sensor_on = False
        self.firmware_ver: str | None = None   # "v5" or "v6" — detected from first packet

        # LSL outlet — set up once at startup; pushes every BLE packet.
        self.lsl_outlet = self._make_lsl_outlet() if LSL_AVAILABLE else None

        self._build_ui()

        self.ble = BLEManager(self.packet_queue, self.status_queue)
        self.ble.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(UI_REFRESH_MS, self._refresh_ui)

    # ---------- UI ----------
    def _build_ui(self):
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        accent = "#89b4fa"
        muted = "#6c7086"

        big_font   = tkfont.Font(family="Segoe UI", size=44, weight="bold")
        label_font = tkfont.Font(family="Segoe UI", size=11)
        small_font = tkfont.Font(family="Segoe UI", size=9)

        # Top bar
        top = tk.Frame(self.root, bg=bg)
        top.pack(fill="x", padx=16, pady=(14, 6))

        self.link_canvas = tk.Canvas(top, width=14, height=14, bg=bg,
                                     highlightthickness=0)
        self.link_dot = self.link_canvas.create_oval(2, 2, 12, 12,
                                                    fill="#f38ba8", outline="")
        self.link_canvas.pack(side="left")
        self.link_label = tk.Label(top, text="Scanning…",
                                   bg=bg, fg=fg, font=label_font)
        self.link_label.pack(side="left", padx=(6, 16))

        self.ap_label = tk.Label(top, text=f"BLE: {DEVICE_NAME}",
                                 bg=bg, fg=muted, font=small_font)
        self.ap_label.pack(side="left")

        lsl_text = f"LSL: {LSL_STREAM_NAME}" if self.lsl_outlet is not None \
                   else "LSL: off"
        lsl_color = "#a6e3a1" if self.lsl_outlet is not None else "#6c7086"
        self.lsl_label = tk.Label(top, text=lsl_text,
                                  bg=bg, fg=lsl_color, font=small_font)
        self.lsl_label.pack(side="left", padx=(12, 0))

        self.bat_label = tk.Label(top, text="Power: —",
                                  bg=bg, fg=muted, font=small_font)
        self.bat_label.pack(side="right")

        # Sensor power row
        sensor_frame = tk.Frame(self.root, bg=bg)
        sensor_frame.pack(fill="x", padx=16, pady=(0, 6))

        self.sensor_btn = tk.Button(sensor_frame, text="⏻  Start Sampling",
                                    command=self.start_sampling,
                                    bg="#a6e3a1", fg="#11111b",
                                    activebackground="#94d391",
                                    font=label_font, bd=0, padx=18, pady=8,
                                    cursor="hand2",
                                    state="disabled")    # enabled when connected
        self.sensor_btn.pack(side="left")

        self.sensor_status = tk.Label(sensor_frame,
                                      text="Sensor OFF — waiting for BLE link",
                                      bg=bg, fg=muted, font=small_font)
        self.sensor_status.pack(side="left", padx=(12, 0))

        # Readouts
        readout = tk.Frame(self.root, bg=bg)
        readout.pack(fill="both", expand=True, padx=16, pady=8)

        hr_frame = tk.Frame(readout, bg="#313244", bd=0)
        hr_frame.pack(fill="x", pady=6, ipady=8)
        tk.Label(hr_frame, text="Heart Rate (bpm)",
                 bg="#313244", fg=muted, font=label_font
                 ).pack(anchor="w", padx=14, pady=(8, 0))
        self.hr_value = tk.Label(hr_frame, text="--",
                                 bg="#313244", fg=accent, font=big_font)
        self.hr_value.pack(anchor="w", padx=14)

        spo2_frame = tk.Frame(readout, bg="#313244", bd=0)
        spo2_frame.pack(fill="x", pady=6, ipady=8)
        tk.Label(spo2_frame, text="SpO₂ (%)",
                 bg="#313244", fg=muted, font=label_font
                 ).pack(anchor="w", padx=14, pady=(8, 0))
        self.spo2_value = tk.Label(spo2_frame, text="--",
                                   bg="#313244", fg="#a6e3a1", font=big_font)
        self.spo2_value.pack(anchor="w", padx=14)

        # Auxiliary metrics row (PI / Temp / HRV / Species)
        metrics_frame = tk.Frame(readout, bg=bg)
        metrics_frame.pack(fill="x", pady=(8, 4))

        def metric_box(parent, title):
            f = tk.Frame(parent, bg="#313244")
            f.pack(side="left", fill="x", expand=True, padx=4)
            tk.Label(f, text=title, bg="#313244", fg=muted,
                     font=small_font).pack(anchor="w", padx=8, pady=(6, 0))
            val = tk.Label(f, text="—", bg="#313244", fg=fg,
                           font=label_font)
            val.pack(anchor="w", padx=8, pady=(0, 6))
            return val

        self.pi_value      = metric_box(metrics_frame, "PI (signal quality)")
        self.temp_value    = metric_box(metrics_frame, "Skin temp")
        self.hrv_value     = metric_box(metrics_frame, "HRV (RMSSD)")
        self.species_value = metric_box(metrics_frame, "Species")

        # Firmware version badge — auto-detected from first BLE packet
        self.fw_badge = tk.Label(self.root, text="",
                                 bg=bg, fg="#6c7086", font=small_font, anchor="w")
        self.fw_badge.pack(fill="x", padx=16, pady=(0, 2))

        # Species toggle row
        species_frame = tk.Frame(self.root, bg=bg)
        species_frame.pack(fill="x", padx=16, pady=(2, 2))
        tk.Label(species_frame, text="Profile:",
                 bg=bg, fg=muted, font=small_font).pack(side="left")
        self.human_btn = tk.Button(species_frame, text="Human",
                                   command=lambda: self._set_species("HUMAN"),
                                   bg="#89b4fa", fg="#11111b",
                                   activebackground="#6fa3f7",
                                   font=small_font, bd=0, padx=10, pady=3,
                                   cursor="hand2", state="disabled")
        self.human_btn.pack(side="left", padx=(8, 4))
        self.marm_btn = tk.Button(species_frame, text="Marmoset",
                                  command=lambda: self._set_species("MARMOSET"),
                                  bg="#45475a", fg="#cdd6f4",
                                  activebackground="#585b70",
                                  font=small_font, bd=0, padx=10, pady=3,
                                  cursor="hand2", state="disabled")
        self.marm_btn.pack(side="left")

        # Mode buttons
        mode_frame = tk.Frame(self.root, bg=bg)
        mode_frame.pack(fill="x", padx=16, pady=(4, 4))

        self.live_btn = tk.Button(mode_frame, text="👁  Live View",
                                  command=self.switch_to_live_view,
                                  bg="#89b4fa", fg="#11111b",
                                  activebackground="#6fa3f7",
                                  font=label_font, bd=0, padx=18, pady=8,
                                  cursor="hand2")
        self.live_btn.pack(side="left")

        self.record_btn = tk.Button(mode_frame, text="●  Record",
                                    command=self.start_recording,
                                    bg="#45475a", fg="#cdd6f4",
                                    activebackground="#585b70",
                                    font=label_font, bd=0, padx=18, pady=8,
                                    cursor="hand2")
        self.record_btn.pack(side="left", padx=(8, 0))

        self.rec_status = tk.Label(self.root,
                                   text="Live View — viewing only, no data saved",
                                   bg=bg, fg=muted, font=small_font, anchor="w")
        self.rec_status.pack(fill="x", padx=16, pady=(0, 4))

        # Footer
        self.footer = tk.Label(self.root,
                               text="Scanning for BLE device 'HealthMonitor'…",
                               bg=bg, fg=muted, font=small_font, anchor="w")
        self.footer.pack(fill="x", padx=16, pady=(0, 12))

    # ---------- Periodic refresh ----------
    def _refresh_ui(self):
        # Drain status events
        while True:
            try:
                status, detail = self.status_queue.get_nowait()
            except queue.Empty:
                break
            self.ble_status = status
            self.ble_detail = detail
            if status == BLEManager.STATUS_CONNECTED:
                self.sensor_btn.config(state="normal")
            else:
                self.sensor_btn.config(state="disabled")

        # Drain packets
        while True:
            try:
                ts, parsed = self.packet_queue.get_nowait()
            except queue.Empty:
                break
            self.last_packet_time = ts
            self.last_data = parsed
            if self.firmware_ver is None:
                self._detect_firmware(parsed)
            if self.recording:
                self._write_csv_row(ts, parsed)
            # Always push to LSL when an outlet exists — independent of recording
            self._push_lsl_sample(parsed)

        # Link indicator
        now = datetime.now().timestamp()
        if self.ble_status == BLEManager.STATUS_CONNECTED and \
           self.last_packet_time and (now - self.last_packet_time) < LINK_TIMEOUT_S:
            self.link_canvas.itemconfig(self.link_dot, fill="#a6e3a1")
            age_ms = int((now - self.last_packet_time) * 1000)
            self.link_label.config(text=f"Live  ({age_ms} ms ago)")
        elif self.ble_status == BLEManager.STATUS_CONNECTED:
            self.link_canvas.itemconfig(self.link_dot, fill="#f9e2af")
            self.link_label.config(text="Connected, no data yet")
        elif self.ble_status == BLEManager.STATUS_CONNECTING:
            self.link_canvas.itemconfig(self.link_dot, fill="#f9e2af")
            self.link_label.config(text="Connecting…")
        elif self.ble_status == BLEManager.STATUS_ERROR:
            self.link_canvas.itemconfig(self.link_dot, fill="#f38ba8")
            self.link_label.config(text=f"BLE error")
        else:
            self.link_canvas.itemconfig(self.link_dot, fill="#f38ba8")
            self.link_label.config(text="Scanning…")

        # Readouts
        if self.last_data:
            hr   = self.last_data.get("HR", -1)
            spo2 = self.last_data.get("SPO2", -1)

            self.hr_value.config(
                text=str(hr) if isinstance(hr, int) and hr > 0 else "--")
            self.spo2_value.config(
                text=str(spo2) if isinstance(spo2, int) and spo2 > 0 else "--")

            # Charge state
            charge = self.last_data.get("CHARGE")
            if isinstance(charge, str) and charge in CHARGE_DISPLAY:
                text, color = CHARGE_DISPLAY[charge]
                self.bat_label.config(text=text, fg=color)
            else:
                self.bat_label.config(text="Power: —", fg="#6c7086")

            # Sensor state (drives the Start/Stop button styling)
            sensor_field = self.last_data.get("SENSOR")
            sampling_field = self.last_data.get("SAMPLING")
            warmup = self.last_data.get("WARMUP")
            if sensor_field == "error":
                self.sensor_status.config(
                    text="⚠ MAX30102 not responding (power-cycle the board)",
                    fg="#f38ba8")
                self.sensor_btn.config(state="disabled")
            elif sampling_field is not None:
                actual_on = (sampling_field == 1)
                if actual_on != self.sensor_on:
                    self.sensor_on = actual_on
                    self._set_sensor_button(on=actual_on)
                if warmup is not None:
                    self.sensor_status.config(
                        text=f"Sensor warming up… {warmup}%   (place finger)",
                        fg="#f9e2af")
                elif actual_on:
                    self.sensor_status.config(
                        text="Sensor ON — LEDs active, sampling 2.5 Hz",
                        fg="#a6e3a1")
                else:
                    self.sensor_status.config(
                        text="Sensor OFF — LEDs off, no data",
                        fg="#6c7086")

            # New metrics (v6 firmware only — v5 shows N/A set by _detect_firmware)
            if self.firmware_ver == "v6":
                pi   = self.last_data.get("PI")
                temp = self.last_data.get("TEMP")
                hrv  = self.last_data.get("HRV")
                sp   = self.last_data.get("SPECIES")
                self.pi_value.config(
                    text=(f"{float(pi):.1f} %" if isinstance(pi, (int, float, str))
                          and str(pi) not in ("", "None") else "—"),
                    fg=("#a6e3a1" if isinstance(pi, (int, float))
                        and float(pi) >= 0.5 else "#f9e2af")
                )
                self.temp_value.config(
                    text=(f"{float(temp):.1f} °C" if isinstance(temp, (int, float))
                          else "—"))
                self.hrv_value.config(
                    text=(f"{float(hrv):.0f} ms" if isinstance(hrv, (int, float))
                          else "—"))
                if isinstance(sp, str) and sp:
                    self.species_value.config(text=sp)
                    # Sync button highlight to actual species
                    if sp.lower() == "human":
                        self.human_btn.config(bg="#89b4fa", fg="#11111b")
                        self.marm_btn.config(bg="#45475a", fg="#cdd6f4")
                    elif sp.lower() == "marmoset":
                        self.human_btn.config(bg="#45475a", fg="#cdd6f4")
                        self.marm_btn.config(bg="#fab387", fg="#11111b")
                else:
                    self.species_value.config(text="—")

            preview = ",".join(f"{k}={v}" for k, v in self.last_data.items())
            self.footer.config(text=f"Last: {preview}")
        else:
            self.footer.config(
                text=f"Waiting for first BLE notification… ({self.ble_status})")

        # Recording status
        if self.recording and self.csv_path:
            elapsed = datetime.now() - self.record_started_at
            self.rec_status.config(
                text=f"Recording → {self.csv_path.name}   "
                     f"({self.records_written} rows, "
                     f"{str(elapsed).split('.')[0]} elapsed)",
                fg="#a6e3a1",
            )

        self.root.after(UI_REFRESH_MS, self._refresh_ui)

    # ---------- Species switch (BLE command) ----------
    def _set_species(self, sp: str):
        sp = sp.upper()
        if sp not in ("HUMAN", "MARMOSET"):
            return
        self.ble.send_command(f"SPECIES:{sp}")
        # Optimistically update the button styling; the next packet will confirm
        if sp == "HUMAN":
            self.human_btn.config(bg="#89b4fa", fg="#11111b")
            self.marm_btn.config(bg="#45475a", fg="#cdd6f4")
        else:
            self.human_btn.config(bg="#45475a", fg="#cdd6f4")
            self.marm_btn.config(bg="#fab387", fg="#11111b")

    # ---------- Firmware version detection ----------
    def _detect_firmware(self, parsed: dict):
        """Called once on the first valid BLE packet to lock in v5 vs v6."""
        if "PI" in parsed or "HRV" in parsed or "TEMP" in parsed or "SPECIES" in parsed:
            self.firmware_ver = "v6"
            self.fw_badge.config(
                text="✓ Firmware v6 — PI / Temp / HRV / Species active",
                fg="#a6e3a1",
            )
            self.human_btn.config(state="normal")
            self.marm_btn.config(state="normal")
        else:
            self.firmware_ver = "v5"
            self.fw_badge.config(
                text="⚠ Firmware v5 running — PI / Temp / HRV / Species require v6 upgrade",
                fg="#f9e2af",
            )
            # v5 doesn't understand SPECIES commands — keep buttons disabled
            self.human_btn.config(state="disabled")
            self.marm_btn.config(state="disabled")
            # Mark metric boxes as intentionally unavailable
            for lbl in (self.pi_value, self.temp_value,
                        self.hrv_value, self.species_value):
                lbl.config(text="N/A (v5 fw)", fg="#6c7086")

    # ---------- LSL ----------
    def _make_lsl_outlet(self):
        try:
            info = StreamInfo(
                name=LSL_STREAM_NAME,
                type=LSL_STREAM_TYPE,
                channel_count=len(LSL_CHANNELS),
                nominal_srate=LSL_NOMINAL_RATE,
                channel_format="float32",
                source_id=LSL_SOURCE_ID,
            )
            # Decorate the stream metadata with channel labels + units —
            # this is what shows up in LabRecorder / MonkeyLogic browsers.
            chans = info.desc().append_child("channels")
            for label, unit in LSL_CHANNELS:
                ch = chans.append_child("channel")
                ch.append_child_value("label", label)
                ch.append_child_value("unit", unit)
                ch.append_child_value("type", "Physiological")
            info.desc().append_child_value("manufacturer", "ESP32-S3 HealthMonitor")
            return StreamOutlet(info)
        except Exception:
            return None

    def _push_lsl_sample(self, parsed: dict):
        if self.lsl_outlet is None:
            return
        # Convert dict to channel-ordered float vector. Missing fields → 0 / -1.
        def fnum(key, default=0.0):
            v = parsed.get(key)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try: return float(v)
                except ValueError: return default
            return default

        sample = [
            fnum("HR",   -1.0),
            fnum("SPO2", -1.0),
            fnum("AVG",   0.0),
            fnum("VALID", 0.0),
            fnum("PI",    0.0),
            fnum("TEMP",  0.0),
            fnum("HRV",   0.0),
            float(CHARGE_CODE.get(parsed.get("CHARGE", ""), -1)),
        ]
        try:
            self.lsl_outlet.push_sample(sample, timestamp=local_clock())
        except Exception:
            pass

    # ---------- Sensor power (BLE commands) ----------
    def start_sampling(self):
        self.ble.send_command("START")
        self.sensor_on = True
        self._set_sensor_button(on=True)

    def stop_sampling(self):
        self.ble.send_command("STOP")
        self.sensor_on = False
        if self.recording:
            self.stop_recording()
        self._set_sensor_button(on=False)

    def _set_sensor_button(self, on: bool):
        if on:
            self.sensor_btn.config(text="⏻  Stop Sampling",
                                   bg="#f9e2af", fg="#11111b",
                                   activebackground="#e7d09f",
                                   command=self.stop_sampling)
        else:
            self.sensor_btn.config(text="⏻  Start Sampling",
                                   bg="#a6e3a1", fg="#11111b",
                                   activebackground="#94d391",
                                   command=self.start_sampling)

    # ---------- Mode switching ----------
    def switch_to_live_view(self):
        if self.recording:
            self.stop_recording()
        self._set_mode_buttons(live_active=True)
        if not self.recording:
            self.rec_status.config(
                text="Live View — viewing only, no data saved",
                fg="#6c7086")

    def _set_mode_buttons(self, live_active: bool):
        if live_active:
            self.live_btn.config(bg="#89b4fa", fg="#11111b",
                                 activebackground="#6fa3f7")
            self.record_btn.config(text="●  Record",
                                   bg="#45475a", fg="#cdd6f4",
                                   activebackground="#585b70",
                                   command=self.start_recording)
        else:
            self.live_btn.config(bg="#45475a", fg="#cdd6f4",
                                 activebackground="#585b70")
            self.record_btn.config(text="■  Stop Recording",
                                   bg="#f38ba8", fg="#11111b",
                                   activebackground="#e57a98",
                                   command=self.stop_recording)

    # ---------- Recording ----------
    def start_recording(self):
        if not self.sensor_on:
            self.start_sampling()
        default_name = f"hr_spo2_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            title="Save recording as",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self.csv_path = Path(path)
        try:
            self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Cannot open file", str(e))
            return
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            ["timestamp_iso", "epoch_s", "hr_bpm", "spo2_pct",
             "hr_avg_bpm", "valid", "pi_pct", "temp_c", "hrv_ms",
             "species", "charge_state"]
        )
        self.csv_file.flush()
        self.records_written = 0
        self.record_started_at = datetime.now()
        self.recording = True
        self._set_mode_buttons(live_active=False)

    def stop_recording(self):
        was_recording = self.recording
        self.recording = False
        if self.csv_file:
            try:
                self.csv_file.flush()
                self.csv_file.close()
            except OSError:
                pass
        path_name = self.csv_path.name if self.csv_path else ""
        rows = self.records_written
        self.csv_file = None
        self.csv_writer = None
        self._set_mode_buttons(live_active=True)
        if was_recording and rows > 0:
            self.rec_status.config(
                text=f"Saved {rows} rows → {path_name}   (back to Live View)",
                fg="#a6e3a1",
            )
        else:
            self.rec_status.config(
                text="Live View — viewing only, no data saved",
                fg="#6c7086",
            )

    def _write_csv_row(self, ts: float, parsed: dict):
        if not self.csv_writer:
            return
        iso = datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
        row = [
            iso,
            f"{ts:.3f}",
            parsed.get("HR", ""),
            parsed.get("SPO2", ""),
            parsed.get("AVG", ""),
            parsed.get("VALID", ""),
            parsed.get("PI", ""),
            parsed.get("TEMP", ""),
            parsed.get("HRV", ""),
            parsed.get("SPECIES", ""),
            parsed.get("CHARGE", ""),
        ]
        try:
            self.csv_writer.writerow(row)
            self.csv_file.flush()
            self.records_written += 1
        except (OSError, ValueError):
            pass

    # ---------- Shutdown ----------
    def on_close(self):
        if self.recording:
            self.stop_recording()
        if self.sensor_on:
            try:
                self.ble.send_command("STOP")
            except Exception:
                pass
        self.ble.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    HealthMonitorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
