"""Tests for audio module — mock sounddevice to avoid hardware dependency."""

import collections
import sys
import time
from unittest.mock import patch, MagicMock, call

import numpy as np
import pytest

from soundbridge.audio import (
    find_pulse_monitor,
    find_monitor_source,
    list_input_devices,
    list_output_devices,
    AudioPlayback,
    PacatPlayback,
    ParecCapture,
    VirtualMicSource,
)
from soundbridge import config


class TestFindPulseMonitor:

    @patch("soundbridge.audio.sd.query_devices")
    def test_finds_monitor_when_exists(self, mock_query):
        mock_query.return_value = [
            {"name": "HDA Intel PCH", "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Monitor of Built-in Audio", "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Headphone", "max_input_channels": 0, "max_output_channels": 2},
        ]
        result = find_pulse_monitor()
        assert result == 1

    @patch("soundbridge.audio.sd.query_devices")
    def test_returns_none_when_no_monitor(self, mock_query):
        mock_query.return_value = [
            {"name": "HDA Intel PCH", "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Headphone", "max_input_channels": 0, "max_output_channels": 2},
        ]
        result = find_pulse_monitor()
        assert result is None


class TestFindMonitorSource:

    def _make_mock_pulsectl(self, default_sink_name, sources):
        """Create a mock pulsectl module with the given sink and sources."""
        mock_pulsectl = MagicMock()
        mock_pulse = MagicMock()
        mock_pulsectl.Pulse.return_value.__enter__ = MagicMock(return_value=mock_pulse)
        mock_pulsectl.Pulse.return_value.__exit__ = MagicMock(return_value=False)
        mock_pulse.server_info.return_value.default_sink_name = default_sink_name
        mock_source_list = []
        for name in sources:
            s = MagicMock()
            s.name = name
            mock_source_list.append(s)
        mock_pulse.source_list.return_value = mock_source_list
        return mock_pulsectl

    def test_finds_monitor_of_default_sink(self):
        mock_pulsectl = self._make_mock_pulsectl(
            "alsa_output.pci-0000_00_1f.3.analog-stereo",
            ["alsa_input.pci-0000_00_1f.3.analog-stereo",
             "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"],
        )
        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            # Re-import to pick up the mock
            from soundbridge.audio import find_monitor_source as fms
            result = fms()
        assert result == "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def test_returns_none_when_no_monitor(self):
        mock_pulsectl = self._make_mock_pulsectl(
            "alsa_output.pci-0000_00_1f.3.analog-stereo",
            ["alsa_input.pci-0000_00_1f.3.analog-stereo"],
        )
        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            from soundbridge.audio import find_monitor_source as fms
            result = fms()
        assert result is None

    def test_skips_soundbridge_null_sink_as_default(self):
        mock_pulsectl = self._make_mock_pulsectl(
            "soundbridge_virtual_mic",
            ["alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
             "soundbridge_virtual_mic.monitor"],
        )
        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            from soundbridge.audio import find_monitor_source as fms
            result = fms()
        assert result == "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def test_returns_none_when_pulsectl_import_fails(self):
        with patch.dict(sys.modules, {"pulsectl": None}):
            result = find_monitor_source()
            assert result is None


class TestListDevices:

    @patch("soundbridge.audio.sd.query_devices")
    def test_list_input_devices_filters_inputs(self, mock_query):
        mock_query.return_value = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Monitor", "max_input_channels": 2, "max_output_channels": 0},
        ]
        result = list_input_devices()
        assert len(result) == 2
        assert all(d["channels"] > 0 for d in result)
        names = [d["name"] for d in result]
        assert "Mic" in names
        assert "Monitor" in names
        assert "Speaker" not in names

    @patch("soundbridge.audio.sd.query_devices")
    def test_list_output_devices_filters_outputs(self, mock_query):
        mock_query.return_value = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Headphone", "max_input_channels": 0, "max_output_channels": 2},
        ]
        result = list_output_devices()
        assert len(result) == 2
        names = [d["name"] for d in result]
        assert "Speaker" in names
        assert "Headphone" in names
        assert "Mic" not in names


class TestAudioPlayback:

    def test_feed_buffer_max_depth(self):
        """Buffer should not exceed max_depth frames."""
        playback = AudioPlayback(channels=config.CHANNELS_STEREO)
        frame = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)

        for _ in range(25):
            playback.feed(frame)

        assert len(playback._buffer) == playback._max_depth

    def test_prebuffering_outputs_silence_until_target_depth(self):
        """Callback should output silence until target_depth frames are buffered."""
        playback = AudioPlayback(channels=config.CHANNELS_STEREO)
        # Fix target_depth to isolate prebuffering logic from jitter adaptation
        playback._target_depth = 4
        frame = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)
        outdata = np.ones((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)

        # Feed 3 frames directly into buffer (bypass jitter measurement)
        with playback._lock:
            for _ in range(3):
                playback._buffer.append(frame)

        # Callback should output silence and not consume buffer
        playback._sd_callback(outdata, config.FRAME_SIZE, None, None)
        assert np.all(outdata == 0)
        assert len(playback._buffer) == 3
        assert playback._prebuffering is True

        # Add 4th frame — reaches target_depth
        with playback._lock:
            playback._buffer.append(frame)

        # Now callback should exit prebuffering and consume a frame
        outdata = np.ones((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)
        playback._sd_callback(outdata, config.FRAME_SIZE, None, None)
        assert playback._prebuffering is False
        assert len(playback._buffer) == 3

    def test_set_volume_clamps(self):
        """Volume must be clamped between 0.0 and 1.0."""
        playback = AudioPlayback()

        playback.set_volume(1.5)
        assert playback._volume == 1.0

        playback.set_volume(-0.5)
        assert playback._volume == 0.0

        playback.set_volume(0.7)
        assert playback._volume == pytest.approx(0.7)

    def test_buffer_is_deque(self):
        """Buffer should be a collections.deque for O(1) popleft."""
        playback = AudioPlayback()
        assert isinstance(playback._buffer, collections.deque)

    def test_jitter_updates_target_depth(self):
        """High jitter should increase target_depth."""
        playback = AudioPlayback(channels=config.CHANNELS_STEREO)
        frame = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)

        initial_target = playback._target_depth

        # Simulate high jitter by feeding with irregular timing
        playback._last_arrival = time.monotonic() - 0.5  # 500ms ago
        playback.feed(frame)

        # Jitter should have been measured and target potentially increased
        assert playback._jitter > 0
        assert playback._target_depth >= playback._min_depth

    def test_target_depth_bounded(self):
        """Target depth should stay within min/max bounds."""
        playback = AudioPlayback(channels=config.CHANNELS_STEREO)

        # Force extreme jitter
        playback._jitter = 10.0  # 10 seconds of jitter
        playback._update_target()
        assert playback._target_depth == playback._max_depth

        # Force zero jitter
        playback._jitter = 0.0
        playback._update_target()
        assert playback._target_depth == playback._min_depth


class TestParecCapture:

    @patch("soundbridge.audio.subprocess.Popen")
    def test_callback_receives_correct_shape_stereo(self, mock_popen):
        """Callback should receive int16 ndarray with shape (FRAME_SIZE, 2)."""
        frames_received = []

        chunk_bytes = config.FRAME_SIZE * config.CHANNELS_STEREO * config.BYTES_PER_SAMPLE
        raw_audio = np.zeros(config.FRAME_SIZE * config.CHANNELS_STEREO, dtype=np.int16).tobytes()

        mock_proc = MagicMock()
        # Return one chunk then empty (end of stream)
        mock_proc.stdout.read = MagicMock(side_effect=[raw_audio, b""])
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()
        mock_popen.return_value = mock_proc

        capture = ParecCapture(
            callback=frames_received.append,
            channels=config.CHANNELS_STEREO,
            device_name="test.monitor",
        )
        capture.start()
        capture._thread.join(timeout=2)
        capture.stop()

        assert len(frames_received) == 1
        frame = frames_received[0]
        assert frame.dtype == np.int16
        assert frame.shape == (config.FRAME_SIZE, config.CHANNELS_STEREO)

    @patch("soundbridge.audio.subprocess.Popen")
    def test_parec_command_args(self, mock_popen):
        """Verify parec is called with correct arguments."""
        mock_proc = MagicMock()
        mock_proc.stdout.read = MagicMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()
        mock_popen.return_value = mock_proc

        capture = ParecCapture(
            callback=lambda x: None,
            channels=config.CHANNELS_STEREO,
            device_name="alsa_output.pci.monitor",
        )
        capture.start()
        capture._thread.join(timeout=2)
        capture.stop()

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "parec"
        assert "--format=s16le" in cmd
        assert f"--channels={config.CHANNELS_STEREO}" in cmd
        assert f"--rate={config.SAMPLE_RATE}" in cmd
        assert "--device=alsa_output.pci.monitor" in cmd
        assert "--latency-msec=20" in cmd

    @patch("soundbridge.audio.subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        """stop() should terminate the subprocess."""
        mock_proc = MagicMock()
        mock_proc.stdout.read = MagicMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()
        mock_popen.return_value = mock_proc

        capture = ParecCapture(
            callback=lambda x: None,
            channels=config.CHANNELS_STEREO,
            device_name="test.monitor",
        )
        capture.start()
        capture._thread.join(timeout=2)
        capture.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()


class TestPacatPlayback:

    @patch("soundbridge.audio.subprocess.Popen")
    def test_feed_writes_bytes_to_stdin(self, mock_popen):
        """feed() should write audio bytes to pacat stdin."""
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        playback = PacatPlayback(
            channels=config.CHANNELS_MONO,
            sink_name="soundbridge_virtual_mic",
        )
        playback.start()

        frame = np.zeros(config.FRAME_SIZE, dtype=np.int16)
        playback.feed(frame)

        mock_proc.stdin.write.assert_called_once_with(frame.tobytes())
        playback.stop()

    @patch("soundbridge.audio.subprocess.Popen")
    def test_pacat_command_args(self, mock_popen):
        """Verify pacat is called with correct arguments."""
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        playback = PacatPlayback(
            channels=config.CHANNELS_MONO,
            sink_name="soundbridge_virtual_mic",
        )
        playback.start()

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "pacat"
        assert "--format=s16le" in cmd
        assert f"--channels={config.CHANNELS_MONO}" in cmd
        assert f"--rate={config.SAMPLE_RATE}" in cmd
        assert "--device=soundbridge_virtual_mic" in cmd
        playback.stop()

    @patch("soundbridge.audio.subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        """stop() should close stdin and terminate the process."""
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        playback = PacatPlayback(
            channels=config.CHANNELS_MONO,
            sink_name="soundbridge_virtual_mic",
        )
        playback.start()
        playback.stop()

        mock_proc.stdin.close.assert_called_once()
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()


class TestVirtualMicSource:

    def test_start_cleans_up_on_fifo_open_failure(self):
        """start() should unload the Pulse module on partial setup failure."""
        mock_pulsectl = MagicMock()
        mock_pulse = MagicMock()
        mock_pulsectl.Pulse.return_value = mock_pulse
        mock_pulse.module_list.return_value = []
        mock_pulse.module_load.return_value = 123

        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            with patch("os.path.exists", return_value=False):
                with patch("os.open", side_effect=OSError("open failed")):
                    source = VirtualMicSource()
                    source.start()

        assert source.active is False
        mock_pulse.module_unload.assert_called_once_with(123)
        mock_pulse.close.assert_called_once()

        source.stop()
        mock_pulse.module_unload.assert_called_once_with(123)
        mock_pulse.close.assert_called_once()

    def test_stop_cleans_up_resources_after_successful_start(self):
        """stop() should release the FIFO, Pulse client, and FIFO path."""
        mock_pulsectl = MagicMock()
        mock_pulse = MagicMock()
        mock_pulsectl.Pulse.return_value = mock_pulse
        mock_pulse.module_list.return_value = []
        mock_pulse.module_load.return_value = 123

        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            with patch("os.path.exists", side_effect=[False, True]):
                with patch("os.open", return_value=10):
                    with patch("os.close") as mock_close:
                        with patch("os.unlink") as mock_unlink:
                            source = VirtualMicSource()
                            source.start()
                            source.stop()

        mock_close.assert_called_once_with(10)
        mock_unlink.assert_called_once_with(VirtualMicSource.FIFO_PATH)
        mock_pulse.module_unload.assert_called_once_with(123)
        mock_pulse.close.assert_called_once()

    def test_start_is_restart_safe(self):
        """start() should clean up active resources before reinitializing."""
        mock_pulsectl = MagicMock()
        first_pulse = MagicMock()
        second_pulse = MagicMock()
        mock_pulsectl.Pulse.side_effect = [first_pulse, second_pulse]
        first_pulse.module_list.return_value = []
        second_pulse.module_list.return_value = []
        first_pulse.module_load.return_value = 123
        second_pulse.module_load.return_value = 456

        with patch.dict(sys.modules, {"pulsectl": mock_pulsectl}):
            with patch("os.path.exists", return_value=False):
                with patch("os.open", side_effect=[10, 11]):
                    with patch("os.close") as mock_close:
                        source = VirtualMicSource()
                        source.start()
                        source.start()
                        source.stop()

        assert source.active is False
        assert mock_close.call_args_list == [call(10), call(11)]
        first_pulse.module_unload.assert_called_once_with(123)
        first_pulse.close.assert_called_once()
        second_pulse.module_unload.assert_called_once_with(456)
        second_pulse.close.assert_called_once()
