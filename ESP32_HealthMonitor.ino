/*
 * ESP32-S3 + MAX30102 Health Monitor — BLE Edition (v6)
 *
 * Additions over v5:
 *   • Beat-detector refractory period — eliminates dicrotic-notch double-counting
 *   • Per-species profiles (HUMAN / MARMOSET) with different HR ranges
 *   • Perfusion Index (PI) for signal-quality scoring
 *   • Outlier rejection: HR jumps > maxRate are filtered to last-good value
 *   • Motion variance gating: VALID=0 when recent IR variance is too high
 *   • HRV (RMSSD over last 5 inter-beat intervals)
 *   • Skin temperature from MAX30102 internal sensor
 *
 * Custom GATT service exposes:
 *   DATA characteristic (NOTIFY)  — ASCII status string, e.g.:
 *     "SAMPLING=1,HR=83,SPO2=98,AVG=82,VALID=1,PI=2.4,
 *      TEMP=33.5,HRV=38,SPECIES=human,CHARGE=charging"
 *   RAW  characteristic (NOTIFY)  — BINARY raw-waveform batch (little-endian),
 *     cheaper to build and ~2x smaller than ASCII so the link sustains a
 *     higher sample rate. A host can re-derive HR/SpO2 or watch the PPG live.
 *     Layout (magic 0xA6 = v2, 3-byte samples; 0xA5 = legacy 4-byte):
 *       [0]  uint8   magic = 0xA6
 *       [1]  uint8   nSamples
 *       [2]  uint8   nBeats
 *       [3]  uint32  seq   (packet counter; host detects drops)
 *       [7]  uint32  t0    (real millis of first sample)
 *       [11] uint16  dt    (real avg sample spacing, ms)
 *       [13] uint24 * nSamples  IR   (18-bit ADC, low 3 bytes LE)
 *       ...  uint24 * nSamples  Red
 *       ...  uint32 * nBeats    beat timestamps (device millis, may be 0)
 *   CMD  characteristic (WRITE)   — accepts:
 *     "START" / "STOP"
 *     "SPECIES:HUMAN" / "SPECIES:MARMOSET"
 */

#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "spo2_algorithm.h"
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <math.h>

// ---- BLE identifiers ----
#define DEVICE_NAME     "HealthMonitor1"
#define SERVICE_UUID    "12345678-1234-1234-1234-123456789abc"
#define DATA_CHAR_UUID  "12345678-1234-1234-1234-123456789abd"
#define CMD_CHAR_UUID   "12345678-1234-1234-1234-123456789abe"
#define RAW_CHAR_UUID   "12345678-1234-1234-1234-123456789abf"

// ---- I2C ----
#define SDA_PIN 9
#define SCL_PIN 8

// ---- TP4056 charging status ----
#define PIN_CHRG   7
#define PIN_STDBY  6

// ---- Finger threshold ----
#define FINGER_THRESHOLD 50000UL

// ---- Species profile ---------------------------------------------------------
enum Species { SPECIES_HUMAN = 0, SPECIES_MARMOSET = 1 };

struct ProfileParams {
    int   hrMin;            // bpm
    int   hrMax;            // bpm
    int   refractoryMs;     // min interval between beats
    int   maxHrChangeBpm;   // per-second rate-of-change limit for outlier reject
    float piMinPct;         // perfusion-index gate
    const char* label;
};

const ProfileParams PROFILES[] = {
    /* HUMAN    */ {  30, 220, 300, 30, 0.5f, "human"    },
    /* MARMOSET */ { 150, 400, 100, 60, 0.4f, "marmoset" },
};

Species  species = SPECIES_HUMAN;
const ProfileParams& prof() { return PROFILES[(int)species]; }

// ---- HR rolling average (display) ----
const byte HR_SAMPLES = 8;
byte   rates[HR_SAMPLES];
byte   rateIndex      = 0;
long   lastBeat       = 0;
float  beatsPerMinute = 0;
int    beatAvg        = 0;

// ---- Inter-beat-interval buffer for HRV (RMSSD over last N) ----
const byte IBI_BUF = 8;
unsigned long ibis[IBI_BUF];     // milliseconds
byte ibiCount = 0;
byte ibiHead  = 0;

// ---- HR outlier-reject median filter (last 5 reported HR) ----
const byte HR_MEDIAN = 5;
int hrHistory[HR_MEDIAN];
byte hrHistCount = 0;
byte hrHistHead  = 0;
int  lastGoodHR  = -1;
unsigned long lastGoodHRTime = 0;

// ---- SpO2 buffers ----
#define SPO2_BUF_LEN       100
#define SAMPLES_PER_ITER   10
uint32_t irBuffer[SPO2_BUF_LEN];
uint32_t redBuffer[SPO2_BUF_LEN];
int32_t  spo2;
int8_t   validSPO2;
int32_t  hrFromAlgo;
int8_t   validHR;

// ---- Sampling state ----
bool sampling          = false;
int  samplesCollected  = 0;
bool sensorOk          = false;
bool deviceConnected   = false;

// SpO2 is computed on a slow cadence, NOT every cycle. The maxim algorithm is
// expensive (~hundreds of ms) and running it every loop was starving the beat
// detector — leaving the PPG blind ~0.6 s out of every 1 s. SpO2 changes slowly
// so 3 s is plenty, and the IR stream now feeds beat detection continuously.
unsigned long lastSpO2Calc = 0;
const unsigned long SPO2_INTERVAL_MS = 3000;

// HR / HRV are only reported when a real beat was seen within this window;
// otherwise we send "invalid" instead of echoing the last good value forever.
const unsigned long HR_TIMEOUT_MS = 4000;

// Diagnostic: peak MAX30102 FIFO occupancy (0–31) seen between status notifies.
// Near 0 => firmware drains as fast as the sensor fills (sensor-rate-limited);
// near 31 => the FIFO is backing up (firmware/link can't keep up, dropping).
byte maxFifo = 0;

// Sensor config read back after setup() — the ACTUAL sample rate (Hz), pulse
// width (us), and sample averaging the chip is running, vs what we asked for.
int cfgSR  = 0;
int cfgPW  = 0;
int cfgAvg = 0;

// Cumulative FIFO overflow count (register 0x05, OVF_COUNTER) this session. If
// this climbs, the chip produced more samples than the firmware read = we ARE
// dropping samples = the ~250 Hz limit is the firmware/link, not the sensor.
unsigned long ovfTotal = 0;

// Loop profiling (micros) — where does the wall-clock go? Reset each status
// window; reported as PROF=read/notify/spo2 (avg us/sample, avg us/raw-notify,
// last SpO2 call us). Reveals whether reads (sensor wait), BLE notify, or the
// SpO2 algorithm dominate the loop.
unsigned long profReadUs = 0, profReadN = 0;
unsigned long profNotifyUs = 0, profNotifyN = 0;
unsigned long profSpO2Us = 0;

// ---- Slow signals (temperature) ----
float tempC = 0;
unsigned long lastTempRead = 0;
const unsigned long TEMP_INTERVAL_MS = 5000;     // MAX30102 temp slow, ~once / 5 s

// ---- Raw waveform transmit buffers (PPG samples + beats since last notify) --
// We flush a packet every RAW_FLUSH_AT samples. Samples are now packed as 3 bytes
// each (18-bit ADC fits in 24 bits), so more fit per packet than the old 4-byte
// encoding. Worst-case packet must stay under the ~244 B MTU:
//   13 header + RAW_TX_MAX*6 (IR+Red, 3B each) + BEAT_TX_MAX*4
//   = 13 + 162 + 64 = 239 B.
const byte    RAW_TX_MAX   = 27;     // hard cap on samples buffered per packet
const byte    RAW_FLUSH_AT = 24;     // flush once this many have accumulated
uint32_t      rawIR[RAW_TX_MAX];
uint32_t      rawRed[RAW_TX_MAX];
byte          rawCount = 0;
unsigned long rawT0    = 0;          // real millis() of first sample in batch
unsigned long rawTlast = 0;          // real millis() of most recent sample
uint32_t      rawSeq   = 0;          // monotonic packet counter

const byte    BEAT_TX_MAX = 16;
unsigned long beatTx[BEAT_TX_MAX];   // absolute device-millis of recent beats
byte          beatTxCount = 0;

MAX30105 sensor;
BLECharacteristic*  dataChar = nullptr;
BLECharacteristic*  cmdChar  = nullptr;
BLECharacteristic*  rawChar  = nullptr;
BLEServer*          server   = nullptr;

unsigned long lastNotify = 0;
const unsigned long NOTIFY_INTERVAL_MS = 400;    // 2.5 Hz

// ============================================================================
//                                  Helpers
// ============================================================================

void resetHRState() {
    for (byte i = 0; i < HR_SAMPLES; i++) rates[i] = 0;
    rateIndex = 0;
    beatAvg = 0;
    beatsPerMinute = 0;
    lastBeat = 0;

    ibiCount = 0;
    ibiHead  = 0;

    hrHistCount = 0;
    hrHistHead  = 0;
    lastGoodHR  = -1;
    lastGoodHRTime = 0;

    validHR = 0;
    validSPO2 = 0;
    hrFromAlgo = 0;
    spo2 = 0;

    rawCount    = 0;
    rawTlast    = 0;
    beatTxCount = 0;

    lastSpO2Calc = 0;
}

void startSampling() {
    if (!sensorOk) return;
    if (sampling)   return;
    sensor.wakeUp();
    delay(20);
    while (sensor.available()) {
        sensor.getRed();
        sensor.getIR();
        sensor.nextSample();
    }
    resetHRState();
    samplesCollected = 0;
    rawSeq = 0;              // fresh raw-stream sequence for this session
    ovfTotal = 0;            // fresh overflow count for this session
    sampling = true;
}

void stopSampling() {
    if (!sampling) return;
    if (sensorOk)  sensor.shutDown();
    sampling = false;
    samplesCollected = 0;
    resetHRState();
}

const char* readChargeState() {
    bool charging = (digitalRead(PIN_CHRG)  == LOW);
    bool full     = (digitalRead(PIN_STDBY) == LOW);
    if (charging && !full) return "charging";
    if (full && !charging) return "full";
    if (!charging && !full) return "battery";
    return "fault";
}

// Read one MAX30102 register directly over I2C (for config readback). The
// sensor's I2C address is 0x57.
uint8_t readSensorReg(uint8_t reg) {
    Wire.beginTransmission((uint8_t)0x57);
    Wire.write(reg);
    Wire.endTransmission(false);          // repeated start, keep bus
    Wire.requestFrom((uint8_t)0x57, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0;
}

// Perfusion Index — pulsatile amplitude as a % of DC level over the SpO2 buffer
float computePerfusionIndex() {
    if (samplesCollected < SPO2_BUF_LEN) return 0.0f;
    uint32_t irMax = 0, irMin = UINT32_MAX;
    uint64_t irSum = 0;
    for (int i = 0; i < SPO2_BUF_LEN; i++) {
        uint32_t v = irBuffer[i];
        if (v > irMax) irMax = v;
        if (v < irMin) irMin = v;
        irSum += v;
    }
    float mean = (float)irSum / SPO2_BUF_LEN;
    if (mean < 1.0f) return 0.0f;
    return 100.0f * ((float)(irMax - irMin)) / mean;
}

// HRV (RMSSD) over the buffered IBIs — root-mean-square of successive differences
float computeRMSSD() {
    if (ibiCount < 3) return 0.0f;
    // Walk the circular buffer in order
    long sumSq = 0;
    byte n = 0;
    byte prevIdx = ibiHead;
    if (prevIdx == 0) prevIdx = IBI_BUF - 1;
    else              prevIdx -= 1;
    // Start from the oldest entry; if buffer not full, start at index 0
    byte start = (ibiCount < IBI_BUF) ? 0 : ibiHead;
    long prev = (long)ibis[start];
    for (byte i = 1; i < ibiCount; i++) {
        byte idx = (start + i) % IBI_BUF;
        long cur = (long)ibis[idx];
        long diff = cur - prev;
        sumSq += diff * diff;
        prev = cur;
        n++;
    }
    if (n == 0) return 0.0f;
    return sqrtf((float)sumSq / (float)n);
}

void pushIBI(unsigned long ms) {
    ibis[ibiHead] = ms;
    ibiHead = (ibiHead + 1) % IBI_BUF;
    if (ibiCount < IBI_BUF) ibiCount++;
}

// Median of last N HRs (insertion sort — N is tiny)
int hrMedian() {
    if (hrHistCount == 0) return -1;
    int tmp[HR_MEDIAN];
    for (byte i = 0; i < hrHistCount; i++) tmp[i] = hrHistory[i];
    for (byte i = 1; i < hrHistCount; i++) {
        int v = tmp[i]; byte j = i;
        while (j > 0 && tmp[j-1] > v) { tmp[j] = tmp[j-1]; j--; }
        tmp[j] = v;
    }
    return tmp[hrHistCount / 2];
}

void pushHR(int hr) {
    hrHistory[hrHistHead] = hr;
    hrHistHead = (hrHistHead + 1) % HR_MEDIAN;
    if (hrHistCount < HR_MEDIAN) hrHistCount++;
}

// Apply species range + rate-of-change limit; returns -1 if rejected.
int applyOutlierFilter(int candidate) {
    if (candidate < prof().hrMin || candidate > prof().hrMax) return -1;
    unsigned long now = millis();
    if (lastGoodHR > 0) {
        unsigned long dt = now - lastGoodHRTime;
        if (dt == 0) dt = 1;
        int allowed = (int)((unsigned long)prof().maxHrChangeBpm * dt / 1000UL) + 5;
        if (abs(candidate - lastGoodHR) > allowed) return -1;
    }
    lastGoodHR = candidate;
    lastGoodHRTime = now;
    return candidate;
}

// ============================================================================
//                              BLE callbacks
// ============================================================================

class CmdCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* c) override {
        String val = c->getValue();
        val.trim();
        String upper = val;
        upper.toUpperCase();

        if (upper == "START") {
            startSampling();
        } else if (upper == "STOP") {
            stopSampling();
        } else if (upper.startsWith("SPECIES:")) {
            String sp = upper.substring(8);
            sp.trim();
            if (sp == "HUMAN")    { species = SPECIES_HUMAN;    resetHRState(); }
            if (sp == "MARMOSET") { species = SPECIES_MARMOSET; resetHRState(); }
        }
    }
};

class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* s) override {
        deviceConnected = true;
    }
    void onDisconnect(BLEServer* s) override {
        deviceConnected = false;
        stopSampling();
        BLEDevice::startAdvertising();
    }
};

// ============================================================================
//                              Sample / loop
// ============================================================================

void readOneSampleIntoBuffer(int pos) {
    while (!sensor.available()) sensor.check();
    uint32_t ir  = sensor.getIR();
    uint32_t red = sensor.getRed();
    redBuffer[pos] = red;
    irBuffer[pos]  = ir;

    // Stash the raw optical sample for the next RAW notify (pre-bpm waveform),
    // recording the real read time so the host sees the true sample cadence.
    if (rawCount < RAW_TX_MAX) {
        unsigned long nowMs = millis();
        if (rawCount == 0) rawT0 = nowMs;
        rawTlast = nowMs;
        rawIR[rawCount]  = ir;
        rawRed[rawCount] = red;
        rawCount++;
    }

    if (checkForBeat(ir)) {
        long now = millis();
        long delta = now - lastBeat;
        // Refractory: ignore peaks too soon after previous beat (dicrotic notch)
        if (delta < prof().refractoryMs) return;
        lastBeat = now;
        float bpm = 60000.0f / (float)delta;
        if (bpm > prof().hrMin && bpm < prof().hrMax) {
            int filtered = applyOutlierFilter((int)bpm);
            if (filtered > 0) {
                pushHR(filtered);
                pushIBI((unsigned long)delta);
                // Record the confirmed beat time for the next RAW notify.
                if (beatTxCount < BEAT_TX_MAX) beatTx[beatTxCount++] = (unsigned long)now;
                rates[rateIndex % HR_SAMPLES] = (byte)min(filtered, 255);
                rateIndex++;
                int sum = 0;
                byte n = (rateIndex < HR_SAMPLES) ? rateIndex : HR_SAMPLES;
                for (byte i = 0; i < n; i++) sum += rates[i];
                beatAvg = sum / n;
                beatsPerMinute = (float)filtered;
            }
        }
    }
    sensor.nextSample();
}

void sampleStep() {
    if (!sensorOk || !sampling) return;
    if (samplesCollected < SPO2_BUF_LEN) {
        readOneSampleIntoBuffer(samplesCollected);
        samplesCollected++;
        if (samplesCollected == SPO2_BUF_LEN) {
            // First full buffer: compute SpO2 once and start the slow cadence.
            maxim_heart_rate_and_oxygen_saturation(
                irBuffer, SPO2_BUF_LEN, redBuffer,
                &spo2, &validSPO2, &hrFromAlgo, &validHR);
            lastSpO2Calc = millis();
        }
        return;
    }
    // Diagnostic: sample the chip-FIFO occupancy BEFORE we drain it this cycle.
    // (writePtr - readPtr) mod 32 = samples waiting in the sensor's FIFO.
    byte occ = (byte)((sensor.getWritePointer() - sensor.getReadPointer()) & 0x1F);
    if (occ > maxFifo) maxFifo = occ;
    // OVF_COUNTER (0x05) counts samples lost to overflow since the last FIFO
    // read; read it BEFORE draining (which resets it) and accumulate. A growing
    // total = the chip outran the firmware (definitive "we're dropping" signal).
    ovfTotal += (unsigned long)(readSensorReg(0x05) & 0x1F);

    // Steady state: slide the buffer and read fresh samples every cycle so the
    // beat detector (inside readOneSampleIntoBuffer) sees a continuous stream.
    for (byte i = SAMPLES_PER_ITER; i < SPO2_BUF_LEN; i++) {
        redBuffer[i - SAMPLES_PER_ITER] = redBuffer[i];
        irBuffer[i - SAMPLES_PER_ITER]  = irBuffer[i];
    }
    unsigned long _mr = micros();                // profile the read batch
    for (byte i = SPO2_BUF_LEN - SAMPLES_PER_ITER; i < SPO2_BUF_LEN; i++) {
        readOneSampleIntoBuffer(i);
    }
    profReadUs += micros() - _mr;
    profReadN  += SAMPLES_PER_ITER;
    // Run the expensive SpO2 algorithm only on its slow cadence — this is the
    // one heavy call, so keeping it off the per-cycle path is what restores
    // continuous PPG sampling (and reliable beat detection).
    if (millis() - lastSpO2Calc >= SPO2_INTERVAL_MS) {
        unsigned long _ms = micros();
        maxim_heart_rate_and_oxygen_saturation(
            irBuffer, SPO2_BUF_LEN, redBuffer,
            &spo2, &validSPO2, &hrFromAlgo, &validHR);
        profSpO2Us = micros() - _ms;             // last SpO2 call duration
        lastSpO2Calc = millis();
    }
}

// ============================================================================
//                              Notification packet
// ============================================================================

void sendStatusNotify() {
    if (!deviceConnected || dataChar == nullptr) return;

    char packet[220];          // room for the diagnostic fields (FIFO/OVF/CFG/PROF)
    const char* chg = readChargeState();
    const char* sp  = prof().label;

    if (!sensorOk) {
        snprintf(packet, sizeof(packet),
                 "SENSOR=error,CHARGE=%s,SPECIES=%s", chg, sp);
    } else if (!sampling) {
        snprintf(packet, sizeof(packet),
                 "SAMPLING=0,HR=-1,SPO2=-1,AVG=0,VALID=0,"
                 "CHARGE=%s,SPECIES=%s", chg, sp);
    } else if (samplesCollected < SPO2_BUF_LEN) {
        int pct = (samplesCollected * 100) / SPO2_BUF_LEN;
        snprintf(packet, sizeof(packet),
                 "SAMPLING=1,HR=-1,SPO2=-1,AVG=0,VALID=0,WARMUP=%d,"
                 "CHARGE=%s,SPECIES=%s", pct, chg, sp);
    } else {
        uint32_t latestIR = irBuffer[SPO2_BUF_LEN - 1];
        bool fingerOn    = latestIR >= FINGER_THRESHOLD;

        float pi    = computePerfusionIndex();
        int displaySpO2 = validSPO2 ? (int)spo2 : -1;

        // HR / HRV are trustworthy only if a real beat was detected recently.
        // When the beat detector goes quiet (weak signal, motion), report
        // invalid rather than echoing the last good value indefinitely.
        bool hrFresh = (lastGoodHR > 0) &&
                       (millis() - lastGoodHRTime < HR_TIMEOUT_MS);
        int   median    = hrMedian();
        int   reportHR  = (hrFresh && median > 0) ? median : -1;
        int   reportAvg = hrFresh ? beatAvg : 0;
        float reportHrv = hrFresh ? computeRMSSD() : 0.0f;

        bool piOk = (pi >= prof().piMinPct);
        bool ok   = fingerOn && piOk && validSPO2 && hrFresh;

        if (!fingerOn) {
            snprintf(packet, sizeof(packet),
                     "SAMPLING=1,HR=-1,SPO2=-1,AVG=0,VALID=0,PI=%.1f,"
                     "TEMP=%.1f,HRV=0,SPECIES=%s,CHARGE=%s",
                     pi, tempC, sp, chg);
        } else {
            snprintf(packet, sizeof(packet),
                     "SAMPLING=1,HR=%d,SPO2=%d,AVG=%d,VALID=%d,PI=%.1f,"
                     "TEMP=%.1f,HRV=%.0f,SPECIES=%s,CHARGE=%s",
                     reportHR, displaySpO2, reportAvg, (int)ok,
                     pi, tempC, reportHrv, sp, chg);
        }
    }

    // Append the FIFO-occupancy + cumulative overflow diagnostics while sampling
    // (auto-appears in the GUI footer). Reset the running peak after each report;
    // OVF is cumulative for the session (a climbing value = dropped samples).
    if (sampling && sensorOk) {
        size_t len = strlen(packet);
        snprintf(packet + len, sizeof(packet) - len,
                 ",FIFO=%d,OVF=%lu", maxFifo, ovfTotal);
        maxFifo = 0;
    }
    // Append the config readback (actual sample rate / pulse width) so we can
    // see, live in the GUI footer, what the chip is really set to.
    if (sensorOk && cfgSR > 0) {
        size_t len = strlen(packet);
        snprintf(packet + len, sizeof(packet) - len,
                 ",CFG=%d/%d/%d", cfgSR, cfgPW, cfgAvg);   // sr/pw/avg
    }
    // Loop profile: avg us/sample read, avg us/raw-notify, last SpO2 us. Reset
    // the accumulators each window so PROF reflects the most recent ~400 ms.
    if (sampling && sensorOk) {
        unsigned long rdus = profReadN   ? profReadUs   / profReadN   : 0;
        unsigned long nfus = profNotifyN ? profNotifyUs / profNotifyN : 0;
        size_t len = strlen(packet);
        snprintf(packet + len, sizeof(packet) - len,
                 ",PROF=%lu/%lu/%lu", rdus, nfus, profSpO2Us);
        profReadUs = profReadN = profNotifyUs = profNotifyN = 0;
    }

    dataChar->setValue((uint8_t*)packet, strlen(packet));
    dataChar->notify();
}

// Raw waveform batch as a compact little-endian BINARY packet (see the layout
// in the header comment). Sent on its own characteristic so the ASCII status
// packet stays unchanged. Binary is cheaper to build than snprintf'd decimals
// and ~2x smaller on the air, which lets the link sustain a higher rate.
void sendRawNotify() {
    if (!deviceConnected || rawChar == nullptr) return;
    if (!sampling || rawCount == 0) return;     // nothing to stream yet

    // Real average sample spacing for this batch (ms), measured from actual
    // read times — not an assumed rate.
    uint16_t realDT = (rawCount > 1)
                      ? (uint16_t)((rawTlast - rawT0) / (rawCount - 1))
                      : (uint16_t)(1000 / 25);

    // Magic 0xA6 = binary v2: IR/Red samples packed as 3 bytes each (18-bit ADC
    // fits in 24 bits), saving 25% vs the old 4-byte (0xA5) layout. Timestamps
    // (t0, beats) stay 4 bytes — they exceed 24 bits. Header is 13 bytes.
    // Max size: 13 + RAW_TX_MAX*6 + BEAT_TX_MAX*4 = 13+162+64 = 239 B (< MTU).
    uint8_t buf[256];
    int p = 0;
    buf[p++] = 0xA6;            // magic / version (v2 = 3-byte samples)
    buf[p++] = rawCount;        // nSamples
    buf[p++] = beatTxCount;     // nBeats
    memcpy(buf + p, &rawSeq, 4); p += 4;
    uint32_t t0 = (uint32_t)rawT0; memcpy(buf + p, &t0, 4); p += 4;
    memcpy(buf + p, &realDT, 2); p += 2;
    for (byte i = 0; i < rawCount; i++) {        // IR, low 3 bytes (little-endian)
        uint32_t v = rawIR[i];
        buf[p++] = v & 0xFF; buf[p++] = (v >> 8) & 0xFF; buf[p++] = (v >> 16) & 0xFF;
    }
    for (byte i = 0; i < rawCount; i++) {        // Red, low 3 bytes
        uint32_t v = rawRed[i];
        buf[p++] = v & 0xFF; buf[p++] = (v >> 8) & 0xFF; buf[p++] = (v >> 16) & 0xFF;
    }
    for (byte i = 0; i < beatTxCount; i++) { uint32_t b = (uint32_t)beatTx[i]; memcpy(buf + p, &b, 4); p += 4; }

    unsigned long _mn = micros();                // profile the BLE notify
    rawChar->setValue(buf, p);
    rawChar->notify();
    profNotifyUs += micros() - _mn;
    profNotifyN++;

    rawSeq++;
    rawCount    = 0;
    beatTxCount = 0;
}

// ============================================================================
//                                  setup / loop
// ============================================================================

void setup() {
    pinMode(PIN_CHRG,  INPUT_PULLUP);
    pinMode(PIN_STDBY, INPUT_PULLUP);

    Wire.begin(SDA_PIN, SCL_PIN);
    if (sensor.begin(Wire, I2C_SPEED_FAST)) {
        // Args: powerLevel, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange
        // 2-LED, sampleRate=800, pulseWidth=215 us (17-bit) -> ~250 Hz, the best
        // config found. PROF profiling proved the per-sample READ WAIT dominates
        // the loop (8-14 ms/sample at 1600/118), i.e. the MAX30102's own sample
        // production is the limit, not BLE/notify/SpO2. Higher configs produce
        // SLOWER (chip timing quirk), so 800/215 is the marmoset-HRV sweet spot.
        sensor.setup(60, 1, 2, 800, 215, 4096);
        // Register readback: SPO2_CONFIG (0x0A) bits[4:2]=sample rate,
        // bits[1:0]=pulse width; FIFO_CONFIG (0x08) bits[7:5]=sample averaging.
        {
            uint8_t r0a = readSensorReg(0x0A);
            uint8_t r08 = readSensorReg(0x08);
            const int SR_MAP[8]  = {50, 100, 200, 400, 800, 1000, 1600, 3200};
            const int PW_MAP[4]  = {69, 118, 215, 411};
            const int AVG_MAP[8] = {1, 2, 4, 8, 16, 32, 32, 32};
            cfgSR  = SR_MAP[(r0a >> 2) & 0x07];
            cfgPW  = PW_MAP[r0a & 0x03];
            cfgAvg = AVG_MAP[(r08 >> 5) & 0x07];
        }
        sensor.enableDIETEMPRDY();   // enable temperature ready interrupt
        sensor.shutDown();
        sensorOk = true;
    } else {
        sensorOk = false;
    }

    BLEDevice::init(DEVICE_NAME);
    BLEDevice::setMTU(247);
    server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());

    BLEService* svc = server->createService(SERVICE_UUID);
    dataChar = svc->createCharacteristic(
        DATA_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
    dataChar->addDescriptor(new BLE2902());
    cmdChar = svc->createCharacteristic(
        CMD_CHAR_UUID,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
    cmdChar->setCallbacks(new CmdCallbacks());
    rawChar = svc->createCharacteristic(
        RAW_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
    rawChar->addDescriptor(new BLE2902());
    svc->start();

    BLEAdvertising* adv = BLEDevice::getAdvertising();
    adv->addServiceUUID(SERVICE_UUID);
    adv->setScanResponse(true);
    adv->setMinPreferred(0x06);
    adv->setMaxPreferred(0x12);
    BLEDevice::startAdvertising();
}

void loop() {
    sampleStep();

    // Flush the raw waveform once a batch has accumulated. Larger batches mean
    // fewer BLE notifications per second, which keeps the link stable at high
    // sample rates (and we never exceed RAW_TX_MAX / the MTU).
    if (sampling && rawCount >= RAW_FLUSH_AT) sendRawNotify();

    // Slow signal: temperature roughly every 5 seconds when sensor is on
    if (sensorOk && sampling && millis() - lastTempRead > TEMP_INTERVAL_MS) {
        tempC = sensor.readTemperature();
        lastTempRead = millis();
    }

    if (millis() - lastNotify >= NOTIFY_INTERVAL_MS) {
        lastNotify = millis();
        sendStatusNotify();
        sendRawNotify();
    }

    if (!sampling) delay(10);
}
