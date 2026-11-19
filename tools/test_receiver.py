"""Measure UDP audio from one server (does not play audio).

Sends heartbeats on UDP 4413, receives UDP 4410 and decodes Opus. Run on
another host, not alongside a client using the same audio port. A loopback
run is only a local smoke test, not a remote playback or reconnect test.

Usage: uv run python tools/test_receiver.py --server-ip <IP> [--seconds 5]
       [--first-packet-timeout 10] [--expect-tone 440]
Exit code 0 = PASS, 1 = FAIL, 2 = invalid arguments.
"""

import argparse
import math
import os
import socket
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from soundbridge import config, protocol  # noqa: E402
from soundbridge.opus import OpusDecoder  # noqa: E402


class SequenceStats:
    """Unwrap 16-bit sequence numbers and count missing packets, not jumps.

    Assumes no interruption/reordering of half the sequence space (32768
    packets). Late packets fill holes; duplicates never inflate throughput.
    """

    def __init__(self):
        self.seen = set()
        self.first = None
        self.highest = None
        self.duplicates = 0
        self.reordered = 0
        self.gap_events = 0

    def add(self, seq):
        if self.highest is None:
            absolute = seq
            self.first = seq
        else:
            delta = (seq - self.highest + 32768) % 65536 - 32768
            absolute = self.highest + delta
        if absolute in self.seen:
            self.duplicates += 1
            return False
        if self.highest is not None:
            if absolute < self.highest:
                self.reordered += 1
            elif absolute > self.highest + 1:
                self.gap_events += 1
        self.seen.add(absolute)
        self.first = min(self.first, absolute)
        self.highest = max(self.highest if self.highest is not None else absolute,
                           absolute)
        return True

    @property
    def missing(self):
        if self.highest is None:
            return 0
        return self.highest - self.first + 1 - len(self.seen)


def heartbeat_loop(server_ip, stop):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        pkt = protocol.encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0)
        reported = False
        while not stop.is_set():
            try:
                sock.sendto(pkt, (server_ip, config.HEARTBEAT_PORT))
            except OSError as e:
                if not reported:
                    print(f"HEARTBEAT SEND ERROR: errno={e.errno} {e!r}")
                    print("Check routing, host availability and network permissions; "
                          "this error alone does not identify the cause.")
                    reported = True
            stop.wait(config.HEARTBEAT_INTERVAL)


def tone_ratio(x, freq):
    """Peak within +/-3 Hz / mean magnitude 10-40 Hz away."""
    if not math.isfinite(freq) or not 0 < freq < config.SAMPLE_RATE / 2:
        raise ValueError("tone frequency must be finite and between 0 and Nyquist")
    if len(x) < config.SAMPLE_RATE / 3:
        raise ValueError("insufficient audio for the +/-3 Hz tone measurement")
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / config.SAMPLE_RATE)
    peak_bins = (freqs >= freq - 3) & (freqs <= freq + 3)
    side = ((freqs >= freq - 40) & (freqs <= freq - 10)) | \
           ((freqs >= freq + 10) & (freqs <= freq + 40))
    if not peak_bins.any() or not side.any():
        raise ValueError("insufficient spectral bins for the requested tone")
    return spec[peak_bins].max() / max(spec[side].mean(), 1e-9)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server-ip", default="127.0.0.1")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--first-packet-timeout", type=float, default=10.0)
    p.add_argument("--expect-tone", type=float, default=None)
    args = p.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 1:
        p.error("--seconds must be finite and at least 1 second")
    if not math.isfinite(args.first_packet_timeout) or args.first_packet_timeout <= 0:
        p.error("--first-packet-timeout must be finite and positive")
    if args.expect_tone is not None and (
            not math.isfinite(args.expect_tone)
            or not 0 < args.expect_tone < config.SAMPLE_RATE / 2):
        p.error("--expect-tone must be finite and between 0 and 24000 Hz (exclusive)")
    try:
        server_ip = socket.gethostbyname(args.server_ip)
    except OSError as exc:
        p.error(f"cannot resolve --server-ip: {exc}")

    stop = threading.Event()
    worker = threading.Thread(target=heartbeat_loop, args=(server_ip, stop), daemon=True)
    dec = OpusDecoder(config.SAMPLE_RATE, config.CHANNELS_STEREO)
    frames = []
    stats = SequenceStats()
    first = None
    ignored = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", config.AUDIO_PORT))
            sock.settimeout(0.1)
            worker.start()
            deadline = time.monotonic() + args.first_packet_timeout
            while True:
                now = time.monotonic()
                if (first is None and now >= deadline) or (
                        first is not None and now - first >= args.seconds):
                    break
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                if addr[0] != server_ip:
                    ignored += 1
                    continue
                pkt = protocol.decode(data)
                if pkt is None or pkt.pkt_type != config.PKT_AUDIO_DATA:
                    continue
                if not stats.add(pkt.seq):
                    continue
                now = time.monotonic()
                if first is None:
                    first = now
                frames.append((now - first, dec.decode(pkt.payload)))
    finally:
        stop.set()
        if worker.ident is not None:
            worker.join(timeout=2)
        dec.destroy()

    if first is None:
        print(f"NO PACKETS from {server_ip}; ignored_other_sources={ignored}")
        return 1
    elapsed = time.monotonic() - first
    packets = len(stats.seen)
    pps = packets / elapsed
    selected = [f for t, f in frames if t >= 0.5]
    if not selected:
        selected = [f for _, f in frames]
    pcm = np.concatenate(selected).reshape(-1, config.CHANNELS_STEREO)
    left = pcm[:, 0].astype(np.float64) / 32768.0
    peak = float(np.abs(pcm.astype(np.int32)).max())
    spec = np.abs(np.fft.rfft(left * np.hanning(len(left))))
    dom = np.fft.rfftfreq(len(left), 1.0 / config.SAMPLE_RATE)[int(np.argmax(spec[1:])) + 1]
    expected = packets + stats.missing
    loss = stats.missing / expected
    print(f"packets={packets} pps={pps:.1f} missing={stats.missing} "
          f"loss={loss:.2%} gap_events={stats.gap_events} duplicates={stats.duplicates} "
          f"reordered={stats.reordered} ignored_other_sources={ignored} "
          f"peak={peak:.0f} dominant={dom:.1f}Hz")
    reasons = []
    if pps < 80:
        reasons.append(f"pps {pps:.1f} < 80")
    if loss > 0.01:
        reasons.append(f"missing {stats.missing} > 1% of {expected}")
    if len(left) < config.SAMPLE_RATE / 3:
        reasons.append("insufficient audio for a reliable measurement")
    if args.expect_tone is not None:
        try:
            ratio = tone_ratio(left, args.expect_tone)
            print(f"tone {args.expect_tone} Hz ratio={ratio:.1f}")
            if ratio < 20:
                reasons.append(f"tone ratio {ratio:.1f} < 20")
        except ValueError as exc:
            reasons.append(str(exc))
    print("FAIL: " + "; ".join(reasons) if reasons else "PASS")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
