"""Tests for audio module — mock sounddevice to avoid hardware dependency."""

from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from soundbridge.audio import (
    find_pulse_monitor,
    list_input_devices,
    list_output_devices,
    AudioPlayback,
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
