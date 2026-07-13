# ESP32 Health Monitor (BLE)

A wireless physiological monitoring system for marmoset and human subjects. An
ESP32-S3 + MAX30102 streams **Heart Rate, SpO₂, HRV, Skin Temperature, Signal
Quality**, and the **raw PPG waveform** over Bluetooth LE — with real-time
forwarding to [Lab Streaming Layer (LSL)](https://github.com/sccn/labstreaminglayer)
(MonkeyLogic / LabRecorder), **live PC-side beat detection**, and an **offline
NeuroKit2 HRV pipeline**.

---

## Hardware

| Component | Part |
|-----------|------|
| Microcontroller | ESP32-S3 Dev Module |
| Pulse oximeter | MAX30102 (I²C) |
| Charger | TP4056 (single-cell LiPo) |
| Connection | USB-C (charging + flashing) |

Wiring: MAX30102 SDA → GPIO 8, SCL → GPIO 9 (or as defined in firmware).

---

## Repository layout

```
ESP32_HealthMonitor/ESP32_HealthMonitor.ino   Firmware (Arduino sketch)
health_monitor_gui.py                         Windows BLE GUI + live PC-side HR/HRV
analyze_recording.py                          Offline NeuroKit2 HRV analysis
test_lsl_stream.py                            LSL smoke test
health_monitor_receiver.m                     MATLAB LSL reader
HARDWARE_v2.md, BUDGET_PLAN.md, DEVLOG.md      Design docs / dev log
```

---

## Firmware (`ESP32_HealthMonitor/ESP32_HealthMonitor.ino`)

### Dependencies (Arduino IDE)
- **ESP32 board package** by Espressif (≥ 3.x) — add via Boards Manager
- **SparkFun MAX3010x Pulse and Proximity Sensor Library** — via Library Manager
  ⚠️ If multiple MAX30105 libraries are installed, keep only the SparkFun one.

### Board settings (Arduino IDE 2.x)
| Setting | Value |
|---------|-------|
| Board | ESP32S3 Dev Module |
| Upload Speed | 460800 |
| USB CDC On Boot | Disabled |
| Port | whichever COM port appears |

### Flashing
**Easiest — Arduino IDE:** open the sketch, pick the board + port, click **Upload**.
The ESP32-S3 uses native USB, so auto-reset often doesn't trigger — if it stalls at
`Connecting…`, do the bootloader dance: **hold BOOT → tap RESET → release BOOT**,
then **re-check Tools → Port** (the COM number can change in bootloader mode) and
Upload again. Use a **USB-C _data_ cable** (many are charge-only).

**Command line (esptool):**
```bash
esptool --chip esp32s3 --port <COM_PORT> --baud 460800 \
  --before no-reset write-flash 0x0 \
  build/esp32.esp32.esp32s3/ESP32_HealthMonitor.ino.merged.bin
```

### BLE service — two characteristics
- **DATA** (NOTIFY, ASCII, ~2.5 Hz) — status string, e.g.:
  ```
  SAMPLING=1,HR=72,SPO2=98,AVG=70,VALID=1,PI=2.4,TEMP=33.5,HRV=38,
  SPECIES=human,CHARGE=battery,FIFO=2,OVF=0,CFG=800/215/1,PROF=4000/300/12000
  ```
- **RAW** (NOTIFY, binary) — the raw PPG waveform in compact little-endian packets
  (3-byte / 18-bit IR & Red samples + real per-sample timestamps + beat markers).
  Lets a host watch the waveform live or re-derive HR/SpO₂/HRV itself.

### Diagnostics (appended to the DATA packet)
| Field | Meaning |
|-------|---------|
| `CFG=sr/pw/avg` | sensor's **actual** sample rate / pulse width / averaging (register readback) |
| `FIFO=n` | peak MAX30102 FIFO occupancy (of 32) |
| `OVF=n` | cumulative FIFO overflow count (dropped samples) |
| `PROF=r/n/s` | loop profile in µs: per-sample **r**ead / per BLE **n**otify / **S**pO₂ call |

### Sample-rate notes
The effective PPG rate is capped by the **MAX30102's own sample production** — not
BLE, not firmware (confirmed with the diagnostics above: `OVF`=0, tiny notify time,
0% packet loss). The practical sweet spot is **`sampleRate=800, pulseWidth=215µs,
2-LED` → ~250 Hz**. Higher configs counterintuitively produce _slower_. The SparkFun
on-board `checkForBeat()` only tracks reliably near ~20 Hz, so **use the PC-side
detector for HRV** at higher rates. For finer inter-beat timing, the offline analyzer
adds sub-sample peak interpolation.

---

## Python GUI (`health_monitor_gui.py`)

### Install
```bash
pip install bleak pylsl matplotlib numpy scipy
```
Only `bleak` is required; `pylsl` (LSL), `matplotlib` (live plot), and `numpy`/`scipy`
(PC-side HR/HRV) are optional and degrade gracefully if missing.

### Run
```bash
python health_monitor_gui.py
```

### Features
- **PC-side beat detection** — species-aware (bandpass + peak find) HR & HRV computed
  on the host from the raw PPG, independent of the firmware; works at any sample rate.
  This is the **primary HR readout**; the board's own HR is shown as a small diagnostic.
- **Live raw-PPG waveform plot** with beat markers
- Live SpO₂ / PI / Temp / HRV; species toggle (Human / Marmoset)
- **Recording** → status CSV **+** `_raw.csv` (per-sample PPG, with packet `seq`) **+**
  `_beats.csv`; fully instrumented (`cfg`/`fifo`/`ovf`/`prof` columns)
- **Auto-resume** sampling after a BLE reconnect (survives brownout reboots)
- **LSL outlets**: status + raw PPG + beat markers

### LSL channel map (status stream)
| Ch | Label | Unit | Notes |
|----|-------|------|-------|
| 1 | HR | bpm | −1 = no finger |
| 2 | SpO2 | % | −1 = invalid |
| 3 | HR_avg | bpm | Rolling average |
| 4 | Valid | bool | 1 = good signal |
| 5 | PI | % | Perfusion index |
| 6 | Temp | °C | Skin temperature |
| 7 | HRV | ms | RMSSD |
| 8 | Charge | code | 0=battery 1=charging 2=full |

---

## Offline HRV analysis (`analyze_recording.py`)

Rigorous, publication-grade HRV from a recorded `_raw.csv`, using **NeuroKit2**.

```bash
pip install neurokit2 scipy numpy
python analyze_recording.py path/to/hr_spo2_..._raw.csv [--species human|marmoset]
```

Pipeline: split into device-clock sessions (handles reboots) → gate out
finger-off / low-perfusion segments → species-aware beat detection with **Kubios
artifact correction** and **sub-sample peak interpolation** → full time / frequency /
nonlinear HRV, a per-segment breakdown, and a plot of the longest clean segment.
Species is auto-detected from the companion status CSV.

---

## MonkeyLogic integration

1. Start `health_monitor_gui.py` → click **Start Sampling**
2. In MonkeyLogic **LSL Setup**: set Stream #1 Name = `HealthMonitor`
3. LSL data is auto-recorded in `AnalogData.LSL.LSL1` in the `.bhv2` file

### Read LSL data in MATLAB after recording
```matlab
data = mlread('your_file.bhv2');
all_hr = []; all_spo2 = [];
for i = 1:numel(data)
    lsl = data(i).AnalogData.LSL.LSL1;
    if isempty(lsl), continue; end
    valid = lsl(:,5) == 1;
    all_hr   = [all_hr;   lsl(valid, 2)];   % HR
    all_spo2 = [all_spo2; lsl(valid, 3)];   % SpO2
end
```

---

## Test LSL stream
```bash
python test_lsl_stream.py
```

## Running two boards simultaneously
Give each board a unique name via `DEVICE_NAME` in the firmware and the matching
`DEVICE_NAME` in the GUI; reflash each board. (Default: `HealthMonitor1`.)

---

## Battery life (measured)

On battery at the ~250 Hz config (100 mAh LiPo):
- **~84–92 min total runtime**, reproducible across runs.
- Near end of charge the device **brownout-cycles** — it reboots every ~10–25 min as
  the LiPo sags under load — so only the first ~30 min is unbroken. The GUI's
  auto-resume keeps recording across the reboots.
- Charge _state_ (charging / full / battery) is reported; there is **no battery-%
  gauge** on the v1 board (planned via the BQ25120A PMIC — see `HARDWARE_v2.md`).

| Battery | Rough runtime |
|---------|---------------|
| 100 mAh | ~1.5 hr |
| 200 mAh | ~3 hr |
| 500 mAh | ~5+ hr |

*(~70–90 mA total draw: ESP32-S3 BLE active + MAX30102 LEDs.)*

---

## Authors
- Desimone Lab / Feng Lab, MIT
