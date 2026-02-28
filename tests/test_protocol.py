"""Tests for protocol encode/decode — pure logic, highest value."""

import struct

import numpy as np
import pytest

from soundbridge import config
from soundbridge.protocol import Packet, encode, decode


class TestEncodeDecodeRoundTrip:

    def test_stereo_round_trip(self, stereo_payload):
        """Encode stereo payload and decode back — data must survive."""
        raw = encode(config.PKT_AUDIO_DATA, stereo_payload,
                     config.CHANNELS_STEREO, seq=42)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_AUDIO_DATA
        assert packet.channels == config.CHANNELS_STEREO
        assert packet.sample_rate == config.SAMPLE_RATE
        assert packet.seq == 42
        assert packet.payload == stereo_payload

    def test_mono_round_trip(self, mono_payload):
        """Encode mono payload and decode back — data must survive."""
        raw = encode(config.PKT_MIC_DATA, mono_payload,
                     config.CHANNELS_MONO, seq=100)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_MIC_DATA
        assert packet.channels == config.CHANNELS_MONO
        assert packet.seq == 100
        assert packet.payload == mono_payload

    def test_heartbeat_no_payload(self):
        """Heartbeat packets have no audio payload."""
        raw = encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0)
        packet = decode(raw)

        assert packet is not None
        assert packet.pkt_type == config.PKT_HEARTBEAT
        assert packet.seq == 0
        assert packet.payload == b""

    def test_seq_wraps_at_65535(self):
        """Sequence number uses full uint16 range."""
        raw = encode(config.PKT_AUDIO_DATA, b"\x00\x01", seq=65535)
        packet = decode(raw)
        assert packet is not None
        assert packet.seq == 65535

    def test_seq_default_zero(self):
        """Default seq is 0."""
        raw = encode(config.PKT_AUDIO_DATA, b"test")
        packet = decode(raw)
        assert packet is not None
        assert packet.seq == 0


class TestHeaderFormat:

    def test_header_size_is_10(self):
        """Header must be 10 bytes."""
        assert config.HEADER_SIZE == 10

    def test_struct_format(self):
        """Struct format !2sBBHHH produces 10 bytes."""
        size = struct.calcsize("!2sBBHHH")
        assert size == config.HEADER_SIZE

    def test_header_fields_order(self):
        """Verify header field order: magic, type, channels, sample_rate, seq, payload_size."""
        raw = encode(config.PKT_AUDIO_DATA, b"\xAA\xBB",
                     channels=2, sample_rate=48000, seq=1234)
        magic, pkt_type, channels, sr, seq, ps = struct.unpack(
            "!2sBBHHH", raw[:10]
        )
        assert magic == config.MAGIC
        assert pkt_type == config.PKT_AUDIO_DATA
        assert channels == 2
        assert sr == 48000
        assert seq == 1234
        assert ps == 2


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
        header = struct.pack(
            "!2sBBHHH",
            config.MAGIC,
            config.PKT_AUDIO_DATA,
            config.CHANNELS_STEREO,
            config.SAMPLE_RATE,
            0,   # seq
            100,  # payload_size
        )
        raw = header + b"\x00" * 10
        assert decode(raw) is None
