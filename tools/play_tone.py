"""Play a sine tone on the default output device (for testing the audio bridge).

Usage: uv run python tools/play_tone.py [--freq 440] [--seconds 8] [--amp 0.1] [--delay 0]
"""

import argparse
import math
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 48000


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freq", type=float, default=440.0)
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--amp", type=float, default=0.1)
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds to wait before starting")
    args = p.parse_args()
    if not math.isfinite(args.freq) or not 0 < args.freq < SAMPLE_RATE / 2:
        p.error("--freq must be finite and between 0 and 24000 Hz (exclusive)")
    if not math.isfinite(args.seconds) or args.seconds < 1 / SAMPLE_RATE:
        p.error("--seconds must be finite and produce at least one sample")
    if not math.isfinite(args.amp) or not 0 < args.amp <= 1:
        p.error("--amp must be finite and in (0, 1]")
    if not math.isfinite(args.delay) or args.delay < 0:
        p.error("--delay must be finite and nonnegative")

    if args.delay > 0:
        time.sleep(args.delay)

    n = int(args.seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    mono = (args.amp * np.sin(2 * np.pi * args.freq * t)).astype(np.float32)
    data = np.column_stack([mono, mono])
    print(f"Playing {args.freq} Hz, amp {args.amp}, {args.seconds}s")
    sd.play(data, samplerate=SAMPLE_RATE)
    sd.wait()
    print("Done")


if __name__ == "__main__":
    main()
