"""Tests for network module — mock socket to avoid real network I/O."""

from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from soundbridge import config
from soundbridge import protocol
from soundbridge.network import UDPSender


class TestUDPSender:

    @patch("soundbridge.network.socket.socket")
    def test_send_audio_sends_correct_packet(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        sender = UDPSender("192.168.1.100", config.AUDIO_PORT)
        audio = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)
        sender.send_audio(audio, config.PKT_AUDIO_DATA, config.CHANNELS_STEREO)

        mock_sock.sendto.assert_called_once()
        call_args = mock_sock.sendto.call_args
        sent_data = call_args[0][0]
        dest = call_args[0][1]

        assert dest == ("192.168.1.100", config.AUDIO_PORT)

        # Verify the sent data is a valid SoundBridge packet
        packet = protocol.decode(sent_data)
        assert packet is not None
        assert packet.pkt_type == config.PKT_AUDIO_DATA
        assert packet.channels == config.CHANNELS_STEREO


class TestProtocolPacketTypes:
    """Verify that protocol.encode produces the correct packet types for network operations."""

    def test_discovery_ping_packet_type(self):
        raw = protocol.encode(config.PKT_DISCOVERY_PING, channels=0, sample_rate=0)
        packet = protocol.decode(raw)
        assert packet is not None
        assert packet.pkt_type == config.PKT_DISCOVERY_PING

    def test_discovery_pong_packet_type(self):
        raw = protocol.encode(config.PKT_DISCOVERY_PONG, channels=0, sample_rate=0)
        packet = protocol.decode(raw)
        assert packet is not None
        assert packet.pkt_type == config.PKT_DISCOVERY_PONG

    def test_heartbeat_packet_type(self):
        raw = protocol.encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0)
        packet = protocol.decode(raw)
        assert packet is not None
        assert packet.pkt_type == config.PKT_HEARTBEAT


class TestUDPReceiverDecode:

    def test_decode_called_on_valid_data(self):
        """protocol.decode should correctly parse a valid packet."""
        audio = np.zeros((config.FRAME_SIZE, config.CHANNELS_STEREO), dtype=np.int16)
        raw = protocol.encode(config.PKT_AUDIO_DATA, audio, config.CHANNELS_STEREO)

        packet = protocol.decode(raw)
        assert packet is not None
        assert packet.pkt_type == config.PKT_AUDIO_DATA

    def test_decode_returns_none_on_garbage(self):
        """protocol.decode should return None for invalid data."""
        assert protocol.decode(b"garbage data that is not a packet") is None
