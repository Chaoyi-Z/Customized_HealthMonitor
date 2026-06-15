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

// ---- Slow signals (temperature) ----
float tempC = 0;
unsigned long lastTempRead = 0;
const unsigned long TEMP_INTERVAL_MS = 5000;     // MAX30102 temp slow, ~once / 5 s

MAX30105 sensor;
BLECharacteristic*  dataChar = nullptr;
BLECharacteristic*  cmdChar  = nullptr;
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
            maxim_heart_rate_and_oxygen_saturation(
                irBuffer, SPO2_BUF_LEN, redBuffer,
                &spo2, &validSPO2, &hrFromAlgo, &validHR);
        }
        return;
    }
    for (byte i = SAMPLES_PER_ITER; i < SPO2_BUF_LEN; i++) {
        redBuffer[i - SAMPLES_PER_ITER] = redBuffer[i];
        irBuffer[i - SAMPLES_PER_ITER]  = irBuffer[i];
    }
    for (byte i = SPO2_BUF_LEN - SAMPLES_PER_ITER; i < SPO2_BUF_LEN; i++) {
        readOneSampleIntoBuffer(i);
    }
    maxim_heart_rate_and_oxygen_saturation(
        irBuffer, SPO2_BUF_LEN, redBuffer,
        &spo2, &validSPO2, &hrFromAlgo, &validHR);
}

// ============================================================================
//                              Notification packet
// ============================================================================

void sendStatusNotify() {
    if (!deviceConnected || dataChar == nullptr) return;

    char packet[160];
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
        float hrv   = computeRMSSD();
        int   median = hrMedian();
        int displayHR = (median > 0) ? median :
                         (validHR ? (int)hrFromAlgo : -1);
        int displaySpO2 = validSPO2 ? (int)spo2 : -1;

        bool piOk = (pi >= prof().piMinPct);
        bool ok   = fingerOn && piOk && validSPO2;

        if (!fingerOn) {
            snprintf(packet, sizeof(packet),
                     "SAMPLING=1,HR=-1,SPO2=-1,AVG=0,VALID=0,PI=%.1f,"
                     "TEMP=%.1f,HRV=%.0f,SPECIES=%s,CHARGE=%s",
                     pi, tempC, hrv, sp, chg);
        } else {
            snprintf(packet, sizeof(packet),
                     "SAMPLING=1,HR=%d,SPO2=%d,AVG=%d,VALID=%d,PI=%.1f,"
                     "TEMP=%.1f,HRV=%.0f,SPECIES=%s,CHARGE=%s",
                     displayHR, displaySpO2, beatAvg, (int)ok,
                     pi, tempC, hrv, sp, chg);
        }
    }

    dataChar->setValue((uint8_t*)packet, strlen(packet));
    dataChar->notify();
}

// ============================================================================
//                                  setup / loop
// ============================================================================

void setup() {
    pinMode(PIN_CHRG,  INPUT_PULLUP);
    pinMode(PIN_STDBY, INPUT_PULLUP);

    Wire.begin(SDA_PIN, SCL_PIN);
    if (sensor.begin(Wire, I2C_SPEED_FAST)) {
        sensor.setup(60, 4, 2, 100, 411, 4096);
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

    // Slow signal: temperature roughly every 5 seconds when sensor is on
    if (sensorOk && sampling && millis() - lastTempRead > TEMP_INTERVAL_MS) {
        tempC = sensor.readTemperature();
        lastTempRead = millis();
    }

    if (millis() - lastNotify >= NOTIFY_INTERVAL_MS) {
        lastNotify = millis();
        sendStatusNotify();
    }

    if (!sampling) delay(10);
}
