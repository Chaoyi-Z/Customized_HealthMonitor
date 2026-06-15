# ESP32 Health Monitor (BLE)

A wireless physiological monitoring system for marmoset and human subjects. Streams **Heart Rate**, **SpO₂**, **HRV**, **Skin Temperature**, and **Signal Quality** over Bluetooth LE, with real-time forwarding to [Lab Streaming Layer (LSL)](https://github.com/sccn/labstreaminglayer) for integration with MonkeyLogic, LabRecorder, and other neuroscience acquisition systems.

---

## Hardware

| Component | Part |
|-----------|------|
| Microcontroller | ESP32-S3 Dev Module |
| Pulse oximeter | MAX30102 (I²C) |
| Connection | USB-C (charging + flashing) |

Wiring: MAX30102 SDA → GPIO 8, SCL → GPIO 9 (or as defined in firmware).

---

## Firmware (`ESP32_HealthMonitor.ino`)

### Dependencies (Arduino IDE)
- **ESP32 board package** by Espressif (≥ 3.x) — add via Boards Manager
- **SparkFun MAX3010x Pulse and Proximity Sensor Library** — install via Library Manager  
  ⚠️ If you have multiple MAX30105 libraries installed, uninstall all except the SparkFun one.

### Board settings (Arduino IDE 2.x)
| Setting | Value |
|---------|-------|
| Board | ESP32S3 Dev Module |
| Upload Speed | 460800 |
| USB CDC On Boot | Disabled |
| Port | whichever COM port appears |

### Flashing (first time or after BOOT+RESET)
The ESP32-S3 uses native USB — auto-reset does not work. Use this procedure:

1. Hold **BOOT** button → press and release **RESET** → release **BOOT**
2. A new COM port appears (bootloader mode)
3. Flash with esptool:

```bash
esptool --chip esp32s3 --port <COM_PORT> --baud 460800 \
  --before no-reset write-flash 0x0 \
  build/esp32.esp32.esp32s3/ESP32_HealthMonitor.ino.merged.bin
```

### BLE packet format (v6 firmware)
```
SAMPLING=1,HR=83,SPO2=98,AVG=82,VALID=1,PI=2.4,TEMP=33.5,HRV=38,SPECIES=human,CHARGE=charging
```

---

## Python GUI (`health_monitor_gui.py`)

### Install dependencies
```bash
pip install bleak pylsl
```

### Run
```bash
python health_monitor_gui.py
```

### Features
- Live HR / SpO₂ / PI / Temp / HRV display
- Species profile toggle (Human / Marmoset)
- CSV recording
- **LSL outlet** — streams 8 channels at 2.5 Hz automatically

### LSL channel map
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

## MonkeyLogic Integration

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

---

## Running two boards simultaneously

Give each board a unique name by changing `DEVICE_NAME` in the firmware and the matching `DEVICE_NAME` / `LSL_STREAM_NAME` in the GUI. Reflash each board separately.

---

## Battery life estimate

| Battery | Runtime |
|---------|---------|
| 100 mAh | ~1 hr |
| 200 mAh | ~2 hr |
| 500 mAh | ~5 hr |

*(Based on ~70 mA total draw: ESP32-S3 BLE active + MAX30102 LEDs)*

---

## Authors
- Desimone Lab / Feng Lab, MIT
