"""Tests for audio module — mock sounddevice to avoid hardware dependency."""

import sys
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from soundbridge.audio import (
    find_pulse_monitor,
    find_monitor_source,
    list_input_devices,
    list_output_devices,
    AudioPlayback,
    ParecCapture,
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

    def test_feed_buffer_max_five_frames(self):
        """Buffer should not exceed 5 frames to keep latency low."""
        playback = AudioPlayback(channels=config.CHANNELS_STEREO)
        frame = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)

        for _ in range(10):
            playback.feed(frame)

        assert len(playback._buffer) == 5

    def test_set_volume_clamps(self):
        """Volume must be clamped between 0.0 and 1.0."""
        playback = AudioPlayback()

        playback.set_volume(1.5)
        assert playback._volume == 1.0

        playback.set_volume(-0.5)
        assert playback._volume == 0.0

        playback.set_volume(0.7)
        assert playback._volume == pytest.approx(0.7)


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
