"""
Offline PPG analysis of a HealthMonitor recording, using NeuroKit2.

The rigorous counterpart to the GUI's live detector. It loads a `<name>_raw.csv`
(the full-rate raw PPG the GUI records) and:

  1. Splits the recording into device-clock SESSIONS at reboots and long gaps.
     (The device's millis() clock resets if it reboots — e.g. a flaky link or a
     draining battery — and disconnects leave gaps. Each session has its own
     clean, monotonic clock.)
  2. Within each session, gates out bad data — finger-off stretches and
     low-perfusion / motion segments — with a quality check that mirrors the
     firmware (IR DC level for finger presence + a perfusion-index threshold on
     the pulse band), then time-slices into contiguous GOOD segments.
     This matters because HRV must never be computed across a gap: a finger-off
     pause creates one enormous fake inter-beat interval that wrecks RMSSD.
  3. Per segment: species-aware bandpass + peak detection, NeuroKit Kubios
     artifact correction, then HRV.
  4. Reports usable vs. discarded time, a per-segment table, a gap-safe pooled
     HRV summary, and the full NeuroKit HRV suite on the longest clean segment.

Why custom peak detection instead of nk.ppg_peaks? NeuroKit's built-in PPG
detectors are validated on human data and assume human-scale timing; the
species-aware passband/refractory here also handle marmosets (~150-400 bpm).

Usage:
    pip install neurokit2 scipy numpy
    python analyze_recording.py <name>_raw.csv [--species human|marmoset]

Species is auto-detected from the companion status CSV (`<name>.csv`) when
present; --species overrides it.
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency: pip install numpy")
try:
    from scipy.signal import butter, filtfilt, find_peaks
except ImportError:
    sys.exit("Missing dependency: pip install scipy")
try:
    import neurokit2 as nk
except ImportError:
    sys.exit("Missing dependency: pip install neurokit2")

FINGER_THRESHOLD = 50000   # raw IR DC level for "finger on" (matches firmware)
QUALITY_WIN_S = 1.0        # window for the finger/perfusion quality check
MIN_SEGMENT_S = 20.0       # shortest clean run worth an HRV estimate
SESSION_GAP_MS = 2000.0    # a forward time jump bigger than this splits a session

# Mirrors SPECIES_PROFILES in health_monitor_gui.py (and the firmware PROFILES).
SPECIES_PROFILES = {
    "human":    {"hr_min": 30,  "hr_max": 220, "refractory_ms": 300, "max_hr_change": 30, "pi_min": 0.5},
    "marmoset": {"hr_min": 150, "hr_max": 400, "refractory_ms": 100, "max_hr_change": 60, "pi_min": 0.4},
}


def load_raw(raw_path: Path):
    """Return (device_t_ms array, IR array) from a _raw.csv."""
    t_ms, ir = [], []
    with raw_path.open() as f:
        for row in csv.DictReader(f):
            try:
                t_ms.append(int(row["device_t_ms"]))
                ir.append(float(row["ir"]))
            except (KeyError, ValueError):
                continue
    if len(t_ms) < 10:
        sys.exit(f"Too few samples in {raw_path.name}")
    return np.array(t_ms, dtype=float), np.array(ir, dtype=float)


def detect_species(raw_path: Path) -> str | None:
    """Most common 'species' value in the companion status CSV, if any."""
    status = raw_path.with_name(raw_path.name.replace("_raw.csv", ".csv"))
    if not status.exists():
        return None
    vals = []
    try:
        with status.open() as f:
            for row in csv.DictReader(f):
                v = (row.get("species") or "").strip().lower()
                if v:
                    vals.append(v)
    except OSError:
        return None
    return Counter(vals).most_common(1)[0][0] if vals else None


def split_sessions(t_ms):
    """Split where the device clock jumps backward (reboot) or forward by more
    than SESSION_GAP_MS (a disconnect gap). Returns list of (i0, i1) ranges."""
    d = np.diff(t_ms)
    breaks = np.where((d < 0) | (d > SESSION_GAP_MS))[0] + 1
    bounds = np.concatenate(([0], breaks, [len(t_ms)]))
    return [(int(bounds[k]), int(bounds[k + 1])) for k in range(len(bounds) - 1)]


def bandpass(sig, fs, prof):
    """Species-aware pulse-band filter, or None if fs is too low for the band."""
    lo = prof["hr_min"] / 60.0
    hi = min(prof["hr_max"] / 60.0, fs / 2.0 - 0.2)
    if hi <= lo:
        return None
    b, a = butter(2, [lo, hi], btype="band", fs=fs)
    return filtfilt(b, a, sig)


def quality_mask(raw_g, bp, fs, pi_min):
    """Per-sample good/bad mask. A window is GOOD when the IR DC level shows a
    finger is present AND the pulse-band amplitude (a perfusion index on the
    bandpassed signal, so baseline wander/motion don't fake it) clears pi_min."""
    n = len(raw_g)
    w = max(1, int(QUALITY_WIN_S * fs))
    mask = np.zeros(n, dtype=bool)
    for start in range(0, n, w):
        sl = slice(start, min(start + w, n))
        dc = float(np.mean(raw_g[sl]))
        if dc < FINGER_THRESHOLD:
            continue
        if 100.0 * float(np.ptp(bp[sl])) / dc >= pi_min:   # pulsatility %
            mask[sl] = True
    return mask


def find_segments(mask, min_len):
    """Contiguous True runs of at least min_len samples, as (i0, i1) ranges."""
    segs, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_len:
                segs.append((i, j))
            i = j
        else:
            i += 1
    return segs


def refine_peaks(sig, peaks):
    """Sub-sample peak refinement by parabolic interpolation. Fits a parabola
    through each peak and its two neighbours; the vertex gives a fractional
    sample position. This sharpens inter-beat timing below the sample period
    (important at lower rates / faster hearts, e.g. marmoset HRV). Returns
    fractional positions (same length as peaks)."""
    pos = peaks.astype(float)
    for k, i in enumerate(peaks):
        if 0 < i < len(sig) - 1:
            y0, y1, y2 = sig[i - 1], sig[i], sig[i + 1]
            denom = y0 - 2.0 * y1 + y2
            if denom != 0:
                delta = 0.5 * (y0 - y2) / denom
                if -0.5 <= delta <= 0.5:        # guard: true vertex is within ±0.5
                    pos[k] = i + delta
    return pos


def detect_beats(raw_seg, fs, prof):
    """Bandpass + peak-find + Kubios correction + sub-sample refinement on one
    clean segment. Returns (integer peak indices, IBIs ms) or None. The integer
    peaks feed NeuroKit/plots; the IBIs use the refined fractional positions for
    finer timing."""
    bp = bandpass(raw_seg, fs, prof)
    if bp is None:
        return None
    # Trim filtfilt edge transients (ringing where the segment starts/ends
    # abruptly) before thresholding — otherwise the spike inflates std and the
    # prominence gate misses real beats. Map peak indices back afterwards.
    guard = int(fs * 1.0)
    core, offset = (bp[guard:-guard], guard) if len(bp) > 3 * guard else (bp, 0)
    peaks, _ = find_peaks(core, distance=max(1, int(fs * prof["refractory_ms"] / 1000.0)),
                          prominence=0.4 * np.std(core))
    if len(peaks) < 4:
        return None
    peaks = peaks + offset
    try:
        _, peaks = nk.signal_fixpeaks(peaks, sampling_rate=fs, method="kubios",
                                      interval_min=60.0 / prof["hr_max"],
                                      interval_max=60.0 / prof["hr_min"])
        peaks = np.asarray(peaks, dtype=int)
    except Exception:
        pass
    # IBIs from sub-sample-refined positions (finer than 1/fs); integer peaks
    # are still returned for NeuroKit (needs sample indices) and the plot.
    pos = refine_peaks(bp, peaks)
    return peaks, np.diff(pos) / fs * 1000.0


def main():
    ap = argparse.ArgumentParser(description="Offline PPG/HRV analysis (NeuroKit2)")
    ap.add_argument("raw_csv", help="path to a <name>_raw.csv recording")
    ap.add_argument("--species", choices=list(SPECIES_PROFILES),
                    help="override species (else auto-detected, else human)")
    ap.add_argument("--no-plot", action="store_true", help="skip saving the plot")
    args = ap.parse_args()

    raw_path = Path(args.raw_csv)
    if not raw_path.exists():
        sys.exit(f"No such file: {raw_path}")

    species = args.species or detect_species(raw_path) or "human"
    prof = SPECIES_PROFILES[species]
    print(f"\nRecording : {raw_path.name}")
    print(f"Species   : {species}  (HR {prof['hr_min']}-{prof['hr_max']} bpm, "
          f"PI gate {prof['pi_min']}%)")

    t_ms, ir = load_raw(raw_path)
    # Effective rate from the typical intra-session sample interval (robust to
    # the reboots/gaps that would otherwise corrupt a naive min/max estimate).
    d = np.diff(t_ms)
    pos = d[(d > 0) & (d < 100)]
    if len(pos) == 0:
        sys.exit("Could not determine the sample rate from device_t_ms.")
    fs = 1000.0 / float(np.median(pos))
    sessions = split_sessions(t_ms)
    active = sum(t_ms[b - 1] - t_ms[a] for a, b in sessions) / 1000.0
    print(f"Samples   : {len(ir)}   effective rate {fs:.1f} Hz")
    print(f"Sessions  : {len(sessions)} device-clock session(s) "
          f"(split at reboots/gaps), {active:.0f}s of sampling")

    # --- per-session quality gate + segmentation ---
    min_seg = int(MIN_SEGMENT_S * fs)
    segs_out = []      # dicts: t (s), raw, peaks, nn (clean IBIs), succ (adjacent diffs)
    usable = 0.0
    candidates = 0
    for a, b in sessions:
        if b - a < min_seg:
            continue
        tl = (t_ms[a:b] - t_ms[a]) / 1000.0
        grid = np.arange(0.0, tl[-1], 1.0 / fs)
        if len(grid) < min_seg:
            continue
        raw_g = np.interp(grid, tl, ir[a:b])
        bp = bandpass(raw_g, fs, prof)
        if bp is None:
            sys.exit(f"Sample rate {fs:.0f} Hz too low for the {species} band.")
        mask = quality_mask(raw_g, bp, fs, prof["pi_min"])
        for i0, i1 in find_segments(mask, min_seg):
            candidates += 1
            res = detect_beats(raw_g[i0:i1], fs, prof)
            if res is None or len(res[1]) < 2:
                continue
            peaks, ibis = res
            # Beat-level quality: a segment can pass the perfusion gate yet still
            # have unreliable detection (sparse/irregular beats). Drop beats whose
            # instantaneous HR is more than the profile's max_hr_change from the
            # median (same rule as the live detector); take successive diffs only
            # between beats that stayed adjacent (never across a dropped beat);
            # and reject the whole segment if too few beats survive.
            inst = 60000.0 / ibis
            med = float(np.median(inst))
            keep = np.abs(inst - med) <= prof["max_hr_change"]
            nn = ibis[keep]
            if len(nn) < 10 or len(nn) / len(ibis) < 0.6:
                continue
            succ = np.diff(ibis)[keep[:-1] & keep[1:]]
            usable += (i1 - i0) / fs
            segs_out.append({"t": grid[i0:i1] - grid[i0], "raw": raw_g[i0:i1],
                             "peaks": peaks, "nn": nn, "succ": succ})

    rejected = candidates - len(segs_out)
    print(f"Quality   : {usable:.0f}s usable, {len(segs_out)} segment(s) analysed "
          f"({rejected} rejected for poor beat detection)")
    if not segs_out:
        sys.exit("No clean segment long enough for HRV. "
                 "(This recording may be mostly finger-off / disconnected.)")

    # --- per-segment table (cleaned beats) ---
    print(f"\n{'seg':>3} {'dur':>6} {'beats':>6} {'HR':>5} {'RMSSD':>7}")
    for n_seg, s in enumerate(segs_out, 1):
        hr = 60000.0 / np.median(s["nn"])
        rmssd = np.sqrt(np.mean(s["succ"] ** 2)) if len(s["succ"]) else float("nan")
        print(f"{n_seg:>3} {s['nn'].sum()/1000:>6.0f} {len(s['nn']):>6} "
              f"{hr:>5.0f} {rmssd:>7.1f}")

    # --- pooled HRV (gap-safe): pool clean NN across segments, but successive
    #     differences (RMSSD/pNN50) only between adjacent in-segment beats ---
    all_nn = np.concatenate([s["nn"] for s in segs_out])
    succ = np.concatenate([s["succ"] for s in segs_out])
    print("\n=== Pooled (gap-safe) ===")
    print(f"  HR (median)   {60000.0/np.median(all_nn):>8.0f} bpm")
    print(f"  Mean NN       {np.mean(all_nn):>8.1f} ms")
    print(f"  SDNN          {np.std(all_nn, ddof=1):>8.1f} ms")
    print(f"  RMSSD         {np.sqrt(np.mean(succ**2)):>8.1f} ms")
    print(f"  pNN50         {100*np.mean(np.abs(succ) > 50):>8.1f} %")

    # --- full NeuroKit suite on the longest clean segment (most reliable) ---
    longest = max(segs_out, key=lambda s: len(s["nn"]))
    dur = longest["nn"].sum() / 1000.0
    print(f"\n=== NeuroKit2 full HRV (longest segment: {dur:.0f}s) ===")
    try:
        hrv = nk.hrv(longest["peaks"], sampling_rate=fs, show=False)

        def show(col, label, unit=""):
            if col in hrv.columns and hrv[col].iloc[0] == hrv[col].iloc[0]:
                print(f"  {label:<16} {hrv[col].iloc[0]:>8.1f} {unit}")
        show("HRV_RMSSD", "RMSSD", "ms")
        show("HRV_SDNN", "SDNN", "ms")
        show("HRV_pNN50", "pNN50", "%")
        show("HRV_SD1", "Poincare SD1", "ms")
        show("HRV_SD2", "Poincare SD2", "ms")
        if dur >= 120:
            show("HRV_LF", "LF power", "ms^2")
            show("HRV_HF", "HF power", "ms^2")
            show("HRV_LFHF", "LF/HF")
        else:
            print("  (frequency domain skipped: segment < 2 min)")
    except Exception as e:
        print(f"  NeuroKit hrv() failed: {e}")

    # --- plot the longest clean segment with detected beats ---
    if not args.no_plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            bp = bandpass(longest["raw"], fs, prof)
            plt.figure(figsize=(12, 3.5))
            plt.plot(longest["t"], bp, color="#1f77b4", lw=0.7)
            plt.plot(longest["t"][longest["peaks"]], bp[longest["peaks"]], "r.", ms=4,
                     label="detected beats")
            # Robust y-limits so any residual filter-edge transient doesn't
            # squash the actual waveform out of view.
            lo, hi = np.percentile(bp, [0.5, 99.5])
            pad = (hi - lo) * 0.2 or 1.0
            plt.ylim(lo - pad, hi + pad)
            plt.title(f"{raw_path.name} — {species}, longest clean segment "
                      f"({dur:.0f}s, {60000.0/np.median(longest['nn']):.0f} bpm)")
            plt.xlabel("time within segment (s)"); plt.ylabel("filtered IR")
            plt.legend(loc="upper right"); plt.tight_layout()
            out = raw_path.with_name(raw_path.name.replace("_raw.csv", "_analysis.png"))
            plt.savefig(out, dpi=110)
            print(f"\nPlot saved -> {out}")
        except Exception as e:
            print(f"(plot skipped: {e})")


if __name__ == "__main__":
    main()
