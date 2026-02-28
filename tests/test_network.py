"""Tests for network module — mock socket/zeroconf to avoid real network I/O."""

from unittest.mock import patch, MagicMock, call

import numpy as np
import pytest

from soundbridge import config
from soundbridge import protocol
from soundbridge.network import UDPSender, Discovery, Heartbeat


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


class TestDiscoveryServer:

    @patch("soundbridge.network.Zeroconf")
    @patch("soundbridge.network._get_local_ip", return_value="192.168.1.10")
    def test_start_listen_registers_service(self, mock_ip, mock_zc_cls):
        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_listen()

        mock_zc.register_service.assert_called_once()
        info = mock_zc.register_service.call_args[0][0]
        assert info.type == config.ZEROCONF_SERVICE_TYPE
        assert info.name == config.ZEROCONF_SERVICE_NAME

    @patch("soundbridge.network.Zeroconf")
    @patch("soundbridge.network._get_local_ip", return_value="192.168.1.10")
    def test_stop_unregisters_and_closes(self, mock_ip, mock_zc_cls):
        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_listen()
        discovery.stop()

        mock_zc.unregister_service.assert_called_once()
        mock_zc.close.assert_called_once()


class TestDiscoveryClient:

    @patch("soundbridge.network.ServiceBrowser")
    @patch("soundbridge.network.Zeroconf")
    def test_start_search_creates_browser(self, mock_zc_cls, mock_browser_cls):
        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_search()

        mock_browser_cls.assert_called_once()
        args = mock_browser_cls.call_args
        assert args[0][1] == config.ZEROCONF_SERVICE_TYPE

    @patch("soundbridge.network.ServiceBrowser")
    @patch("soundbridge.network.Zeroconf")
    def test_on_state_change_calls_peer_found(self, mock_zc_cls, mock_browser_cls):
        from zeroconf import ServiceStateChange

        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc

        mock_info = MagicMock()
        mock_info.parsed_addresses.return_value = ["192.168.1.10"]
        mock_zc.get_service_info.return_value = mock_info

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_search()

        # Simulate the ServiceBrowser calling the handler
        handler = mock_browser_cls.call_args[1]["handlers"][0]
        handler(mock_zc, config.ZEROCONF_SERVICE_TYPE,
                config.ZEROCONF_SERVICE_NAME, ServiceStateChange.Added)

        callback.assert_called_once_with("192.168.1.10")

    @patch("soundbridge.network.ServiceBrowser")
    @patch("soundbridge.network.Zeroconf")
    def test_on_state_change_ignores_removed(self, mock_zc_cls, mock_browser_cls):
        from zeroconf import ServiceStateChange

        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_search()

        handler = mock_browser_cls.call_args[1]["handlers"][0]
        handler(mock_zc, config.ZEROCONF_SERVICE_TYPE,
                config.ZEROCONF_SERVICE_NAME, ServiceStateChange.Removed)

        callback.assert_not_called()

    @patch("soundbridge.network.ServiceBrowser")
    @patch("soundbridge.network.Zeroconf")
    def test_stop_cancels_browser_and_closes(self, mock_zc_cls, mock_browser_cls):
        mock_zc = MagicMock()
        mock_zc_cls.return_value = mock_zc
        mock_browser = MagicMock()
        mock_browser_cls.return_value = mock_browser

        callback = MagicMock()
        discovery = Discovery(on_peer_found=callback)
        discovery.start_search()
        discovery.stop()

        mock_browser.cancel.assert_called_once()
        mock_zc.close.assert_called_once()


class TestHeartbeat:

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
