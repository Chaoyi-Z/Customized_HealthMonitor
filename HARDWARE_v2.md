# ESP32 Health Monitor — Hardware v2 (Shrink Revision)

Target form factor: **~15 × 25 mm main board** + MAX30102 flex tail (unchanged).
Goal: wearable-grade for marmoset harness mounting.

---

## Headline change

Replace **three separate power chips + slide switch + BOOT button** with **one PMIC**:

| v1 (current) | v2 (new) |
|--------------|----------|
| TP4056 (charger, SOP-8) | **BQ25120A** (2.5 × 2.6 mm WCSP-25) |
| ME6217 (3.3V LDO, SOT-23-5) | …integrated 3.3V buck in BQ25120A |
| XC6206 (1.8V LDO, SOT-23-5) | …integrated LDO in BQ25120A (set to 1.8V) |
| MSK12C02 slide switch | …integrated push-button manager in BQ25120A |
| SW1 BOOT button | **eliminated** — replaced by OTA + emergency solder pads |
| SW2 ENABLE button | **kept** — wired to BQ25120A MR/PB pin |

**Net result:** 4 ICs → 1 IC, 2 buttons → 1 button. Saves ~60% of power-section area and eliminates all the broken-button headaches.

---

## TI BQ25120A — why it's the right PMIC

A single 2.5 mm chip that does:

- **Linear charger** for single-cell LiPo, programmable 5–300 mA via I²C
- **3.3V buck converter** (300 mA max, 92% efficient at typical loads)
- **Programmable LDO** (100 mA, set to 1.8V for MAX30102 VLED rail)
- **Push-button controller** — short press wakes/sleeps, long press hard-reset
- **700 nA ship-mode current** — board sleeps for months on a full battery
- **Battery voltage monitor** with I²C readout (no more GPIO guessing for charge state)
- **I²C control** — share the same bus as MAX30102

Cost: ~$2.50 at DigiKey, available at LCSC for JLCPCB assembly.

---

## Power tree (v2)

```
USB-C 5V ──┐
           ├──► BQ25120A.VIN
Charge pads┘
                    │
                    ├──► CHG → BAT pin → LiPo 100 mAh
                    │
                    ├──► SYS (3.3V buck, 300mA) ──► ESP32-S3 3V3, MAX30102 VDD
                    │
                    ├──► LS/LDO (1.8V, 100mA)   ──► MAX30102 VLED+
                    │
                    ├──► MR/PB ◄── push button (SW_PWR) ─── GND
                    │
                    └──► SDA/SCL/INT ──► ESP32-S3 I²C bus + GPIO

Push button behavior (firmware handles):
  • Short press (<2s)  → wake from deep sleep / start sampling
  • Long press  (>3s)  → hard reset (BQ25120A handles in hardware)
  • Held during boot   → enter bootloader (via firmware check at startup)
```

---

## Schematic block diagram (text)

```
┌──────────────────┐        ┌─────────────────────┐
│   USB-C          │        │   ESP32-S3-MINI-1   │
│   ──────────     │        │                     │
│   VBUS ──────────┼──────► │ 3V3 ◄─── from BQ.SYS│
│   D+/D- ◄────────┼────────│ IO19/IO20 (USB)     │
│   GND            │        │ IO8/IO9 (I²C bus)   │──┐
└──────────────────┘        │ IO0  (boot, exp pad)│  │
                            │ EN   (chip enable)  │  │
                            │ IO4  (MAX int)      │  │
                            │ IO6  (BQ int)       │  │
                            └─────────────────────┘  │
                                                      │
┌──────────────────────────────────────────┐         │
│  BQ25120A  (2.5 × 2.6 mm WCSP-25)        │         │
│                                          │         │
│  VIN  ◄─── 5V from USB-C / charge pads   │         │
│  BAT  ◄──► LiPo +  (100 mAh)             │         │
│  SYS  ────► 3.3V to ESP32 + MAX VDD      │         │
│  LDO  ────► 1.8V to MAX VLED+            │         │
│  MR   ◄─── SW_PWR push button ─── GND    │         │
│  SDA  ◄───┐                              │         │
│  SCL  ◄───┼── shared I²C bus ◄───────────┼─────────┘
│  INT  ────┘    (also to MAX30102)        │
└──────────────────────────────────────────┘
                                                      
┌─────────────────────────────────┐                  
│  MAX30102 on flex tail          │                  
│  VDD   ◄── 3.3V from BQ.SYS     │                  
│  VLED+ ◄── 1.8V from BQ.LDO     │                  
│  SDA/SCL ◄── shared I²C bus     │                  
│  INT     ────► ESP32 IO4        │                  
└─────────────────────────────────┘                  

┌─────────────────────────────────┐
│  Emergency boot pads            │
│  GPIO0 ─── solder pad TP_BOOT   │  ← bridge to GND
│  GND   ─── solder pad TP_GND    │     with tweezers
│                                 │     only if OTA fails
│  EN    ─── solder pad TP_EN     │  ← reset signal
└─────────────────────────────────┘
```

---

## BOM (key parts only — for JLCPCB/LCSC ordering)

| Ref | Part | Package | LCSC # | Notes |
|-----|------|---------|--------|-------|
| U1 | ESP32-S3-MINI-1 | LGA-65 | C2913203 | Same as v1 |
| U2 | TI BQ25120AYFPR | WCSP-25 (2.5×2.6mm) | C2654987 | The PMIC |
| U3 | MAX30102EFD+T | OESIP-14 | C89215 | Same as v1, on flex tail |
| L1 | 2.2 µH inductor | 0805 | C167221 | For BQ25120A buck |
| C_in | 4.7 µF / 10 V | 0402 | C19666 | BQ25120A VIN cap |
| C_bat | 4.7 µF / 10 V | 0402 | C19666 | BAT pin cap |
| C_sys | 10 µF / 10 V | 0603 | C19702 | SYS output cap |
| C_ldo | 1 µF / 10 V | 0402 | C15849 | LDO output cap |
| SW_PWR | Tactile 3.5×3.5mm | SMD | C720477 | Power/wake button |
| J1 | USB-C 6-pin power-only | THT/SMD | C165948 | Or omit + use pads |
| J2 | 1.25mm 2-pin JST | SMD | C2685120 | LiPo connector |

All passives in **0402** to save space. Pull-ups for I²C: 4.7 kΩ on SDA/SCL (already required by MAX30102).

---

## PCB layout notes

1. **2-layer PCB is fine** — 4-layer only helps with very tight routing or impedance control. Stick with 2-layer to keep cost down.
2. **Components on both sides** — ESP32 module on top, BQ25120A + passives on bottom. Cuts area roughly in half.
3. **Antenna keepout** — the ESP32-S3-MINI-1 antenna sticks off one edge; that edge must have **no copper, no ground plane, no parts** within 15 mm of the antenna pattern. Easiest: orient the module so the antenna hangs over the corner of the PCB.
4. **MAX30102 flex tail** — keep the existing footprint. A 4-conductor 0.5 mm pitch flex cable connector (or just direct solder pads on the flex) carries SDA, SCL, INT, VDD, GND, VLED+.
5. **Emergency boot pads** — three 1.5 mm² gold pads on the bottom labeled `BOOT`, `EN`, `GND`. Used only if OTA flash fails.
6. **Test points** for BAT+, SYS, 1.8V, GND on the bottom for debugging.

---

## Where to get this made

You have three tiers depending on how much you want to do yourself:

### Tier 1 — fully outsourced (recommended for first prototype)

**JLCPCB + PCBA service** (Shenzhen, China)
- Upload Gerbers + BOM + pick-and-place file from EasyEDA or KiCad
- They source parts from LCSC, assemble, and ship in 7–10 days
- Cost for 5 fully assembled boards (with BQ25120A): **~$60–90 total**
- URL: https://jlcpcb.com/  → upload → tick "PCB Assembly"
- Best price/performance ratio in the industry right now
- ESP32-S3-MINI-1 and BQ25120A are both in their **Basic Parts** library (no extended-parts fee)

**PCBWay** (Shenzhen, China)
- Slightly more expensive (~$80–120 for 5 assembled) but better customer service and faster turnaround for complex builds
- URL: https://www.pcbway.com/
- Good if you want them to source unusual parts that LCSC doesn't carry

**Seeed Fusion** (Shenzhen, China)
- Similar pricing to JLCPCB, better for small flex-PCB orders
- URL: https://www.seeedstudio.com/fusion.html
- Use this for the MAX30102 flex tail if you redesign it

### Tier 2 — bare PCB only, you do assembly

**OSH Park** (Portland, OR, USA)
- Premium 2-layer PCBs in purple soldermask, made in USA
- ~$5–15 per 3 boards depending on size, ~2 week turnaround
- URL: https://oshpark.com/
- Use this if you want US-made PCBs and don't mind soldering yourself
- You'd then buy components from DigiKey/Mouser and assemble with a hot air station or reflow oven

### Tier 3 — in-house at MIT

You have free or low-cost access to:

- **MIT.nano fab** (Building 12) — PCB milling on the LPKF, SMD reflow oven, paste stencils. Free for MIT users with training. https://mitnano.mit.edu/
- **MIT Edgerton Center Maker Space** (4-409) — pick-and-place, reflow, hand assembly stations. Good for the BQ25120A WCSP package since they have proper tooling. https://edgerton.mit.edu/
- **MIT MakerWorks** (Hobby Shop area) — basic SMD soldering stations
- **Beaverworks** at Lincoln Lab — if you have a faculty sponsor, they have professional PCB assembly equipment

For the 2.5 mm WCSP BQ25120A specifically, you really want **machine placement + reflow oven**. Hand-soldering a 25-ball WCSP is possible but painful. The MIT.nano LPKF + ProtoFlow oven combo is the best free option.

---

## My recommended path for you

1. **Design** the schematic + PCB in **EasyEDA** (free, web-based, integrated with JLCPCB)
2. **Order assembled prototypes** from JLCPCB — 5 boards for ~$70 total, 10 days
3. **Once it works**, you can either keep using JLCPCB for production runs (up to ~100 boards still cost-effective) or move to a US-based assembler if MIT requires it for grant-funded work

For your marmoset application, 5–10 boards from JLCPCB is plenty for the foreseeable future. The BQ25120A is in their library, the ESP32-S3-MINI-1 is in their library, MAX30102 is in their library — zero extended-parts fees.

If you want, the next step is to actually draw the schematic in EasyEDA and generate the Gerbers. I can guide you through the EasyEDA UI step-by-step, or you can hand this document to an undergrad / Edgerton tech and they can do the layout in a few hours.

---

# Flex PCB / Tail-Wrap Edition

For the marmoset tail-mount use case, a **rigid-flex hybrid** PCB is the right architecture. Pure flex doesn't work because the ESP32-S3 module, the BQ25120A, the USB-C connector, and the LiPo battery are all rigid — they need rigid PCB sections. But the *connections between them*, and especially the sensor section that contacts the tail, can be flex.

## Recommended architecture: "two islands + tail"

```
  ┌──────────────┐                                                    ┌─────────┐
  │ RIGID MAIN   │═══════ flex cable (40-80 mm) ═══════════════════════│ FLEX    │
  │ ISLAND       │                                                    │ TAIL    │
  │ ~15 × 18 mm  │                                                    │ WRAP    │
  │              │                                                    │ ~10×30mm│
  │ • ESP32-S3   │                                                    │         │
  │ • BQ25120A   │                                                    │ MAX30102│
  │ • USB-C/pads │                                                    │ + LEDs  │
  │ • Power btn  │                                                    │         │
  │ • LiPo glued │                                                    │         │
  │   to back    │                                                    │         │
  └──────────────┘                                                    └─────────┘
       ↑                                                                  ↑
  Worn on harness /                                                  Wraps around
  vest / collar                                                      tail base,
                                                                     sensor faces in,
                                                                     velcro/silicone
                                                                     band over it
```

**Why this split:**

1. The marmoset is ~300–400 g. The max comfortable device weight is ~30 g (10 % body mass rule for chronic implants). A LiPo battery is the heaviest single item — you do **not** want it on the tail. Body-mounted main island keeps weight off the tail.
2. The tail is mobile and gets bitten/scratched. Keeping only the cheap sensor section on the tail means damage there doesn't kill the expensive ESP32 + battery.
3. The flex cable acts as a strain relief between the two — if the marmoset yanks the sensor, the flex flexes instead of ripping the main board apart.

## The tail-wrap flex section

Dimensions matched to marmoset tail anatomy:

| Parameter | Value | Why |
|-----------|-------|-----|
| Wrap width | 8–10 mm | Wide enough to not migrate, narrow enough to flex easily |
| Wrap length | 35–40 mm | Marmoset tail base circumference ~25 mm + 10–15 mm overlap for fastening |
| Sensor pad position | Centered, 12 mm from one end | LEDs face the underside of the tail (less hair, better perfusion) |
| Total flex thickness | 0.1–0.15 mm (polyimide) | Conforms tightly to skin without buckling |
| Stiffener at sensor | 0.4 mm FR4 island, 5 × 5 mm | Keeps MAX30102 flat against skin, distributes pressure |

**Fastening:** The simplest and least invasive option is a thin **silicone band** or **Coban veterinary wrap** over the entire flex wrap. Avoid Velcro — it catches fur. The flex itself does *not* need adhesive — friction + the wrap keeps it in place.

**Hair management:** Marmoset tail hair will block the MAX30102 LEDs. Two options:
- **Shave a small (~8×8 mm) spot** under the sensor at the start of the experiment. Hair regrows in ~2–3 weeks.
- **Use a small clear silicone "window"** as a hair-displacing optical coupler. Press-fit into the FR4 stiffener cutout. This is what wearable pulse-ox devices like Owlet use.

## Flex PCB stack-up

For JLCPCB / Seeed Fusion flex:

```
Top coverlay (polyimide, 25 µm) ────────────┐
Top adhesive (12 µm) ─────────────────────┐ │
Top copper trace (35 µm / 1 oz) ────────┐ │ │
Polyimide base (50 µm) ───────────────┐ │ │ │
Bottom copper trace (35 µm) ────────┐ │ │ │ │
Bottom adhesive (12 µm) ──────────┐ │ │ │ │ │
Bottom coverlay (polyimide, 25µm) │ │ │ │ │ │
                                  ▼ ▼ ▼ ▼ ▼ ▼
Total thickness: ~0.15 mm
```

For rigid sections (ESP32 main island, MAX30102 stiffener), the flex laminates onto **0.4 mm or 0.6 mm FR4** islands. Don't go thicker on the islands — extra stiffness creates stress concentrations where rigid meets flex.

**Critical layout rules:**
- No traces perpendicular to the bend axis — route 45° to the bend
- No vias within 1 mm of a flex bend
- Tear-drop pads at all rigid-to-flex transitions
- Coverlay opening only at component pads and the sensor window

## Where to get flex / rigid-flex made

This is where vendor choice matters a lot — not all PCB houses do flex well.

| Vendor | Flex quality | PCBA on flex? | Cost (5 boards) | Notes |
|--------|-------------|---------------|-----------------|-------|
| **Seeed Fusion** ⭐ | Excellent | Yes, full PCBA | **$120–180** | Best one-stop shop for prototypes. Has dedicated flex+rigid-flex assembly. |
| **PCBWay** | Excellent | Yes | $150–250 | Premium quality, slower turnaround |
| **JLCPCB** | OK for plain flex, limited rigid-flex | Limited (no PCBA on rigid-flex yet) | $80–120 (bare boards) | Cheapest for plain flex; you'd assemble the rigid sections yourself or send to a separate assembler |
| **Flexible Circuit Technologies (FCT)** | Excellent (US) | Yes | $400–800 | US-based, ITAR-compliant, slow (4-6 weeks). Use only if MIT grant requires US fab |
| **All Flex Solutions** (MN, USA) | Excellent (US) | Yes | $300–600 | Aerospace-grade, ITAR option, US-based |
| **Trackwise** (UK) | Excellent | Yes | £400+ | UK-based, very long flex specialty (meter-scale) |

**My recommendation for the marmoset prototype:** **Seeed Fusion** rigid-flex + PCBA service. ~$150 for 5 assembled boards including the BQ25120A, ESP32 module, MAX30102, and the polyimide flex tail. They have a dedicated rigid-flex line and won't try to talk you out of the design like JLCPCB might.

## Weight budget for marmoset (rough estimate)

| Component | Mass |
|-----------|------|
| Main rigid island (15×18 mm, 2-layer, 1.0 mm FR4) | ~1.2 g |
| ESP32-S3-MINI-1 module | 0.5 g |
| BQ25120A + passives | 0.1 g |
| LiPo 100 mAh (10×15×3.5 mm) | 2.2 g |
| Flex cable (40 mm × 8 mm × 0.15 mm) | 0.05 g |
| Tail wrap section + FR4 stiffener + MAX30102 | 0.4 g |
| Silicone band (10 mm × 35 mm) | 0.3 g |
| **Total** | **~4.7 g** |

A 400 g marmoset can carry this all day with no apparent discomfort (well under the 10 % rule). For a 300 g marmoset you'd want a 50 mAh battery instead (~1.1 g) to stay under 4 g total.

## Sensor placement on the tail

For a marmoset tail:

```
        ─── base of tail (where fur is densest) ──────► tip
       │                                                  │
       │     ↑ Mount the sensor here:                     │
       │     • 25–40 mm from the base                     │
       │     • Underside (less hair, better perfusion)    │
       │     • Above the ventral artery (visible vein)    │
       │                                                  │
       └──────────────────────────────────────────────────┘
              ↑
   Mark this spot with a tiny tattoo or shaved patch
   on day 1 so subsequent measurements are at the same
   anatomical location (consistency for chronic studies)
```

The MAX30102's red (660 nm) + IR (880 nm) wavelengths penetrate ~5 mm into tissue. Marmoset tail at this location is ~6–8 mm thick — perfect for reflectance pulse-ox geometry. SpO₂ should be reliable; HR is guaranteed reliable.

## Validation before going to production

Before you commit to a fab order, do **two cheap things first**:

1. **Mock up the wrap with a piece of 0.15 mm polyimide tape** + the existing PCB's MAX30102 flex tail. Tape it to a marmoset tail (or your own finger, then a colleague's wrist as a proxy for the tail's curvature) and verify signal quality and comfort. The current v1 PCB already has the MAX30102 on a flex pigtail — you can test the wrap concept *today*.

2. **3D print a tail dummy** (5 mm diameter, 50 mm long, in soft TPU at 95A hardness) to test fastening and signal stability under simulated motion. This catches a lot of mechanical problems before paying for a $150 flex PCB run.

If both of those work, then commit to the Seeed Fusion order with confidence.

