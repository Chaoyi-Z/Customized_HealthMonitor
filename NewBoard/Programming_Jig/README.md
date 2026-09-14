# Programming jig + battery for the Animal Pulse Sensing flex board (CH591D, Schematic_V3)

## 1. What the debug pads are

The four round pads on the flex neck are the CH591D's **USB** port (PB11 = UD+, PB10 = UD−) plus GND and the board's
own **+5 V boost-converter output** (not a USB power input). The chip is programmed over USB with WCH's ISP bootloader.

Measured from the EasyEDA render (`debug_pad_layout.png`, ±0.05 mm; confirm in EasyEDA before ordering anything):

| Pad | x (mm) | y (mm) | Note |
|-----|-------:|-------:|------|
| 5V  | −6.09 | −1.21 | boost output — **leave unconnected** |
| GND | −4.50 | +1.11 | |
| D−  | −2.78 | −1.19 | UD− (PB10) |
| D+  | −1.14 | +1.11 | UD+ (PB11) |

Pad Ø 2.15 mm (silk ring Ø 2.39), rows 2.31 mm apart, ~1.65 mm between neighbouring pads along the neck,
neck 5.0 mm wide × ~8 mm long. Coordinates are in the pick-and-place frame (`PickAndPlace_FLEX.xlsx`).

**This is not a standard 2.54 / 2.0 / 1.27 mm pattern, so no off-the-shelf pogo cable or clip fits it.**
Use the printed jig below, or for a one-off flash simply tack-solder three wires (GND, D−, D+) to the pads.

## 2. The jig (files in this folder)

| File | Purpose |
|------|---------|
| `pogo_jig_base.stl` | Base plate 56 × 30 × 5 mm with a 0.5 mm pocket in the board outline, a through-slot under the MAX86916, two M3 self-tap holes |
| `pogo_jig_top.stl`  | Pin carrier 7.3 × 28 × 9 mm that drops into the neck notch, 4 pogo holes (Ø1.2) + 2 M3 clearance holes. Already oriented for printing (tongue up) |
| `pogo_jig.scad`     | Same geometry, editable. If EasyEDA gives different pad coordinates, edit `pads=[...]` and re-render |
| `pogo_jig_preview.png` | Section views for a sanity check |

Parts to buy:

- 4 × **P75-B1** spring pogo pins (Ø1.02 mm barrel, round head, ~16.5 mm long) — sold in 100-packs on Amazon, e.g. https://www.amazon.com/100pcs-P75-B1-1-02mm-Spring-Loaded/dp/B07BZJ74RC . (Round head "B1" is kinder to gold pads than the pointed "E2".)
- 2 × M3 × 16 mm screws (self-tap into the base plate).
- One sacrificial **USB-A cable** (or a USB-A pigtail). Standard colours: black = GND, white = D−, green = D+, red = +5 V.

The cable (any one of these, see `jig_wiring.png` for the hole-to-wire map):

- **USB-A male to bare-wire pigtail**, e.g. [PATIKIL 3-pack, 28 AWG](https://www.amazon.com/PATIKIL-Pigtail-Extension-Adapter-Charging/dp/B0DWJY8T6Q) or [ELNONE 0.5 m 2-pack](https://www.amazon.com/ELNONE-2Pcs-Short-Pigtail-Wire/dp/B0CKPV3MZR). Thin (28 AWG) is easier to solder to the pin tails than 22–24 AWG.
- **Adafruit 4448 USB-A Plug Breakout Cable** ($1.95, 30 cm, female jumper ends): https://www.adafruit.com/product/4448 . Cut the jumper sockets off and solder the wires, or leave them on and solder a short 0.64 mm header pin to each pogo tail so the sockets push on.
- Any old USB cable cut in half.

Wire colours are standard USB 2.0: pin 1 red = VBUS 5 V, pin 2 white = D−, pin 3 green = D+, pin 4 black = GND. Cheap pigtails sometimes swap colours, so check each wire against the plug pins with a meter first.

Assembly:

1. Print both parts, 0.12–0.2 mm layers, PLA or PETG. If the pin holes print tight, open them with a 1.0 mm drill.
2. Push the pins in from the top until the tips stick out **~1.3 mm below the tongue face** (the raised 4.7 mm strip). Fix with a drop of CA glue on the top side only.
3. Solder black→GND, white→D−, green→D+. **Cut and insulate the red wire** — the board's 5 V pad is an output.
4. Drop the board into the pocket (sensor window down, it goes into the slot), set the pin carrier in the neck notch (the notched corners of both parts go together), tighten the two screws until the carrier sits on the base.

### 2b. Magnetic snap-on version (no screws)

`pogo_jig_base_mag.stl` + `pogo_jig_top_mag.stl` (or set `magnetic=true` in the .scad). Same pad geometry; the two M3 screws are
replaced by two magnet pairs and two alignment pegs. The carrier snaps onto the board one-handed and pulls off with a tilt.

- Magnets: **4 × neodymium discs, Ø4 × 2 mm** (fits `pogo_jig_*_mag.stl`). Amazon listings checked live on 2026-09-03, all Prime-shipped:
  - [MAGNEZMATIC N52 4 mm × 2 mm, 100 pack](https://www.amazon.com/dp/B0HC4G9WGT) — $19.49, 0.94 lb pull each. Best grade; low stock.
  - [AplysiaTech N52 D4×2 mm, 400 pack](https://www.amazon.com/dp/B0F4MYLY85) — $23.99, 4.6★.
  - [BEST CHOICE MAGNETS 4×2 mm, 300 pack](https://www.amazon.com/dp/B0CCXZ1J6N) — $9.99, 4.5★, grade not stated (assume N35, ~70 % of N52 pull; still ample).
  Two N52 pairs give well over 1 kg of clamp against ~0.2–0.4 kg of pogo spring force; N35 still gives roughly 0.7 kg.
  Ø4 × 3 mm discs fit the `pogo_jig_*_mag_4x3.stl` files instead, e.g. [4×3 mm 100 pack](https://www.amazon.com/dp/B0G2RFCBYW) ($9.99, grade not stated) or
  [The Magnet Baron N52 4×3 mm](https://themagnetbaron.com/products/100pcs-4mm-x-3mm-5-32-x-1-8-disc-magnets).
- Pockets: Ø4.15 × 2.1 mm, two in the top face of the base and two in the underside of the carrier, 8 mm either side of the neck centre line.
  Press the magnets in flush and fix with a drop of CA or epoxy. Both faces sit ~0.1 mm below the plastic so the parts mate plastic-to-plastic.
- Alignment: two Ø3 × 2 mm pegs printed on the carrier drop into Ø3.3 holes in the base (12.3 mm either side of centre). Pegs give ±0.15 mm
  registration; the magnets only supply force. If the pegs print oversize, a light pass with a file is enough.
- **Polarity keying**: glue the two base magnets with opposite poles up (north up on the +y side, south up on the −y side), then set the carrier
  magnets so they attract in the correct orientation. A carrier put on backwards is then repelled, which is a second orientation guard on top of the notched corners.
- Keep the magnets on the jig only. A magnet or steel target on the board would add ~0.5–1 g to the wearable, pick up metal debris, and is
  incompatible with any MRI use, which is why a MagSafe-style board connector is not recommended for this device.

## 3. Flashing procedure

- **Never feed USB 5 V into the 5 V pad.** Power the board from its own battery: battery plugged in, USB cable plugged into the PC, then slide SW2 to ON.
- Install **WCHISPTool** (includes the CH372 USB driver): https://www.wch-ic.com/downloads/WCHISPTool_Setup_exe.html .
  Cross-platform CLI alternative: `wchisp` (`wchisp flash firmware.bin`) — https://github.com/ch32-rs/wchisp .
- Chip series CH59x → CH591, interface USB, load the `.hex`/`.bin`, Download.

Bootloader entry facts for the **CH591D (QFN20)**:

- A **blank chip enters the USB bootloader automatically** at power-on (flash address 0 erased). So the first flash of a fresh board "just works".
- Afterwards the bootloader is only entered if **PB7 is low at power-on** (Boot0 for CH591D; PB22 does not exist on this package). PB7 is QFN pin 10 and is **not connected to anything on this board**, so re-flashing needs one of:
  1. Firmware support: e.g. hold SW1 (PA9) at power-on → firmware erases its own first 4 KB (`FLASH_ROM_ERASE(0, 4096)`) and resets, so the bootloader sees blank flash and stays in ISP. (Derived from the documented blank-flash behaviour — test it on the first board before relying on it.) Same thing can be triggered by a BLE command.
  2. Touch QFN pin 10 (PB7) to GND with a fine probe while switching the board on. Awkward on a flex but workable under a scope.
  3. Next board revision: add a PB7 test pad (and ideally a standard 2.54 mm 1×4 or Tag-Connect footprint for the USB pads so a stock pogo clip fits).
- The board's VIO33 rail is 3.0 V, inside the CH591D spec (2.3–3.6 V).

## 4. Battery

CN1 is **JST SH, 1.0 mm pitch, 2-pin, side entry (SM02B-SRSS-TB)**. Mating plug: **JST SHR-02V-S-B**. Input range 3.6–4.2 V (single LiPo).
There is **no charger on the board**, so the cell must be charged externally.

Ready-made cells with the right plug (TinyCircuits, also on Amazon):

| Cell | Size (mm) | Weight | Max discharge | Price | Link |
|------|-----------|-------:|--------------:|------:|------|
| 150 mAh | 20 × 20 × 5 | 3.8 g | 150 mA | $4.95 | https://tinycircuits.com/products/lithium-ion-polymer-battery-3-7v-150mah |
| 70 mAh  | 15 × 15 × 5 | 1.9 g | 70 mA  | $4.95 | https://tinycircuits.com/products/lithium-ion-polymer-battery-3-7v-70mah |
| 40 mAh  | 20 × 8 × 3.5 | ~1 g | 40 mA | — | https://tinycircuits.com/products/lithium-ion-polymer-battery-3-7v-40mah |

All include a protection PCM. Recommended: **150 mAh** for bench/first animals (headroom for the MAX86916 LED pulses through the 5 V boost), 70 mAh where size matters.
Rough runtime at an estimated 8–20 mA average draw (BLE streaming + PPG + IMU): 150 mAh ≈ 7–18 h, 70 mAh ≈ 3–9 h. Measure it on the real firmware.

Charger with a JST SH socket: **TinyCircuits Tiny Battery Charger** (MCP73831, 100 mA default, micro-USB, $6.95)
https://tinycircuits.com/products/tiny-battery-charger . Adafruit/SparkFun chargers use JST PH 2.0 mm and would need an adapter.

Rolling your own leads: housing **SHR-02V-S-B** + pre-crimped **ASSHSSH28K152** SH jumper (DigiKey), cut in half and soldered to any cell.

**Polarity:** JST SH plugs are not standardised. With the antenna to the right and SW2 at the top, the CN1 pin nearest the neck is
"−" and the outer pin is "+" (silkscreened). Check with a meter that the red lead lands on "+". The board has a P-MOSFET reverse-polarity
guard (Q1), so a swapped plug will not damage anything — it just won't power up; swap the crimps in the housing to fix it.
Switch SW2 OFF before plugging or unplugging the cell.
