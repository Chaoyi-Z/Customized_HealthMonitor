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
import struct
import threading
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
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

# Optional: matplotlib for the live raw-PPG waveform plot. Imported gracefully
# like pylsl — if it isn't installed the GUI still runs, just without the plot.
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    PLOT_AVAILABLE = True
except ImportError:
    PLOT_AVAILABLE = False

# Optional: NumPy (+ SciPy) for PC-side beat detection on the raw PPG stream.
# NumPy does the heavy lifting; SciPy gives a cleaner filter/peak-finder if
# present, otherwise we fall back to a NumPy-only bandpass + peak detector.
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
try:
    from scipy.signal import butter, filtfilt, find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# --- Match the firmware -----------------------------------------------------
DEVICE_NAME    = "HealthMonitor1"
SERVICE_UUID   = "12345678-1234-1234-1234-123456789abc"
DATA_CHAR_UUID = "12345678-1234-1234-1234-123456789abd"
CMD_CHAR_UUID  = "12345678-1234-1234-1234-123456789abe"
RAW_CHAR_UUID  = "12345678-1234-1234-1234-123456789abf"

LINK_TIMEOUT_S = 5.0
UI_REFRESH_MS  = 100
SCAN_TIMEOUT_S = 8.0

# --- Raw waveform / live plot ----------------------------------------------
PPG_SAMPLE_RATE = 300.0     # nominal effective Hz (real rate measured per packet)
PLOT_WINDOW_S   = 10.0      # seconds of PPG history shown in the live plot
PLOT_MAXLEN     = int(PPG_SAMPLE_RATE * PLOT_WINDOW_S)  # samples kept for plot
BEAT_MAXLEN     = 80        # beat markers kept for plot
RAW_MAGIC       = 0xA5      # legacy binary RAW packet (4-byte samples)
RAW_MAGIC_V2    = 0xA6      # binary RAW packet v2 (3-byte / 24-bit samples)

# --- PC-side beat detection -------------------------------------------------
DET_WINDOW_S    = 12.0      # seconds of PPG used for beat detection
DET_MAXLEN      = 4096      # ~12 s even at ~300 Hz
DET_INTERVAL_MS = 1000      # how often to re-run detection

# Per-species detection params, mirroring the firmware PROFILES table so the PC
# detector and the board agree on what a valid beat looks like. Chosen live from
# the SPECIES field the firmware broadcasts in every status packet.
#   hr_min/hr_max  -> bandpass passband (bpm/60 = Hz) and the IBI plausibility gate
#   refractory_ms  -> minimum spacing between detected peaks
#   max_hr_change  -> bpm; rejects artifact beats whose instantaneous HR jumps
#                     too far from the median (the HRV cleanup)
SPECIES_PROFILES = {
    "human":    {"hr_min": 30,  "hr_max": 220, "refractory_ms": 300, "max_hr_change": 30},
    "marmoset": {"hr_min": 150, "hr_max": 400, "refractory_ms": 100, "max_hr_change": 60},
}
DEFAULT_SPECIES = "human"

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

# Raw PPG waveform — its own LSL stream (different rate than the status stream).
LSL_PPG_NAME     = "HealthMonitor_PPG"
LSL_PPG_TYPE     = "PPG"
LSL_PPG_SOURCE   = "esp32_healthmonitor_ppg_001"
LSL_PPG_CHANNELS = [("IR", "raw_adc"), ("Red", "raw_adc")]
# Beat events — irregular-rate marker stream (one sample per detected beat).
LSL_BEAT_NAME    = "HealthMonitor_Beats"
LSL_BEAT_TYPE    = "Markers"
LSL_BEAT_SOURCE  = "esp32_healthmonitor_beats_001"

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


def parse_raw_packet(data: bytes) -> dict | None:
    """Parse a binary RAW waveform packet (little-endian) into structured arrays.

    Header (both versions): magic(u8), nSamples(u8), nBeats(u8), seq(u32),
    t0(u32 ms), dt(u16 ms). Then per version:
      0xA6 (v2): IR[u24*n], Red[u24*n]  (3-byte samples; 18-bit ADC)
      0xA5 (v1): IR[u32*n], Red[u32*n]  (legacy 4-byte samples)
    Followed by beats[u32*nBeats]. Returns {seq,t0,dt,ir,red,beats} or None.
    """
    if len(data) < 13 or data[0] not in (RAW_MAGIC, RAW_MAGIC_V2):
        return None
    sample_bytes = 3 if data[0] == RAW_MAGIC_V2 else 4
    n  = data[1]
    nb = data[2]
    seq, t0, dt = struct.unpack_from("<IIH", data, 3)
    off = 13
    # Guard against a truncated notify: clamp to what actually arrived.
    avail = len(data) - off
    if 2 * n * sample_bytes + nb * 4 > avail:
        n = min(n, (avail // sample_bytes) // 2)
        nb = max(0, min(nb, (avail - 2 * n * sample_bytes) // 4))

    if sample_bytes == 3:
        def u24(o):
            return data[o] | (data[o + 1] << 8) | (data[o + 2] << 16)
        ir  = [u24(off + 3 * i) for i in range(n)]; off += 3 * n
        red = [u24(off + 3 * i) for i in range(n)]; off += 3 * n
    else:
        ir  = list(struct.unpack_from(f"<{n}I", data, off)); off += 4 * n
        red = list(struct.unpack_from(f"<{n}I", data, off)); off += 4 * n
    beats = list(struct.unpack_from(f"<{nb}I", data, off)) if nb else []
    return {
        "seq":   seq,
        "t0":    t0,
        "dt":    float(dt) if dt else 1000.0 / PPG_SAMPLE_RATE,
        "ir":    ir,
        "red":   red,
        "beats": beats,
    }


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

    def __init__(self, packet_queue: "queue.Queue", status_queue: "queue.Queue",
                 raw_queue: "queue.Queue"):
        self.packet_queue = packet_queue
        self.status_queue = status_queue
        self.raw_queue = raw_queue
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

                def raw_handler(_ch, data: bytearray):
                    parsed = parse_raw_packet(bytes(data))
                    if parsed:
                        self.raw_queue.put((datetime.now().timestamp(), parsed))

                async with BleakClient(device) as client:
                    self._client = client
                    self._publish_status(self.STATUS_CONNECTED,
                                         f"{device.name}  {device.address}")
                    await client.start_notify(DATA_CHAR_UUID, notify_handler)
                    # RAW characteristic is optional — older firmware won't have
                    # it, so subscribe best-effort and carry on if it's absent.
                    try:
                        await client.start_notify(RAW_CHAR_UUID, raw_handler)
                    except Exception:
                        pass

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
        self.root.geometry("640x760")
        self.root.configure(bg="#1e1e2e")

        # State
        self.packet_queue: "queue.Queue" = queue.Queue()
        self.status_queue: "queue.Queue" = queue.Queue()
        self.raw_queue: "queue.Queue" = queue.Queue()
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
        # User intent: True once the user starts sampling, until they stop. Used
        # to auto-resume after a BLE drop (the firmware halts sampling on any
        # disconnect and doesn't restart on its own).
        self.want_sampling = False
        self.firmware_ver: str | None = None   # "v5" or "v6" — detected from first packet

        # Raw-waveform state: rolling buffers for the live plot, CSV writers,
        # and a running last-beat time for inter-beat-interval derivation.
        self.raw_t  = deque(maxlen=PLOT_MAXLEN)   # device time (s) per sample
        self.raw_ir = deque(maxlen=PLOT_MAXLEN)
        self.raw_red = deque(maxlen=PLOT_MAXLEN)
        self.beat_t = deque(maxlen=BEAT_MAXLEN)   # device time (s) per beat
        self.raw_dirty = False                    # redraw plot only on new data
        self.last_raw_seq: int | None = None
        self.raw_csv_file = None
        self.raw_csv_writer = None
        self.beats_csv_file = None
        self.beats_csv_writer = None
        self._last_beat_ms: int | None = None     # for IBI (CSV + LSL)

        # PC-side beat detection: a longer rolling window over the raw PPG, plus
        # the latest computed HR/HRV and detected beat times (device-time s).
        self.det_t  = deque(maxlen=DET_MAXLEN)
        self.det_ir = deque(maxlen=DET_MAXLEN)
        self.pc_hr: float | None = None
        self.pc_hrv: float | None = None
        self.pc_beat_t: list = []
        self._pc_detect_ctr = 0

        # LSL outlets — set up once at startup; push every BLE packet.
        self.lsl_outlet = self._make_lsl_outlet() if LSL_AVAILABLE else None
        self.lsl_ppg_outlet = self._make_lsl_ppg_outlet() if LSL_AVAILABLE else None
        self.lsl_beat_outlet = self._make_lsl_beat_outlet() if LSL_AVAILABLE else None

        self._build_ui()

        self.ble = BLEManager(self.packet_queue, self.status_queue, self.raw_queue)
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

        # --- Scrollable container ------------------------------------------
        # The window can be shorter than the full UI (especially with the live
        # plot), so everything is built inside a Canvas-backed frame that
        # scrolls vertically. self.content is the parent for all UI sections.
        outer = tk.Frame(self.root, bg=bg)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vscroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.content = tk.Frame(canvas, bg=bg)
        content_id = canvas.create_window((0, 0), window=self.content, anchor="nw")

        # Keep the scroll region matched to the content height, and the content
        # width matched to the canvas so child frames still fill horizontally.
        self.content.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(content_id, width=e.width))
        # Mouse-wheel scrolling (Windows / macOS deliver <MouseWheel>).
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # Top bar
        top = tk.Frame(self.content, bg=bg)
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
        sensor_frame = tk.Frame(self.content, bg=bg)
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
        readout = tk.Frame(self.content, bg=bg)
        readout.pack(fill="both", expand=True, padx=16, pady=8)

        # Heart rate — the PC-side detector on the raw PPG is the PRIMARY reading
        # now (works at any sample rate). The board's on-board checkForBeat is
        # kept (it still runs untethered) but demoted to a small diagnostic line,
        # since it's unreliable above ~20 Hz.
        pc_frame = tk.Frame(readout, bg="#313244", bd=0)
        pc_frame.pack(fill="x", pady=6, ipady=8)
        pc_title = "Heart Rate (bpm)" if NUMPY_AVAILABLE else \
                   "Heart Rate — needs: pip install numpy scipy"
        tk.Label(pc_frame, text=pc_title,
                 bg="#313244", fg=muted, font=label_font
                 ).pack(anchor="w", padx=14, pady=(8, 0))
        self.pc_hr_value = tk.Label(pc_frame, text="--",
                                    bg="#313244", fg=accent, font=big_font)
        self.pc_hr_value.pack(anchor="w", padx=14)
        self.pc_hrv_value = tk.Label(pc_frame, text="HRV —",
                                     bg="#313244", fg=muted, font=small_font)
        self.pc_hrv_value.pack(anchor="w", padx=14)
        # Board's on-board HR — small diagnostic line (unreliable above ~20 Hz).
        self.hr_value = tk.Label(pc_frame, text="board: -- (diagnostic)",
                                 bg="#313244", fg=muted, font=small_font)
        self.hr_value.pack(anchor="w", padx=14, pady=(0, 6))

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

        # Live raw-PPG waveform (matplotlib, optional). Detrended IR with beat
        # markers — the fastest way to eyeball signal quality / finger contact.
        self._build_plot(readout, muted)

        # Firmware version badge — auto-detected from first BLE packet
        self.fw_badge = tk.Label(self.content, text="",
                                 bg=bg, fg="#6c7086", font=small_font, anchor="w")
        self.fw_badge.pack(fill="x", padx=16, pady=(0, 2))

        # Species toggle row
        species_frame = tk.Frame(self.content, bg=bg)
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
        mode_frame = tk.Frame(self.content, bg=bg)
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

        self.rec_status = tk.Label(self.content,
                                   text="Live View — viewing only, no data saved",
                                   bg=bg, fg=muted, font=small_font, anchor="w")
        self.rec_status.pack(fill="x", padx=16, pady=(0, 4))

        # Footer
        self.footer = tk.Label(self.content,
                               text="Scanning for BLE device 'HealthMonitor'…",
                               bg=bg, fg=muted, font=small_font, anchor="w")
        self.footer.pack(fill="x", padx=16, pady=(0, 12))

    # ---------- Live PPG plot ----------
    def _build_plot(self, parent, muted):
        """Embed a matplotlib waveform canvas, or a placeholder if unavailable."""
        plot_frame = tk.Frame(parent, bg="#313244")
        plot_frame.pack(fill="x", pady=6)
        tk.Label(plot_frame, text="Raw PPG waveform (IR, detrended)",
                 bg="#313244", fg=muted,
                 font=tkfont.Font(family="Segoe UI", size=9)
                 ).pack(anchor="w", padx=12, pady=(6, 0))

        # Default to "no plot" so the rest of the GUI never depends on it.
        self.ppg_ax = None
        self.ppg_canvas = None
        self.ppg_line = None
        self._beat_artists = []

        if not PLOT_AVAILABLE:
            tk.Label(plot_frame,
                     text="Live plot unavailable — install matplotlib:\n"
                          "    pip install matplotlib",
                     bg="#313244", fg="#f9e2af", justify="left",
                     font=tkfont.Font(family="Segoe UI", size=9)
                     ).pack(anchor="w", padx=12, pady=(4, 10))
            return

        fig = Figure(figsize=(5.6, 2.2), dpi=100)
        fig.patch.set_facecolor("#313244")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#1e1e2e")
        for spine in ax.spines.values():
            spine.set_color("#45475a")
        ax.tick_params(colors="#6c7086", labelsize=7)
        ax.set_xlabel("seconds ago", color="#6c7086", fontsize=8)
        ax.set_xlim(-PLOT_WINDOW_S, 0)
        (self.ppg_line,) = ax.plot([], [], color="#89b4fa", lw=1.2)
        fig.tight_layout(pad=1.0)

        self.ppg_fig = fig
        self.ppg_ax = ax
        self.ppg_canvas = FigureCanvasTkAgg(fig, master=plot_frame)
        self.ppg_canvas.get_tk_widget().pack(fill="x", padx=10, pady=(2, 8))

    def _update_plot(self):
        """Redraw the rolling PPG trace + beat markers from the raw buffers."""
        if self.ppg_ax is None or not self.raw_t:
            return
        t = list(self.raw_t)
        ir = list(self.raw_ir)
        latest = t[-1]
        xs = [ti - latest for ti in t]          # seconds ago (≤ 0)
        mean_ir = sum(ir) / len(ir)
        ys = [v - mean_ir for v in ir]          # detrend: strip the DC offset
        self.ppg_line.set_data(xs, ys)

        lo, hi = min(ys), max(ys)
        if hi - lo < 1.0:                        # flat/no-finger signal
            lo, hi = -1.0, 1.0
        pad = (hi - lo) * 0.15
        self.ppg_ax.set_ylim(lo - pad, hi + pad)

        for art in self._beat_artists:
            art.remove()
        self._beat_artists = []
        # Board's checkForBeat beats (faint red) vs PC-side detected beats (green).
        for bt in self.beat_t:
            x = bt - latest
            if -PLOT_WINDOW_S <= x <= 0:
                self._beat_artists.append(
                    self.ppg_ax.axvline(x, color="#f38ba8", lw=0.8, alpha=0.5))
        for bt in self.pc_beat_t:
            x = bt - latest
            if -PLOT_WINDOW_S <= x <= 0:
                self._beat_artists.append(
                    self.ppg_ax.axvline(x, color="#a6e3a1", lw=1.0, alpha=0.9))

        self.ppg_canvas.draw_idle()

    # ---------- PC-side beat detection ----------
    def _detect_pc_beats(self):
        """Run a bandpass + peak detector over the rolling raw-PPG window to
        compute heart rate independently of the firmware. Species-aware: the
        passband, refractory period, IBI gate, and artifact threshold all come
        from the active SPECIES profile (mirroring the firmware). Updates
        pc_hr/pc_hrv and pc_beat_t. No-op if NumPy isn't available."""
        if not NUMPY_AVAILABLE or len(self.det_t) < 40:
            return
        # Active species profile (defaults to human until the board reports one).
        sp = self.last_data.get("SPECIES")
        sp = sp.lower() if isinstance(sp, str) else DEFAULT_SPECIES
        prof = SPECIES_PROFILES.get(sp, SPECIES_PROFILES[DEFAULT_SPECIES])
        lo_hz   = prof["hr_min"] / 60.0
        hi_hz   = prof["hr_max"] / 60.0
        refr_s  = prof["refractory_ms"] / 1000.0
        ibi_min = 60000.0 / prof["hr_max"]      # ms
        ibi_max = 60000.0 / prof["hr_min"]      # ms
        max_chg = prof["max_hr_change"]         # bpm

        t = np.asarray(self.det_t, dtype=float)
        ir = np.asarray(self.det_ir, dtype=float)
        # Use only the most recent DET_WINDOW_S seconds for responsive HR.
        mask = t >= (t[-1] - DET_WINDOW_S)
        t, ir = t[mask], ir[mask]
        if len(t) < 40:
            return
        span = t[-1] - t[0]
        if span < 3.0:
            return
        fs = (len(t) - 1) / span
        if fs < 5.0:
            return
        # Resample onto a uniform grid using the real device timestamps.
        grid = np.arange(t[0], t[-1], 1.0 / fs)
        sig = np.interp(grid, t, ir)
        try:
            if SCIPY_AVAILABLE:
                hi = min(hi_hz, fs / 2.0 - 0.2)     # cap below Nyquist
                if hi <= lo_hz:
                    return                          # sample rate too low for band
                b, a = butter(2, [lo_hz, hi], btype="band", fs=fs)
                filt = filtfilt(b, a, sig)
                peaks, _ = find_peaks(filt, distance=max(1, int(fs * refr_s)),
                                      prominence=0.4 * np.std(filt))
            else:
                filt = self._np_bandpass(sig, fs)
                peaks = self._np_peaks(filt, fs, refr_s)
        except Exception:
            return
        if len(peaks) < 2:
            self.pc_hr = None
            self.pc_beat_t = []
            return
        beat_t = grid[peaks]
        self.pc_beat_t = beat_t.tolist()
        ibis = np.diff(beat_t) * 1000.0                       # ms
        ibis = ibis[(ibis > ibi_min) & (ibis < ibi_max)]      # physiological gate
        if len(ibis) < 2:
            self.pc_hr = None
            return
        # Artifact cleanup: drop beats whose instantaneous HR sits more than the
        # profile's max rate-of-change away from the median — this removes the
        # doubled/halved intervals (missed/extra beats) that inflate RMSSD.
        inst_hr = 60000.0 / ibis
        med_hr = float(np.median(inst_hr))
        clean = ibis[np.abs(inst_hr - med_hr) <= max_chg]
        if len(clean) >= 2:
            self.pc_hr = float(60000.0 / np.median(clean))
            self.pc_hrv = float(np.sqrt(np.mean(np.diff(clean) ** 2)))
        else:
            self.pc_hr = 60000.0 / med_hr          # HR still solid from median
            self.pc_hrv = None                     # too few clean beats for HRV

    def _np_bandpass(self, sig, fs):
        """NumPy-only bandpass fallback: smooth (lowpass) then remove drift."""
        def mov(x, w):
            w = max(1, int(w))
            return np.convolve(x, np.ones(w) / w, mode="same")
        low = mov(sig, fs * 0.1)
        return low - mov(low, fs * 1.2)

    def _np_peaks(self, filt, fs, refr_s=0.33):
        """NumPy-only peak finder with a physiological refractory period."""
        refr = max(1, int(fs * refr_s))
        thr = 0.4 * float(np.std(filt))
        peaks, last = [], -refr
        for i in range(1, len(filt) - 1):
            if (filt[i] > filt[i - 1] and filt[i] >= filt[i + 1]
                    and filt[i] > thr and i - last >= refr):
                peaks.append(i)
                last = i
        return np.asarray(peaks, dtype=int)

    def _handle_raw(self, ts: float, raw: dict):
        """Route one RAW packet to the plot buffers, CSV files, and LSL outlets."""
        ir, red = raw["ir"], raw["red"]
        n = len(ir)
        if n == 0:
            return

        seq = raw["seq"]
        # A SEQ that goes backwards means the firmware restarted its raw stream
        # (new sampling session) — clear rolling state so stale data / a bogus
        # first IBI don't leak across sessions.
        if self.last_raw_seq is not None and seq < self.last_raw_seq:
            self._last_beat_ms = None
            self.raw_t.clear(); self.raw_ir.clear()
            self.raw_red.clear(); self.beat_t.clear()
            self.det_t.clear(); self.det_ir.clear()
            self.pc_beat_t = []
        self.last_raw_seq = seq

        dt_s = raw["dt"] / 1000.0
        t0_s = raw["t0"] / 1000.0
        sample_t = [t0_s + i * dt_s for i in range(n)]   # device-clock seconds
        last_dev = sample_t[-1]

        for st, vir, vred in zip(sample_t, ir, red):
            self.raw_t.append(st)
            self.raw_ir.append(vir)
            self.raw_red.append(vred)
            self.det_t.append(st)        # longer window for PC-side detection
            self.det_ir.append(vir)

        # Beat events: IBI is the device-clock gap from the previous beat;
        # `back` is how far before the last sample the beat occurred (for sync).
        beat_events = []
        for b_ms in raw["beats"]:
            ibi = None if self._last_beat_ms is None else (b_ms - self._last_beat_ms)
            self._last_beat_ms = b_ms
            b_s = b_ms / 1000.0
            self.beat_t.append(b_s)
            host_epoch = ts - (last_dev - b_s)
            beat_events.append((b_ms, ibi, host_epoch, last_dev - b_s))

        self.raw_dirty = True

        if self.recording:
            self._write_raw_rows(ts, raw, sample_t, beat_events)

        # LSL — timestamps computed in the local_clock domain, spaced by the
        # true device sample interval so PPG and beats stay mutually aligned.
        if LSL_AVAILABLE and (self.lsl_ppg_outlet or self.lsl_beat_outlet):
            now = local_clock()
            if self.lsl_ppg_outlet is not None:
                samples = [[float(ir[i]), float(red[i])] for i in range(n)]
                # Scalar timestamp = time of the most recent sample (≈ now); LSL
                # back-derives the earlier ones from the stream's nominal rate.
                try:
                    self.lsl_ppg_outlet.push_chunk(samples, now)
                except Exception:
                    pass
            if self.lsl_beat_outlet is not None:
                for _b_ms, ibi, _epoch, back in beat_events:
                    val = float(ibi) if ibi is not None else 0.0
                    try:
                        self.lsl_beat_outlet.push_sample([val], timestamp=now - back)
                    except Exception:
                        pass

    def _write_raw_rows(self, ts: float, raw: dict, sample_t: list, beat_events: list):
        n = len(raw["ir"])
        dt_s = raw["dt"] / 1000.0
        if self.raw_csv_writer:
            try:
                seq = raw["seq"]
                for i in range(n):
                    # Back-date each sample from arrival (last sample ≈ ts).
                    epoch_i = ts - (n - 1 - i) * dt_s
                    self.raw_csv_writer.writerow([
                        f"{epoch_i:.3f}",
                        int(round(sample_t[i] * 1000)),
                        raw["ir"][i],
                        raw["red"][i],
                        seq,
                    ])
                self.raw_csv_file.flush()
            except (OSError, ValueError):
                pass
        if self.beats_csv_writer:
            try:
                for b_ms, ibi, host_epoch, _back in beat_events:
                    self.beats_csv_writer.writerow([
                        f"{host_epoch:.3f}", b_ms,
                        "" if ibi is None else ibi,
                    ])
                self.beats_csv_file.flush()
            except (OSError, ValueError):
                pass

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
                # Auto-resume: a BLE drop halts sampling on the firmware, so if
                # the user had it running, restart it on (re)connect. START is
                # idempotent on the device, so a duplicate here is harmless.
                if self.want_sampling:
                    self.ble.send_command("START")
                    self.sensor_on = True
                    self._set_sensor_button(on=True)
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

        # Drain raw waveform packets (PPG samples + beats)
        while True:
            try:
                ts, raw = self.raw_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_raw(ts, raw)

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
                text=f"board: {hr} bpm (diagnostic)"
                if isinstance(hr, int) and hr > 0 else "board: -- (diagnostic)")
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

        # PC-side beat detection runs on its own slower cadence (~1 s).
        self._pc_detect_ctr += 1
        if self._pc_detect_ctr >= max(1, DET_INTERVAL_MS // UI_REFRESH_MS):
            self._pc_detect_ctr = 0
            self._detect_pc_beats()
            if self.pc_hr:
                self.pc_hr_value.config(text=f"{self.pc_hr:.0f}")
                self.pc_hrv_value.config(
                    text=f"HRV {self.pc_hrv:.0f} ms" if self.pc_hrv is not None
                    else "HRV —")
            else:
                self.pc_hr_value.config(text="--")
                self.pc_hrv_value.config(text="HRV —")

        # Redraw the live waveform only when new raw data has arrived.
        if self.raw_dirty:
            self.raw_dirty = False
            self._update_plot()

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

    def _make_lsl_ppg_outlet(self):
        """Raw PPG waveform outlet — regular rate, IR + Red channels."""
        try:
            info = StreamInfo(
                name=LSL_PPG_NAME,
                type=LSL_PPG_TYPE,
                channel_count=len(LSL_PPG_CHANNELS),
                nominal_srate=PPG_SAMPLE_RATE,
                channel_format="float32",
                source_id=LSL_PPG_SOURCE,
            )
            chans = info.desc().append_child("channels")
            for label, unit in LSL_PPG_CHANNELS:
                ch = chans.append_child("channel")
                ch.append_child_value("label", label)
                ch.append_child_value("unit", unit)
                ch.append_child_value("type", "PPG")
            info.desc().append_child_value("manufacturer", "ESP32-S3 HealthMonitor")
            return StreamOutlet(info)
        except Exception:
            return None

    def _make_lsl_beat_outlet(self):
        """Beat-event outlet — irregular rate (one sample per detected beat)."""
        try:
            info = StreamInfo(
                name=LSL_BEAT_NAME,
                type=LSL_BEAT_TYPE,
                channel_count=1,
                nominal_srate=0.0,          # irregular / event stream
                channel_format="float32",
                source_id=LSL_BEAT_SOURCE,
            )
            ch = info.desc().append_child("channels").append_child("channel")
            ch.append_child_value("label", "IBI")
            ch.append_child_value("unit", "ms")
            ch.append_child_value("type", "Marker")
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
        self.want_sampling = True
        self.ble.send_command("START")
        self.sensor_on = True
        self._set_sensor_button(on=True)

    def stop_sampling(self):
        self.want_sampling = False
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
             "species", "charge_state", "fifo_max", "cfg", "ovf", "prof"]
        )
        self.csv_file.flush()

        # Companion raw-data files alongside the status CSV:
        #   <name>_raw.csv   — one row per PPG sample (full 25 Hz)
        #   <name>_beats.csv — one row per detected beat (with derived IBI)
        raw_path   = self.csv_path.with_name(self.csv_path.stem + "_raw.csv")
        beats_path = self.csv_path.with_name(self.csv_path.stem + "_beats.csv")
        try:
            self.raw_csv_file = raw_path.open("w", newline="", encoding="utf-8")
            self.raw_csv_writer = csv.writer(self.raw_csv_file)
            # seq = firmware packet counter; gaps in it = dropped BLE packets.
            self.raw_csv_writer.writerow(["epoch_s", "device_t_ms", "ir", "red", "seq"])
            self.raw_csv_file.flush()
            self.beats_csv_file = beats_path.open("w", newline="", encoding="utf-8")
            self.beats_csv_writer = csv.writer(self.beats_csv_file)
            self.beats_csv_writer.writerow(["epoch_s", "device_t_ms", "ibi_ms"])
            self.beats_csv_file.flush()
        except OSError as e:
            # Non-fatal: keep the status recording even if raw files can't open.
            messagebox.showwarning("Raw file unavailable", str(e))
            self.raw_csv_file = self.raw_csv_writer = None
            self.beats_csv_file = self.beats_csv_writer = None

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
        for f in (self.raw_csv_file, self.beats_csv_file):
            if f:
                try:
                    f.flush()
                    f.close()
                except OSError:
                    pass
        self.raw_csv_file = self.raw_csv_writer = None
        self.beats_csv_file = self.beats_csv_writer = None
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
            parsed.get("FIFO", ""),
            parsed.get("CFG", ""),
            parsed.get("OVF", ""),
            parsed.get("PROF", ""),
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
