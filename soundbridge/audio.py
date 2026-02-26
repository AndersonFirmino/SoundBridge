"""Audio capture and playback for SoundBridge.

Server (Linux): captures system audio via PulseAudio monitor, receives remote mic as virtual source.
Client (Windows): plays received audio to headphone, captures mic and sends it.
"""

import logging
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

    def start(self):
        self._stream = sd.OutputStream(
            samplerate=config.SAMPLE_RATE,
            blocksize=config.FRAME_SIZE,
            channels=self.channels,
            dtype=config.SAMPLE_FORMAT,
            device=self.device,
            callback=self._sd_callback,
        )
        self._stream.start()

    def _sd_callback(self, outdata, frames, time_info, status):
        with self._lock:
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
            # Keep buffer small to minimize latency (max ~100ms)
            if len(self._buffer) < 5:
                self._buffer.append(audio_data)

    def set_volume(self, volume: float):
        """Set volume (0.0 to 1.0)."""
        self._volume = max(0.0, min(1.0, volume))

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


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


class VirtualMicSource:
    """Creates a virtual PulseAudio source on Linux to pipe remote mic audio.

    Uses pulsectl to load a null-sink module and writes audio to it,
    making it appear as a microphone in applications like Google Meet.
    """

    def __init__(self):
        self._module_id: int | None = None
        self._sink_name = "soundbridge_virtual_mic"
        self._pulse = None

    def start(self):
        if sys.platform != "linux":
            return

        try:
            import pulsectl
            self._pulse = pulsectl.Pulse("soundbridge")

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

    def write_audio(self, audio_data: np.ndarray):
        """Write mic audio to the virtual source.

        This feeds audio into the null-sink so its monitor appears as a mic.
        Since PulseAudio null-sink doesn't have a direct write API,
        we use sounddevice to output to the sink.
        """
        # Audio is written via a separate AudioPlayback targeting the null-sink
        pass

    def get_sink_device_index(self) -> int | None:
        """Get the sounddevice output device index for the null-sink."""
        try:
            devices = sd.query_devices()
            for i, dev in enumerate(devices):
                if self._sink_name in dev["name"] and dev["max_output_channels"] > 0:
                    return i
        except Exception:
            pass
        return None

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
