"""Quick LSL stream verifier — run while health_monitor_gui.py is open."""
from pylsl import resolve_streams, StreamInlet
import time

print("Searching for 'HealthMonitor' LSL stream (5 s timeout)…")
all_streams = resolve_streams(wait_time=5.0)
streams = [s for s in all_streams if s.name() == "HealthMonitor"]

if not streams:
    print("NOT FOUND — make sure the GUI is running and pylsl is installed.")
else:
    print(f"Found: {streams[0].name()}  type={streams[0].type()}  "
          f"channels={streams[0].channel_count()}  rate={streams[0].nominal_srate()} Hz")
    print("Reading 10 samples (press Ctrl+C to quit early)…\n")
    inlet = StreamInlet(streams[0])
    labels = ["HR", "SpO2", "HR_avg", "Valid", "PI", "Temp", "HRV", "Charge"]
    for i in range(10):
        sample, ts = inlet.pull_sample(timeout=5.0)
        if sample is None:
            print("  (timeout waiting for sample — is sensor sampling?)")
            break
        vals = "  ".join(f"{l}={v:.1f}" for l, v in zip(labels, sample))
        print(f"  [{i+1:02d}] t={ts:.3f}   {vals}")
        time.sleep(0.05)
    print("\nDone.")
