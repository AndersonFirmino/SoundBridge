"""Audio capture and playback for SoundBridge.

Server (Linux): captures system audio via PulseAudio monitor, receives remote mic as virtual source.
Client (Windows): plays received audio to headphone, captures mic and sends it.
"""

import logging
import subprocess
import sys
import threading

import numpy as np
import sounddevice as sd

from . import config

logger = logging.getLogger(__name__)


class AudioCapture:
    """Captures audio from a device (system monitor or mic)."""

    def __init__(self, callback, channels: int = config.CHANNELS_STEREO,
                 device=None):
        """
        Args:
            callback: function(audio_data: np.ndarray) called for each frame.
            channels: number of channels to capture.
            device: sounddevice device index or name. None = default.
        """
        self.callback = callback
        self.channels = channels
        self.device = device
        self._stream: sd.InputStream | None = None

    def start(self):
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            blocksize=config.FRAME_SIZE,
            channels=self.channels,
            dtype=config.SAMPLE_FORMAT,
            device=self.device,
            callback=self._sd_callback,
        )
        self._stream.start()

    def _sd_callback(self, indata, frames, time_info, status):
        if status:
            logger.debug("Audio capture status: %s", status)
        self.callback(indata.copy())

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioPlayback:
    """Plays audio frames received from the network."""

    def __init__(self, channels: int = config.CHANNELS_STEREO, device=None):
        self.channels = channels
        self.device = device
        self._stream: sd.OutputStream | None = None
        self._buffer: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._volume = 1.0
        self._prebuffer_count = 3
        self._prebuffering = True

    def start(self):
        self._stream = sd.OutputStream(
            samplerate=config.SAMPLE_RATE,
            blocksize=config.FRAME_SIZE,
            channels=self.channels,
            dtype=config.SAMPLE_FORMAT,
            device=self.device,
            latency="low",
            callback=self._sd_callback,
        )
        self._stream.start()

    def _sd_callback(self, outdata, frames, time_info, status):
        with self._lock:
            if self._prebuffering:
                if len(self._buffer) >= self._prebuffer_count:
                    self._prebuffering = False
                else:
                    outdata[:] = np.zeros((frames, self.channels), dtype=np.int16)
                    return
            if self._buffer:
                chunk = self._buffer.pop(0)
                if self._volume != 1.0:
                    chunk = (chunk.astype(np.float32) * self._volume).astype(np.int16)
                if chunk.shape[0] < frames:
                    padded = np.zeros((frames, self.channels), dtype=np.int16)
                    padded[:chunk.shape[0]] = chunk.reshape(-1, self.channels)
                    outdata[:] = padded
                else:
                    outdata[:] = chunk[:frames].reshape(-1, self.channels)
            else:
                outdata[:] = np.zeros((frames, self.channels), dtype=np.int16)

    def feed(self, audio_data: np.ndarray):
        """Feed audio data into the playback buffer."""
        with self._lock:
            if len(self._buffer) < 15:
                self._buffer.append(audio_data)

    def set_volume(self, volume: float):
        """Set volume (0.0 to 1.0)."""
        self._volume = max(0.0, min(1.0, volume))

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def find_monitor_source() -> str | None:
    """Find the PulseAudio/PipeWire monitor source name for the default sink.

    Uses pulsectl to query PulseAudio/PipeWire directly, bypassing
    sounddevice/PortAudio limitations with PipeWire.

    Skips the SoundBridge null-sink to avoid capturing its own monitor.
    If the default sink is the null-sink (e.g. after a crash), falls back
    to the first real hardware monitor source.
    """
    try:
        import pulsectl
        with pulsectl.Pulse("soundbridge-discover") as pulse:
            default_sink = pulse.server_info().default_sink_name

            # Skip if default sink is our own null-sink
            if "soundbridge" not in default_sink:
                for source in pulse.source_list():
                    if "monitor" in source.name and default_sink in source.name:
                        return source.name

            # Fallback: find any hardware monitor (not ours)
            for source in pulse.source_list():
                if ("monitor" in source.name
                        and "soundbridge" not in source.name):
                    return source.name
    except Exception:
        pass
    return None


class ParecCapture:
    """Captures audio from a PulseAudio/PipeWire monitor source using parec."""

    def __init__(self, callback, channels: int = config.CHANNELS_STEREO,
                 device_name: str = ""):
        self.callback = callback
        self.channels = channels
        self.device_name = device_name
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        cmd = [
            "parec",
            f"--format=s16le",
            f"--channels={self.channels}",
            f"--rate={config.SAMPLE_RATE}",
            f"--device={self.device_name}",
            "--latency-msec=20",
        ]
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        chunk_bytes = config.FRAME_SIZE * self.channels * config.BYTES_PER_SAMPLE
        while self._running and self._process:
            data = self._process.stdout.read(chunk_bytes)
            if not data:
                break
            audio = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
            self.callback(audio)

    def stop(self):
        self._running = False
        if self._process:
            self._process.terminate()
            self._process.wait()
            self._process = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None


def find_pulse_monitor() -> int | None:
    """Find PulseAudio monitor source device index (Linux only)."""
    try:
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            name = dev["name"].lower()
            if "monitor" in name and dev["max_input_channels"] >= 2:
                return i
    except Exception:
        pass
    return None


def list_input_devices() -> list[dict]:
    """List available input (recording) devices."""
    devices = sd.query_devices()
    result = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            result.append({"index": i, "name": dev["name"],
                           "channels": dev["max_input_channels"]})
    return result


def list_output_devices() -> list[dict]:
    """List available output (playback) devices."""
    devices = sd.query_devices()
    result = []
    for i, dev in enumerate(devices):
        if dev["max_output_channels"] > 0:
            result.append({"index": i, "name": dev["name"],
                           "channels": dev["max_output_channels"]})
    return result


class PacatPlayback:
    """Plays audio into a PulseAudio/PipeWire sink using pacat."""

    def __init__(self, channels: int = config.CHANNELS_MONO,
                 sink_name: str = ""):
        self.channels = channels
        self.sink_name = sink_name
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self):
        cmd = [
            "pacat",
            "--format=s16le",
            f"--channels={self.channels}",
            f"--rate={config.SAMPLE_RATE}",
            f"--device={self.sink_name}",
        ]
        self._process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def feed(self, audio_data: np.ndarray):
        """Write audio data to the sink."""
        with self._lock:
            if self._process and self._process.stdin:
                try:
                    self._process.stdin.write(audio_data.tobytes())
                except (BrokenPipeError, OSError):
                    pass

    def stop(self):
        if self._process:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            self._process.terminate()
            self._process.wait()
            self._process = None


class VirtualMicSource:
    """Creates a virtual PulseAudio source on Linux to pipe remote mic audio.

    Uses pulsectl to load a null-sink module. Audio is written to the sink
    via PacatPlayback, making its monitor appear as a microphone.
    """

    def __init__(self):
        self._module_id: int | None = None
        self._sink_name = "soundbridge_virtual_mic"
        self._pulse = None

    @property
    def sink_name(self) -> str:
        return self._sink_name

    @property
    def active(self) -> bool:
        return self._module_id is not None

    def start(self):
        if sys.platform != "linux":
            return

        try:
            import pulsectl
            self._pulse = pulsectl.Pulse("soundbridge")

            # Remove any leftover null-sink modules from previous runs
            for module in self._pulse.module_list():
                if module.name == "module-null-sink" and self._sink_name in (module.argument or ""):
                    try:
                        self._pulse.module_unload(module.index)
                    except Exception:
                        pass

            # Load a null sink — its monitor becomes our virtual mic
            self._module_id = self._pulse.module_load(
                "module-null-sink",
                f"sink_name={self._sink_name} "
                f"sink_properties=device.description={config.VIRTUAL_SOURCE_DESC} "
                f"rate={config.SAMPLE_RATE} channels={config.CHANNELS_MONO} "
                f"format=s16le"
            )
        except Exception as e:
            logger.error("Failed to create virtual mic source: %s", e)
            self._module_id = None

    def stop(self):
        if self._pulse and self._module_id is not None:
            try:
                self._pulse.module_unload(self._module_id)
            except Exception:
                pass
            self._module_id = None
        if self._pulse:
            self._pulse.close()
            self._pulse = None
