"""Shared fixtures for SoundBridge tests."""

import numpy as np
import pytest

from soundbridge import config


@pytest.fixture
def stereo_frame():
    """480 samples, 2 channels of int16 audio."""
    return np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16
    )


@pytest.fixture
def mono_frame():
    """480 samples, 1 channel of int16 audio."""
    return np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE,), dtype=np.int16
    )


@pytest.fixture
def stereo_payload():
    """PCM stereo frame as raw bytes (Opus-like test payload)."""
    frame = np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16
    )
    return frame.tobytes()


@pytest.fixture
def mono_payload():
    """PCM mono frame as raw bytes (Opus-like test payload)."""
    frame = np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE,), dtype=np.int16
    )
    return frame.tobytes()
