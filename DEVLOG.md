# ESP32-S3 + MAX30102 Health Monitor — Development Log

End-to-end build log of a wireless fingertip/tail pulse-oximeter intended for
MATLAB / MonkeyLogic behavioral-recording setups (incl. marmoset task work).

---

## Hardware

- **MCU**: ESP32-S3-MINI-1 (rev v0.2), 8 MB embedded flash
  - Old PCB MAC: `90:70:69:2a:d8:2c`
  - New PCB MAC: `90:70:69:2a:d8:00`
- **Sensor**: MAX30102 (pulse oximetry, IR + Red LEDs)
- **Charging**: TP4056 + USB-C, 1 A charge current (R9 = 1.2 kΩ)
- **Battery**: 3.7 V LiPo via JST-PH connector
- **Power path**: USB → TP4056 → BAT+ → SW3 → SWITCH rail → regulators → ESP32
  (single SWITCH rail powers everything; SW3 must be ON for either USB or battery to power the chip)
- **Buttons**: SW1 = BOOT (right, IO0), SW2 = ENABLE / RESET (left, CHIP_PU)
- **TP4056 status pins** (open-drain, active LOW):
  - IO7 = CHRG# (LOW while charging)
  - IO6 = STDBY# (LOW when charge complete)
- **Indicator LED**: IO5 + 220 Ω
- **Sensor I²C**: SDA = IO9, SCL = IO8

### Pin map

| ESP32 GPIO | Schematic label | Function |
|-----------|-----------------|----------|
| GPIO0  | IO0 / BOOT  | BOOT strap, SW1 |
| GPIO4  | IO4 / INT   | MAX30102 interrupt |
| GPIO5  | IO5 / LED   | Indicator LED |
| GPIO6  | IO6 / FULL  | TP4056 STDBY# |
| GPIO7  | IO7 / CHARGE | TP4056 CHRG# |
| GPIO8  | IO8 / SCL   | I²C clock to MAX30102 |
| GPIO9  | IO9 / SDA   | I²C data to MAX30102 |

---

## Software evolution

Five firmware iterations, all in `ESP32_HealthMonitor.ino`. Latest source on disk is **v6 (BLE + motion rejection)**; latest *flashed* is **v5**.

| Ver | Transport | Notes |
|----|-----------|-------|
| v1 | WiFi STA (MIT) | Blocked by MIT subnet routing between WiFi 10.29.x.x and Ethernet 10.93.x.x |
| v2 | WiFi AP "HealthMonitor" + UDP broadcast 192.168.4.255:12345 @ 1 Hz | First working wireless path |
| v3 | WiFi AP + UDP @ 2.5 Hz, `beatAvg` HR, on-demand sampling via UDP `START`/`STOP` | Sensor LEDs only on while requested |
| v4 | v3 + `CHARGE=` field from TP4056 status pins | Top-bar shows Charging / Full / On battery |
| v5 | **BLE GATT** (custom service), same data fields | ~10× lower power than WiFi AP |
| v6 (on disk, not flashed) | v5 + motion-resilient HR + Human/Marmoset profile + PI + HRV + skin temp | Blocked: SW1 BOOT button defective on current PCB |

---

## Current architecture (v5 firmware, current GUI)

```
ESP32-S3 ──BLE GATT──► Windows laptop ──LSL outlet──► MonkeyLogic LSL inlet
  • MAX30102                    ▲                       (auto-discovers stream)
  • TP4056 status pins          │
  • 2.5 Hz notify               │ Python tkinter GUI
                                │   • Sensor on/off button
  (writes "START"/"STOP" ──BLE──┤   • Live View / Record mode
                                │   • CSV logger
                                └── • LSL streamer (pylsl)
```

### BLE GATT service

- **Service UUID**: `12345678-1234-1234-1234-123456789abc`
- **DATA characteristic** (NOTIFY): `12345678-1234-1234-1234-123456789abd`
  - ASCII status string sent at 2.5 Hz
- **CMD characteristic** (WRITE): `12345678-1234-1234-1234-123456789abe`
  - Accepts `"START"`, `"STOP"`, and (v6 only) `"SPECIES:HUMAN"` / `"SPECIES:MARMOSET"`

### Status packet format

v5 (currently running):
```
SAMPLING=1,HR=83,SPO2=98,AVG=82,VALID=1,CHARGE=charging
SAMPLING=1,HR=-1,SPO2=-1,AVG=0,VALID=0,WARMUP=42,CHARGE=battery
SAMPLING=0,HR=-1,SPO2=-1,AVG=0,VALID=0,CHARGE=full
SENSOR=error,CHARGE=battery        # I²C bus to MAX30102 failed
```

v6 (in source, awaiting flash):
```
SAMPLING=1,HR=83,SPO2=98,AVG=82,VALID=1,PI=2.4,TEMP=33.5,HRV=38,
  SPECIES=human,CHARGE=charging
```

---

## Key technical findings

### 1. CDC on Boot kills the WiFi AP
On the WiFi-era firmware, building with `-DARDUINO_USB_CDC_ON_BOOT=1` (FQBN variant `esp32s3:CDCOnBoot=cdc`) made the `HealthMonitor` softAP invisible to laptops. Verified twice. The default FQBN (`esp32:esp32:esp32s3`) keeps the AP visible but loses the soft-reset upload path → manual BOOT button required for every flash.

Made irrelevant by the BLE switch in v5 — no WiFi at all anymore.

### 2. HR algorithm produced harmonic doubling
The MAX30105 library's `maxim_heart_rate_and_oxygen_saturation()` periodically reports 2× the real HR (e.g., 166 instead of 83). Even `checkForBeat()` does this — it detects the secondary dicrotic-notch peak as a separate beat. **Solution (v6)**: enforce a refractory period after each detected beat (300 ms human / 100 ms marmoset). Single drop-in fix.

### 3. TP4056 can never report "Full" on this design
The load (ESP32 ~120 mA on WiFi, ~15 mA on BLE) sits on the **BAT+ rail**, not behind a separate OUT pin. The TP4056's charge-complete logic waits for current to drop below ~100 mA → never fires while WiFi is on (load > threshold). With BLE the idle load is below threshold so `STDBY#` should fire and we should finally see `CHARGE=full`.

### 4. BLE is ~10× lower power than WiFi AP
Measured-out runtime: this device on WiFi died in ~1 minute on a half-charged cell. On BLE with sensor on, same cell ran a 20-minute (3105-row) recording successfully. BLE idle draw is ~10-20 mA vs ~120 mA for WiFi AP.

### 5. MAX30102 I²C state machine can wedge on brown-out
After repeated battery brown-outs, the MAX30102 may stop responding to `sensor.begin()`. **Fix in firmware**: don't lock up in the failure path — set `sensorOk = false`, keep BLE alive, send `SENSOR=error` so the GUI can show the failure instead of going dark. **Fix in hardware**: full power cycle (disconnect both USB and battery JST for 60 s) to drain the sensor's local caps.

### 6. Battery state is impossible to monitor without a hardware mod
The schematic has no ADC divider from BAT+ to a free GPIO. The only "battery info" we can read is the TP4056's two open-drain status pins (charging / full / battery). True voltage / percentage requires soldering a 2× 100 kΩ divider from BAT+ to a free ADC pin (e.g., GPIO1).

### 7. SW1 (BOOT button) on the current PCB is unreliable
After v5 was flashed on the current PCB, SW1 stopped reliably pulling IO0 LOW. Multiple BOOT+RESET sequences with 30+ esptool retries all failed to enter ROM bootloader mode. Likely a cold solder joint or worn-out switch contact. Workarounds documented below.

---

## Build & flash workflow

### Compile (no CDC on Boot)

```powershell
$ardc = "$env:TEMP\arduino-cli-bin\arduino-cli.exe"
& $ardc compile `
  --fqbn "esp32:esp32:esp32s3" `
  --output-dir "C:\Users\Owner\AppData\Local\Temp\esp32build_hm_v6" `
  "...\Documents\Arduino\ESP32_HealthMonitor"
```

`arduino-cli.exe` needs to be downloaded per-session; cached to `$env:TEMP\arduino-cli-bin`. Quick install:
```powershell
Invoke-WebRequest https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip -OutFile $env:TEMP\arduino-cli.zip
Expand-Archive $env:TEMP\arduino-cli.zip $env:TEMP\arduino-cli-bin -Force
```

### Flash (BOOT-button entry to bootloader, when SW1 works)

1. Hold BOOT (SW1, right) — keep holding firmly with a pen tip
2. Briefly tap RESET (SW2, left)
3. Keep holding BOOT for 5-10 s
4. Run esptool:
   ```powershell
   $esptool = "C:\Users\Owner\AppData\Local\Arduino15\packages\esp32\tools\esptool_py\5.2.0\esptool.exe"
   & $esptool --chip esp32s3 --port COM<N> --baud 921600 `
     --before no-reset --after hard-reset `
     write-flash -z 0x0 "<path>\ESP32_HealthMonitor.ino.merged.bin"
   ```
5. After flashing, **manually press RESET** to start the new firmware (the RTS-pin "hard reset" in esptool is a no-op on this board — no auto-reset circuit).

### Flash fallback when SW1 is unreliable

#### A) Paperclip / tweezer jumper across SW1 pads
The fastest workaround with no soldering. SW1 is a 4-pin SMD tactile switch. Two of the four pins are connected to GND, the other two are connected to IO0 via a 10 kΩ pull-up (R1) and a 100 nF debounce cap (C1). Pressing the button shorts IO0 to GND.

To simulate the press without a working switch:
1. Locate SW1 on the PCB (the right-side button)
2. With a fine metal tweezers tip or paperclip end, **bridge two diagonal pads** of SW1 — touch one pad and an adjacent diagonal pad simultaneously
3. While the jumper is held, briefly press SW2 (RESET / ENABLE) with another finger
4. Release SW2 — keep the SW1 jumper held for 2 more seconds
5. Remove the jumper
6. The chip is now in ROM bootloader. Immediately run esptool:
   ```powershell
   $esptool = "C:\Users\Owner\AppData\Local\Arduino15\packages\esp32\tools\esptool_py\5.2.0\esptool.exe"
   & $esptool --chip esp32s3 --port COM<N> --baud 921600 `
     --before no-reset --after hard-reset `
     write-flash -z 0x0 "C:\Users\Owner\AppData\Local\Temp\esp32build_hm_v6\ESP32_HealthMonitor.ino.merged.bin"
   ```
7. After flashing, briefly tap SW2 (RESET) to start the new firmware.

Pro tip: tape a piece of foil between two SW1 pads if you want a hands-free "stuck on" while you tap RESET with the same hand.

#### B) Resolder or replace SW1
Permanent fix. ~15 min with a fine-tip soldering iron.

1. Heat each pad of the existing SW1 in turn while gently lifting with tweezers to remove
2. Clean residual solder with desoldering braid
3. Apply small dot of flux to each pad
4. Place a new 4-pin SMD tactile switch (e.g., C&K PTS645 series, ~6×6 mm) in the same orientation
5. Solder each leg
6. Test continuity: multimeter between IO0 pad and GND should show open circuit normally, shorted when switch pressed

#### C) OpenOCD via JTAG (no buttons at all)
ESP32-S3 has a built-in USB-Serial/JTAG interface (the same `303A:1001` device that appears as a COM port). OpenOCD can flash via the JTAG side of that interface without using SW1 at all.

**Windows-specific setup** (one-time, ~30 min):
1. Download Zadig from https://zadig.akeo.ie/
2. With the board plugged in, open Zadig → Options → "List All Devices"
3. Find the entry labeled `USB JTAG/serial debug unit (Interface 2)` or similar — **NOT** the COM-port interface (Interface 0), only the JTAG one
4. From the driver dropdown, pick **WinUSB** (or **libusbK**)
5. Click "Replace Driver" — accept warnings
6. The COM port should still work (Interface 0 keeps its serial driver)
7. OpenOCD is already installed at `C:\Users\Owner\AppData\Local\Arduino15\packages\esp32\tools\openocd-esp32\v0.12.0-esp32-20251215\bin\openocd.exe`

**Flash command** (after Zadig setup):
```powershell
$openocd = "C:\Users\Owner\AppData\Local\Arduino15\packages\esp32\tools\openocd-esp32\v0.12.0-esp32-20251215\bin\openocd.exe"
$scripts = "C:\Users\Owner\AppData\Local\Arduino15\packages\esp32\tools\openocd-esp32\v0.12.0-esp32-20251215\share\openocd\scripts"
$bin = "C:\Users\Owner\AppData\Local\Temp\esp32build_hm_v6\ESP32_HealthMonitor.ino.merged.bin"

& $openocd -s $scripts -f "board/esp32s3-builtin.cfg" `
  -c "program_esp `"$bin`" 0x0 verify reset exit"
```

If you see `libusb_open() failed with LIBUSB_ERROR_NOT_FOUND` it means Zadig didn't install WinUSB on the right interface — redo step 3 (it must be the *JTAG* interface, not the COM-port one).

---

## Python GUI

Single tkinter window, ~600 lines. Background thread holds a `bleak` asyncio loop for BLE; main thread does UI + CSV + LSL.

### Features

- **BLE link**: scans for `HealthMonitor`, connects, subscribes to DATA notifications, reconnects on drop
- **Top bar**: connection status, BLE name, LSL stream name, power indicator (🔌/✅/🔋)
- **Sensor power button**: sends `START`/`STOP` over CMD characteristic
- **Big HR / SpO₂ readouts**
- **Auxiliary metric boxes** (PI, Temp, HRV, Species — populated when v6 firmware is flashed)
- **Profile toggle**: Human / Marmoset (sends `SPECIES:` command, v6 only)
- **Live View vs Record** mode buttons; Record opens save dialog and writes CSV
- **LSL outlet**: 8-channel float32 at 2.5 Hz, name `HealthMonitor`, type `Physiological`. Auto-broadcasts when GUI launches.
- **Clean shutdown**: on window close, sends STOP, closes BLE, flushes CSV

### Dependencies (miniconda base env)

```powershell
& "C:\Users\Owner\miniconda3\python.exe" -m pip install bleak pylsl
```

### CSV columns

```
timestamp_iso, epoch_s, hr_bpm, spo2_pct, hr_avg_bpm, valid,
pi_pct, temp_c, hrv_ms, species, charge_state
```

(v5 firmware leaves `pi_pct`, `temp_c`, `hrv_ms`, `species` empty; v6 fills them.)

### LSL channels

| # | Label | Unit | Notes |
|---|-------|------|-------|
| 1 | HR     | bpm  | -1 when invalid/no finger |
| 2 | SpO2   | %    | -1 when invalid |
| 3 | HR_avg | bpm  | rolling 8-beat average |
| 4 | Valid  | bool | gated by PI + finger detection |
| 5 | PI     | %    | perfusion index (v6 only) |
| 6 | Temp   | °C   | skin temp from MAX30102 (v6 only) |
| 7 | HRV    | ms   | RMSSD over last 8 beats (v6 only) |
| 8 | Charge | code | 0=battery, 1=charging, 2=full, -2=fault, -1=unknown |

---

## MonkeyLogic integration (LSL)

1. Launch GUI → outlet starts automatically. Green `LSL: HealthMonitor` indicator in the top bar confirms it's broadcasting.
2. In MonkeyLogic: `Other device settings → Lab Streaming Layer` opens the Setup dialog.
3. In any Stream slot, the dropdown should auto-discover `HealthMonitor`. If not, type `HealthMonitor` in the Name field.
4. Buffer length: 360 s default is fine for typical sessions.
5. Done.

After that, MonkeyLogic shows `360 s, 1 stream(s) selected` and records the HR / SpO₂ samples into the `.bhv2` file alongside its other channels, time-synced via LSL's local-clock.

---

## Motion artifacts and marmoset use

PPG sensors fundamentally struggle with motion: any change in optical path length between LED and photodetector looks like a pulse. For awake marmoset tail recording:

### What v5 already does
- `beatAvg` instead of FFT-based HR (less prone to algorithm doubling)
- `VALID=0` when no finger / IR below threshold
- On-demand sampling so the sensor is off when not needed

### What v6 adds (compiled, not flashed)
- **Refractory period** in beat detection → eliminates dicrotic-notch doubling
- **Species profiles**:
  - HUMAN: 30-220 bpm, 300 ms refractory, ±30 bpm/s outlier limit
  - MARMOSET: 150-400 bpm, 100 ms refractory, ±60 bpm/s outlier limit
- **Perfusion Index** (PI) — `(max−min)/mean` of IR signal over 4 s buffer
- **Outlier-rejecting median filter** on HR (5-sample)
- **HRV (RMSSD)** computed from inter-beat intervals
- **Skin temperature** from MAX30102 internal sensor
- `VALID=1` requires PI above species-specific threshold (gates noisy data automatically)

### Possible hardware additions for marmoset work
- **Accelerometer** (LIS3DH or MPU-6050) on the spare I²C lines — enables motion-gated VALID flag and motion-correlated subtraction
- **Battery voltage divider** (2 × 100 kΩ to GPIO1) for real charge %
- **Snug mounting clip** matters more than firmware — secure optical coupling cuts motion artifact by >50% on its own

---

## Battery diagnosis notes

The original PCB's cell shipped at near-zero voltage, likely deeply discharged in storage. Symptoms:
- Charged for 4+ hours on laptop USB → still died in <1 minute when unplugged
- Even 8 h overnight charge stored only ~2-3 mAh of usable capacity

The new PCB's cell is healthier — ran a 20-min BLE recording on battery alone.

### Testing a battery (multimeter)

```
Test 1 (resting, USB unplugged, wait 30 s):
  4.10 – 4.20 V  → fully charged
  3.80 – 4.10 V  → ~50-90 %
  3.30 – 3.80 V  → mostly empty
  < 3.0 V        → over-discharged, likely damaged
  ~ 0 V          → bad JST or dead cell

Test 2 (under load, USB unplugged, press RESET):
  Stays within ~0.1 V of resting  → healthy
  Sags by 0.3-0.5 V               → high internal resistance, degraded
  Crashes < 3 V                   → can't sustain load, replace

Test 3 (charging, USB plugged):
  Rising over time, 3.5-4.2 V     → healthy charge cycle
  Stuck at one value 30+ min      → cell not accepting charge
```

### Battery selection
For replacements: **JST-PH 2-pin, 3.7 V**, 300-500 mAh, dimensions ~25 × 35 mm (matches the existing footprint). Adafruit, SparkFun, or Amazon LiPo cells are fine.

### Estimated runtime (v5 BLE + healthy cell)
- Idle / Live View only: 10-20+ h
- Active sampling (LEDs on): 5-10 h
- Continuous recording: same 5-10 h

---

## File layout

```
Documents/Arduino/ESP32_HealthMonitor/
├── ESP32_HealthMonitor.ino      # firmware source (currently v6 on disk; v5 flashed)
├── health_monitor_gui.py        # Windows BLE + LSL + CSV GUI
├── health_monitor_receiver.m    # legacy MATLAB UDP receiver (for reference)
└── DEVLOG.md                    # this file
```

Compiled binaries (cached, only present after a successful build):
```
C:\Users\Owner\AppData\Local\Temp\esp32build_hm_ble\ESP32_HealthMonitor.ino.merged.bin   # v5
C:\Users\Owner\AppData\Local\Temp\esp32build_hm_v6\ESP32_HealthMonitor.ino.merged.bin    # v6 (awaiting flash)
```

---

## Open items / next steps

1. **Fix or bypass SW1** to enable v6 flash (resolder, jumper IO0→GND, or OpenOCD/JTAG with Zadig).
2. **Replace the original PCB's degraded LiPo** if returning to that hardware.
3. **Test v6 marmoset profile** on a calm animal — confirm HR range tracks correctly and PI gating reduces artifact.
4. **Optional hardware mods**: battery voltage divider; accelerometer on spare I²C.
5. **Document MonkeyLogic experiment script** that subscribes to the LSL stream and references HR/SpO₂ channels in trial logic.

---

## MRI safety

**This device is NOT MR-safe.** Do not bring it inside an MRI scan room.
- USB-C connector has ferromagnetic steel shell → projectile risk in high field
- LiPo cell foils heat under gradient pulses → burn risk
- 2.4 GHz BLE radio → severe RF interference with MRI imaging
- Multiple ICs have nickel-plated lead frames

For in-scanner pulse-ox, use a dedicated **fiber-optic MR-compatible** unit (Nonin 7500FO, Invivo Expression, etc.). The LSL framework from this project can subscribe to that device's stream and ours simultaneously for pre/in/post-scan continuity.

---

## Acknowledgements

ESP32 Arduino core 3.3.8 · SparkFun MAX30105 library 1.1.2 · NimBLE / Arduino BLE · `bleak` 3.0.2 · `pylsl` 1.18.2 · esptool 5.2.0 · arduino-cli 1.5.0
