"""Tests for protocol encode/decode — pure logic, highest value."""

import struct

import numpy as np
import pytest

from soundbridge import config
from soundbridge.protocol import Packet, encode, decode, payload_to_audio


class TestEncodeDecodeRoundTrip:

    def test_stereo_round_trip(self, stereo_frame):
        """Encode stereo audio and decode back — data must survive."""
        raw = encode(config.PKT_AUDIO_DATA, stereo_frame, config.CHANNELS_STEREO)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_AUDIO_DATA
        assert packet.channels == config.CHANNELS_STEREO
        assert packet.sample_rate == config.SAMPLE_RATE

        recovered = payload_to_audio(packet)
        np.testing.assert_array_equal(recovered, stereo_frame)

    def test_mono_round_trip(self, mono_frame):
        """Encode mono audio and decode back — data must survive."""
        raw = encode(config.PKT_MIC_DATA, mono_frame, config.CHANNELS_MONO)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_MIC_DATA
        assert packet.channels == config.CHANNELS_MONO

        recovered = payload_to_audio(packet)
        np.testing.assert_array_equal(recovered, mono_frame)

    def test_heartbeat_no_payload(self):
        """Heartbeat packets have no audio payload."""
        raw = encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_HEARTBEAT
        assert packet.payload == b""



class TestDecodeRejection:

    def test_invalid_magic(self):
        """Packets with wrong magic bytes must be rejected."""
        raw = encode(config.PKT_AUDIO_DATA, channels=0, sample_rate=0)
        corrupted = b"\x00\x00" + raw[2:]
        assert decode(corrupted) is None

    def test_truncated_header(self):
        """Packets shorter than HEADER_SIZE must be rejected."""
        assert decode(b"\x53\x42\x01") is None
        assert decode(b"") is None

    def test_inconsistent_payload_size(self):
        """Packet declaring more payload than available bytes must be rejected."""
        # Build a valid header that claims 100 bytes of payload, but only attach 10
        header = struct.pack(
            "!2sBBHH",
            config.MAGIC,
            config.PKT_AUDIO_DATA,
            config.CHANNELS_STEREO,
            config.SAMPLE_RATE,
            100,
        )
        raw = header + b"\x00" * 10
        assert decode(raw) is None


class TestPayloadToAudio:

    def test_stereo_reshape(self, stereo_frame):
        """Stereo payload must be reshaped to (N, 2)."""
        raw = encode(config.PKT_AUDIO_DATA, stereo_frame, config.CHANNELS_STEREO)
        packet = decode(raw)
        audio = payload_to_audio(packet)

        assert audio.ndim == 2
        assert audio.shape[1] == config.CHANNELS_STEREO

    def test_mono_no_reshape(self, mono_frame):
        """Mono payload stays 1D."""
        raw = encode(config.PKT_MIC_DATA, mono_frame, config.CHANNELS_MONO)
        packet = decode(raw)
        audio = payload_to_audio(packet)

        assert audio.ndim == 1
