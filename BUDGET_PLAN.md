# ESP32 Health Monitor — $1,500 Build Budget Plan

**Goal:** From current v1 prototype → 8–10 production-ready marmoset tail-wrap units, with 2 design iterations along the way to catch and fix problems before committing to the final batch.

**Timeline:** ~6–8 weeks (3 PCB iterations × 10 days each + testing time between).

**Total budget target:** $1,500 (with $500 reserve for surprises, bringing to $2,000 ceiling).

---

## Why this phasing matters

Custom rigid-flex PCBs cost $150–200 per 5-board run. The first design will have at least one thing wrong — wrong pad layout, missed pull-up, antenna keepout violation, sensor window in the wrong place, battery doesn't fit, etc. That's normal. Plan for **3 PCB runs**:

1. **Run 1 (verify electronics)**: Plain rigid PCB, no flex yet, just confirm the BQ25120A circuit works
2. **Run 2 (verify mechanics)**: Rigid-flex with the tail wrap, fit-check on actual marmoset
3. **Run 3 (production)**: Final 10–15 boards based on what runs 1 & 2 taught you

Skipping straight to Run 3 with no validation is the #1 way to spend $2000 and end up with 10 boards that don't fit. Don't do that.

---

## Phase 0 — Validation with current v1 board ($60, week 1)

Before spending any PCB money, validate the **tail-wrap concept** with the hardware you already have. The v1 PCB has the MAX30102 on a flex pigtail — that's all you need to test geometry and signal quality.

| Item | Vendor | Cost |
|------|--------|------|
| 3D-printed TPU tail dummies (5–8 mm dia, 95A hardness, 50 mm long, qty 5) | MIT Edgerton 3D printer or Shapeways | ~$15 |
| Polyimide / Kapton tape, 6 mm wide × 33 m roll | Amazon | $8 |
| 3M Coban self-adherent wrap, 1" × 5 yd, qty 4 rolls | Amazon | $15 |
| Medical-grade silicone tubing 6 mm ID (for skin-coupling tests) | McMaster-Carr 5236K232 | $12 |
| Finger pulse oximeter (Masimo MightySat or similar) for ground-truth comparison | Amazon | $50 |
| Hair clipper, fine animal-grade (Wahl Bravura) | Amazon | $80 |

**Subtotal: ~$180** (over by $20 — drop the hair clipper if your facility already has one; most marmoset rooms do)

**Deliverable:** Confirmed signal quality on a curved tail-sized surface, with motion. If this fails, no point ordering custom flex PCBs.

---

## Phase 1 — First custom PCB (electronics validation, $230, week 2-3)

Goal: validate the **BQ25120A power section + ESP32 + MAX30102** on a rigid PCB before adding the complexity of flex. Keep the form factor the same as v1 (~30×40 mm) so debugging is easy.

| Item | Vendor | Cost |
|------|--------|------|
| Rigid 2-layer PCB + PCBA, 5 boards assembled | **JLCPCB** | $70 |
| Extended-parts surcharge (if BQ25120A not in basic library) | JLCPCB | $25 |
| Spare BQ25120A chips (qty 5) | DigiKey | $14 |
| Spare ESP32-S3-MINI-1 modules (qty 3) | DigiKey | $15 |
| Spare MAX30102 sensors (qty 5) | DigiKey | $25 |
| LiPo batteries, 100 mAh, qty 5 (PKCELL 401020 or similar) | Adafruit | $25 |
| LiPo batteries, 50 mAh, qty 5 (smaller for tail-mount tests) | Adafruit | $35 |
| Shipping (JLCPCB DHL, ~3 days) | JLCPCB | $20 |

**Subtotal: ~$230**

**Deliverable:** 5 working boards with the new PMIC architecture, verified to:
- Charge from USB-C
- Run on battery with deep sleep (<20 µA)
- Wake on push button
- I²C battery voltage readout works
- BLE range matches v1
- MAX30102 still works on both 1.8V and 3.3V rails

If this passes, you're confident the electronics design is correct and ready to commit to the flex version. If something fails (it probably will — first runs always have issues), debug, redesign, send Run 1b ($90 for re-spin).

**Budget reserve for Run 1b respin: $90.**

---

## Phase 2 — Rigid-flex prototype (mechanical validation, $280, week 4-5)

Goal: take the working electronics from Phase 1 and put them on the actual rigid-flex form factor. Test fit on a marmoset (or proxy).

| Item | Vendor | Cost |
|------|--------|------|
| Rigid-flex PCB + PCBA, 5 boards assembled | **Seeed Fusion** | $170 |
| Rush production option (5 days vs 10) | Seeed | $40 |
| Custom 3D-printed harness/vest for marmoset (TPU 95A, fitted to ~350g animal) | MIT Edgerton + your animal's measurements | $30 |
| Medical-grade silicone elastic bands, various sizes (Polymer Plastics 1/8" × 3/8" × 1") | McMaster-Carr | $25 |
| Tegaderm transparent dressing (for skin-side attachment tests) | Amazon | $20 |
| Shipping (Seeed DHL) | Seeed | $25 |

**Subtotal: ~$310** (over by $30 — skip rush production if not time-critical, saves $40)

**Deliverable:** 5 wearable units fit-tested on a marmoset or marmoset-sized proxy. You'll learn:
- Does the wrap stay on without irritation?
- Is the cable strain relief adequate?
- Does motion cause sensor liftoff?
- Is the battery life realistic for a session (target: 4+ hours active, days idle)?

Almost certainly you'll need a Run 2b respin to fix something mechanical. Common findings: cable too short, wrap too narrow, sensor window misaligned, harness mount in wrong spot.

**Budget reserve for Run 2b respin: $200.**

---

## Phase 3 — Production batch ($380, week 6-7)

Goal: order the final batch of devices for actual experiments. Quantity = number of marmosets in your study + 30% spares for failures.

Assuming 6 animals in the study:

| Item | Vendor | Cost |
|------|--------|------|
| Rigid-flex PCB + PCBA, 10 boards assembled (production) | Seeed Fusion | $280 |
| 10 × LiPo 50 mAh tail-friendly batteries | Adafruit | $70 |
| 10 × custom-fit harnesses (one per animal) | MIT Edgerton | $50 |
| Bulk Coban wrap (12 rolls, 3M brand) | Amazon | $35 |
| Bulk silicone bands (50 pieces) | McMaster | $25 |
| Shipping | Seeed | $25 |

**Subtotal: ~$485** (over by $105 — order 8 boards instead of 10 if tight, saves $80)

**Deliverable:** 8–10 production units ready for live experiments.

---

## Phase 4 — Tooling and spares ($150, ongoing)

Stuff that pays for itself across the whole project. Buy at start of Phase 1.

| Item | Vendor | Cost |
|------|--------|------|
| USB microscope, 1000× (for inspecting BQ25120A WCSP solder joints) | Amazon | $45 |
| Saleae logic analyzer clone (for I²C debugging) | Amazon | $15 |
| ESD-safe precision tweezers, qty 2 | DigiKey | $20 |
| Magnifying lamp w/ ring light | Amazon | $40 |
| Isopropyl 99% (1 L, for cleaning) | Amazon | $12 |
| Flux pen (for solder rework) | DigiKey | $8 |
| Spare USB-C cables (3 ft, qty 3, USB-IF certified) | Anker | $20 |

**Subtotal: ~$160**

You already have access to MIT.nano and Edgerton, so no need to buy a reflow oven, hot air station, or oscilloscope. If you didn't have campus access, add $300 for a TS100 / hot air rework station combo.

---

## Total budget summary

| Phase | Cost | Cumulative |
|-------|------|------------|
| Phase 0 — Validation kit | $180 | $180 |
| Phase 1 — Electronics PCB | $230 | $410 |
| Phase 1b reserve (respin) | $90 | $500 |
| Phase 2 — Rigid-flex prototype | $310 | $810 |
| Phase 2b reserve (respin) | $200 | $1,010 |
| Phase 3 — Production batch | $485 | $1,495 |
| Phase 4 — Tools & spares | $160 | $1,655 |
| **Hidden contingency (shipping fees, customs, mistakes)** | $250 | **$1,905** |

**Lands you at ~$1,900 out of a $2,000 ceiling, with all 8–10 production units in hand.**

If you stay disciplined and Run 1 and Run 2 succeed on first try (rare but possible), you can land at ~$1,400 and use the remainder for extra spares or a publication-quality enclosure.

---

## Vendors at a glance

| Vendor | What | Why |
|--------|------|-----|
| **JLCPCB** | Plain rigid PCB + PCBA | Cheapest first-iteration (Phase 1) |
| **Seeed Fusion** | Rigid-flex + PCBA | Best rigid-flex shop for prototype quantities |
| **DigiKey** | Spare components, fast US shipping | Same-day stock for emergency parts |
| **Adafruit** | LiPo batteries with JST, breakout boards | Friendly small quantities, good docs |
| **McMaster-Carr** | Silicone bands, tubing, mechanical hardware | Next-day NY delivery, no minimum |
| **MIT Edgerton Center** | 3D printing, hand assembly help | Free w/ MIT ID, on-campus |
| **MIT.nano** | Reflow oven, PCB milling backup | Free w/ MIT training |

---

## What to NOT spend money on

| Tempting purchase | Why skip |
|-------------------|----------|
| Premium US PCB house (OSH Park, FCT) for prototypes | 5× cost for marginal quality improvement — overkill until you're shipping units to other labs |
| Custom injection-molded enclosure | Not needed for n=6 animals. Use the rigid-flex PCB + silicone band as-is. Mold tooling is $5k+ |
| Bluetooth Mesh / Thread / LoRa | BLE 5 already does what you need with MonkeyLogic LSL |
| Bigger battery (200+ mAh) | More weight on the tail. 50–100 mAh is plenty at BLE's ~15 mA draw — that's 3–6 hours active, weeks idle |
| Pulse oximetry FDA certification | Research use, not medical device. Save it for a clinical translation later |
| OLED display on the board | Battery drain, adds weight, you have the GUI on the laptop already |

---

## Recommended order of purchases (next 7 days)

1. **Today**: Order Phase 0 validation kit from Amazon ($180). Arrives in 2 days.
2. **This week**: Validate the tail-wrap concept with v1 PCB + Phase 0 materials. If it works, schematic for v2 in EasyEDA.
3. **End of week 1**: Submit Phase 1 PCB order to JLCPCB. 10-day lead time. Order spare components from DigiKey same day (next-day delivery).
4. **Week 2**: While waiting on PCBs, build the v6 GUI integration (LSL → MonkeyLogic verification).
5. **Week 3**: Phase 1 boards arrive, bring-up week. Confirm BQ25120A works.
6. **Week 4**: Order Phase 2 rigid-flex from Seeed.
7. **Week 5-6**: Fit checks, animal trials with Phase 2 boards.
8. **Week 7**: Phase 3 production order, locks the design.
9. **Week 8**: Final units in hand, ready for experiments.
