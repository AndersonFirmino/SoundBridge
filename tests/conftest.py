"""Shared fixtures for SoundBridge tests."""

import numpy as np
import pytest

from soundbridge import config


@pytest.fixture
def stereo_frame():
    """960 samples, 2 channels of int16 audio."""
    return np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16
    )


@pytest.fixture
def mono_frame():
    """960 samples, 1 channel of int16 audio."""
    return np.random.randint(
        -32768, 32767, size=(config.FRAME_SIZE,), dtype=np.int16
    )
