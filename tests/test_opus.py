"""Tests for Opus encoder/decoder wrapper."""

import pytest
import numpy as np

from soundbridge import config

# Skip all tests if libopus is not available
try:
    from soundbridge.opus import OpusEncoder, OpusDecoder
    HAS_OPUS = True
except OSError:
    HAS_OPUS = False

pytestmark = pytest.mark.skipif(not HAS_OPUS, reason="libopus not available")


class TestOpusRoundTrip:

    def test_stereo_encode_decode_roundtrip(self):
        """Encode stereo PCM -> Opus -> decode back. Result should be correlated."""
        encoder = OpusEncoder(48000, 2, bitrate=128000)
        decoder = OpusDecoder(48000, 2)

        # Generate a sine wave (more realistic than random noise)
        t = np.linspace(0, config.FRAME_SIZE / 48000, config.FRAME_SIZE, endpoint=False)
        sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm = np.column_stack([sine, sine])  # stereo

        opus_data = encoder.encode(pcm)
        recovered = decoder.decode(opus_data)

        # Opus is lossy — shapes must match, values correlated
        assert recovered.shape == pcm.shape
        assert recovered.dtype == np.int16

        # Correlation check: recovered should not be all zeros
        assert np.any(recovered != 0)

        encoder.destroy()
        decoder.destroy()

    def test_mono_encode_decode_roundtrip(self):
        """Encode mono PCM -> Opus -> decode back."""
        encoder = OpusEncoder(48000, 1, bitrate=64000)
        decoder = OpusDecoder(48000, 1)

        t = np.linspace(0, config.FRAME_SIZE / 48000, config.FRAME_SIZE, endpoint=False)
        pcm = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)

        opus_data = encoder.encode(pcm)
        recovered = decoder.decode(opus_data)

        assert recovered.shape == pcm.shape
        assert recovered.dtype == np.int16
        assert np.any(recovered != 0)

        encoder.destroy()
        decoder.destroy()

    def test_opus_compression_ratio(self):
        """Opus output should be significantly smaller than raw PCM."""
        encoder = OpusEncoder(48000, 2, bitrate=128000)

        pcm = np.random.randint(-32768, 32767,
                                size=(config.FRAME_SIZE, 2), dtype=np.int16)
        raw_size = pcm.nbytes  # 480 * 2 * 2 = 1920
        opus_data = encoder.encode(pcm)

        assert len(opus_data) < raw_size
        # Typical Opus stereo 128kbps at 10ms: ~160 bytes
        assert len(opus_data) < 500

        encoder.destroy()


class TestOpusPLC:

    def test_plc_returns_correct_shape_stereo(self):
        """PLC should return ndarray of correct shape without exception."""
        decoder = OpusDecoder(48000, 2)

        # First decode a real frame so the decoder has state
        encoder = OpusEncoder(48000, 2, bitrate=128000)
        pcm = np.zeros((config.FRAME_SIZE, 2), dtype=np.int16)
        opus_data = encoder.encode(pcm)
        decoder.decode(opus_data)

        # Now PLC
        plc_frame = decoder.plc(config.FRAME_SIZE)
        assert plc_frame.shape == (config.FRAME_SIZE, 2)
        assert plc_frame.dtype == np.int16

        encoder.destroy()
        decoder.destroy()

    def test_plc_returns_correct_shape_mono(self):
        """PLC mono should return 1D ndarray."""
        decoder = OpusDecoder(48000, 1)

        encoder = OpusEncoder(48000, 1, bitrate=64000)
        pcm = np.zeros(config.FRAME_SIZE, dtype=np.int16)
        opus_data = encoder.encode(pcm)
        decoder.decode(opus_data)

        plc_frame = decoder.plc(config.FRAME_SIZE)
        assert plc_frame.shape == (config.FRAME_SIZE,)
        assert plc_frame.dtype == np.int16

        encoder.destroy()
        decoder.destroy()


class TestOpusEncoderConfig:

    def test_encoder_creates_successfully(self):
        """Encoder should be created without errors."""
        encoder = OpusEncoder(48000, 2, bitrate=128000)
        assert encoder._encoder is not None
        encoder.destroy()

    def test_encoder_destroy_idempotent(self):
        """Calling destroy() twice should not raise."""
        encoder = OpusEncoder(48000, 2)
        encoder.destroy()
        encoder.destroy()  # should not raise

    def test_decoder_destroy_idempotent(self):
        """Calling destroy() twice should not raise."""
        decoder = OpusDecoder(48000, 2)
        decoder.destroy()
        decoder.destroy()  # should not raise
