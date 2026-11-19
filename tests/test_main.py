"""Tests for soundbridge.main with all collaborators mocked."""

import threading
from unittest.mock import patch, MagicMock

import pytest

from soundbridge import config
from soundbridge.state import ConnectionState
from soundbridge.main import (
    SoundBridgeClient, SoundBridgeServer, _stream_callback, _detach_stream_resources,
)


CLIENT_PATCHES = ["Heartbeat", "OpusDecoder", "OpusEncoder", "AudioPlayback",
                  "UDPReceiver", "UDPSender", "AudioCapture"]


def _patch_client():
    return {n: patch(f"soundbridge.main.{n}") for n in CLIENT_PATCHES}


class TestClientMic:

    def test_no_mic_skips_capture_and_sender(self):
        ps = _patch_client()
        m = {n: p.start() for n, p in ps.items()}
        try:
            client = SoundBridgeClient(server_ip="192.0.2.1", mic_enabled=False)
            client._start_streaming()
            m["AudioCapture"].assert_not_called()
            for call in m["UDPSender"].call_args_list:
                assert config.MIC_PORT not in call.args
            assert client._mic_capture is None
            assert client._mic_sender is None
            assert client._audio_playback is not None
        finally:
            for p in ps.values():
                p.stop()

    def test_mic_failure_keeps_client_running(self):
        ps = _patch_client()
        m = {n: p.start() for n, p in ps.items()}
        try:
            m["AudioCapture"].return_value.start.side_effect = RuntimeError("no mic")
            client = SoundBridgeClient(server_ip="192.0.2.1")
            client._start_streaming()
            assert client._audio_playback is not None
            assert client._mic_capture is None
            assert client._mic_sender is None
            m["UDPSender"].return_value.close.assert_called()
            m["AudioCapture"].return_value.stop.assert_called_once()
        finally:
            for p in ps.values():
                p.stop()


class TestBridgeDiscoveryLifecycle:

    @patch("soundbridge.main.Discovery")
    def test_direct_client_validates_endpoint_without_mdns_and_ignores_callback_after_stop(self, discovery_cls):
        client = SoundBridgeClient(server_ip="192.0.2.10", mic_enabled=False)
        client.start()
        discovery_cls.return_value.start_search.assert_called_once_with(
            direct_ip="192.0.2.10", browse=False,
        )
        assert not client.connected
        client.stop()
        with patch.object(client, "_start_streaming") as stream:
            client._on_server_found("198.51.100.10")
            stream.assert_not_called()


    @pytest.mark.parametrize("bridge_cls", [SoundBridgeClient, SoundBridgeServer])
    @patch("soundbridge.main.Discovery")
    def test_callbacks_and_timeouts_cannot_cross_restart(self, discovery_cls, bridge_cls):
        bridge = bridge_cls()
        with patch.object(bridge, "_start_streaming") as stream:
            bridge.start()
            old_callback = discovery_cls.call_args.kwargs["on_peer_found"]
            old_generation = bridge._search_generation
            bridge.stop()
            bridge.start()
            callback = discovery_cls.call_args.kwargs["on_peer_found"]
            old_callback("192.0.2.10")
            assert not bridge.connected
            stream.assert_not_called()
            callback("198.51.100.10")
            endpoint = bridge.server_ip if isinstance(bridge, SoundBridgeClient) else bridge.peer_ip
            assert bridge.connected and endpoint == "198.51.100.10"
            stream.assert_called_once()
            searches = discovery_cls.call_count
            bridge._on_disconnect(old_generation)
            assert discovery_cls.call_count == searches
            assert bridge.connected
            bridge.stop()


    @pytest.mark.parametrize("bridge_cls", [SoundBridgeClient, SoundBridgeServer])
    def test_stop_during_discovery_construction_cannot_publish_or_start_it(self, bridge_cls):
        bridge = bridge_cls()
        entered, release = threading.Event(), threading.Event()
        fresh = MagicMock()
        def construct(**kwargs):
            entered.set()
            release.wait()
            return fresh
        worker = threading.Thread(target=bridge.start, daemon=True)
        with patch("soundbridge.main.Discovery", side_effect=construct):
            try:
                worker.start()
                assert entered.wait(1)
                bridge.stop()
                assert bridge._discovery is None
                release.set()
                worker.join(timeout=1)
                assert not worker.is_alive()
                assert bridge._state == ConnectionState.DISCONNECTED
                assert bridge._discovery is None
                fresh.stop.assert_called_once()
                fresh.start_search.assert_not_called()
                fresh.start_listen.assert_not_called()
            finally:
                release.set()
                worker.join(timeout=1)
                bridge.stop()


class TestStreamGenerationCleanup:

    @pytest.mark.parametrize("bridge_cls, audio_attr", [
        (SoundBridgeClient, "_audio_receiver"), (SoundBridgeServer, "_audio_capture"),
    ])
    @patch("soundbridge.main.Discovery")
    def test_invalidation_and_snapshot_are_atomic_before_restart(self, discovery_cls, bridge_cls, audio_attr):
        bridge = bridge_cls()
        bridge._search_generation = bridge._connected_generation = bridge._stream_generation = 1
        bridge.connected = True
        old_audio, old_hb, new_audio, new_hb = [MagicMock() for _ in range(4)]
        setattr(bridge, audio_attr, old_audio)
        bridge._heartbeat = old_hb
        capturing, allow_capture = threading.Event(), threading.Event()
        stopping, allow_stop, restarted = threading.Event(), threading.Event(), threading.Event()
        first = [True]
        def detach(owner, generation=None):
            if first[0]:
                first[0] = False
                capturing.set()
                allow_capture.wait()
            return _detach_stream_resources(owner, generation)
        def stop_audio():
            stopping.set()
            allow_stop.wait()
        old_audio.stop.side_effect = stop_audio
        def open_new():
            setattr(bridge, audio_attr, new_audio)
            bridge._heartbeat = new_hb
        def restart():
            bridge.start()
            discovery_cls.call_args.kwargs["on_peer_found"]("198.51.100.10")
            restarted.set()
        old = threading.Thread(target=bridge._on_disconnect, args=(1,), daemon=True)
        new = threading.Thread(target=restart, daemon=True)
        with patch("soundbridge.main._detach_stream_resources", side_effect=detach), \
                patch.object(bridge, "_open_streams", side_effect=open_new):
            try:
                old.start()
                assert capturing.wait(1)
                new.start()
                assert not restarted.wait(0.05)  # Snapshot still owns both locks.
                allow_capture.set()
                assert stopping.wait(1)
                assert restarted.wait(1)
                allow_stop.set()
                old.join(timeout=1)
                new.join(timeout=1)
                assert not old.is_alive() and not new.is_alive()
                assert bridge.connected
                assert getattr(bridge, audio_attr) is new_audio and bridge._heartbeat is new_hb
                old_audio.stop.assert_called_once()
                old_hb.stop.assert_called_once()
                new_audio.stop.assert_not_called()
                new_hb.stop.assert_not_called()
            finally:
                allow_capture.set()
                allow_stop.set()
                old.join(timeout=1)
                if new.ident is not None:
                    new.join(timeout=1)
                bridge.stop()

    @pytest.mark.parametrize("bridge_cls, audio_attr", [
        (SoundBridgeClient, "_audio_receiver"), (SoundBridgeServer, "_audio_capture"),
    ])
    @patch("soundbridge.main.Discovery")
    def test_blocked_old_cleanup_cannot_touch_new_stream_or_deliver_old_frames(self, discovery_cls, bridge_cls, audio_attr):
        bridge = bridge_cls()
        bridge._search_generation = bridge._connected_generation = bridge._stream_generation = 1
        bridge.connected = True
        old_audio, old_heartbeat = MagicMock(), MagicMock()
        new_audio, new_heartbeat = MagicMock(), MagicMock()
        setattr(bridge, audio_attr, old_audio)
        bridge._heartbeat = old_heartbeat
        entered, release = threading.Event(), threading.Event()
        def stop_audio():
            entered.set()
            release.wait()
        old_audio.stop.side_effect = stop_audio
        callback = MagicMock()
        old_frame = _stream_callback(bridge, callback)
        def open_new():
            setattr(bridge, audio_attr, new_audio)
            bridge._heartbeat = new_heartbeat
        worker = threading.Thread(target=bridge._on_disconnect, args=(1,), daemon=True)
        try:
            worker.start()
            assert entered.wait(1)
            assert getattr(bridge, audio_attr) is None and bridge._heartbeat is None
            with patch.object(bridge, "_open_streams", side_effect=open_new):
                bridge.start()
                discovery_cls.call_args.kwargs["on_peer_found"]("198.51.100.10")
            assert bridge.connected
            release.set()
            worker.join(timeout=1)
            assert not worker.is_alive()
            assert getattr(bridge, audio_attr) is new_audio
            assert bridge._heartbeat is new_heartbeat
            assert discovery_cls.call_count == 1
            old_heartbeat.stop.assert_called_once()
            new_audio.stop.assert_not_called()
            new_heartbeat.stop.assert_not_called()
            bridge.stop_streaming(1)  # A stale teardown cannot detach current refs.
            assert getattr(bridge, audio_attr) is new_audio
            old_frame(b"old")
            callback.assert_not_called()
            _stream_callback(bridge, callback)(b"new")
            callback.assert_called_once_with(b"new")
        finally:
            release.set()
            worker.join(timeout=1)
            bridge.stop()


class TestClientReconnect:

    @patch("soundbridge.main.Discovery")
    @patch("soundbridge.main._close_stream_resources")
    @patch.object(SoundBridgeClient, "_start_streaming")
    def test_explicit_ip_retries_and_browses_without_claiming_connection(self, start_streaming, cleanup, discovery):
        callback = MagicMock()
        client = SoundBridgeClient(server_ip="192.0.2.1", mic_enabled=False,
                                   gui_callback=callback)
        client.start()
        assert not client.connected
        discovery.return_value.start_search.assert_called_once_with(
            direct_ip="192.0.2.1", browse=False,
        )
        client._on_server_found("192.0.2.1")
        callback.reset_mock()
        discovery.return_value.start_search.reset_mock()
        client._on_disconnect()

        assert not client.connected
        assert start_streaming.call_count == 1
        cleanup.assert_called_once()
        discovery.return_value.start_search.assert_called_once_with(direct_ip="192.0.2.1")
        callback.assert_called_once_with("disconnected", None)
        # mDNS may discover a different address after DHCP changes.
        client._on_server_found("192.0.2.2")
        assert client.connected
        assert client.server_ip == "192.0.2.2"
        assert start_streaming.call_count == 2

    @patch("soundbridge.main.Discovery")
    @patch("soundbridge.main._close_stream_resources")
    @patch.object(SoundBridgeClient, "_start_streaming")
    def test_discovered_ip_restarts_discovery_after_timeout(self, start_streaming, cleanup, discovery):
        client = SoundBridgeClient(mic_enabled=False)
        client.start()
        client._on_server_found("192.0.2.1")
        client._on_disconnect()

        assert discovery.call_count == 2
        assert discovery.return_value.start_search.call_count == 2
        assert not client.connected


class TestServerWindows:

    def test_win32_uses_wasapi_and_no_mic(self):
        names = ["Heartbeat", "OpusDecoder", "OpusEncoder", "UDPReceiver",
                 "UDPSender", "WasapiLoopbackCapture", "VirtualMicSource",
                 "ParecCapture", "find_monitor_source"]
        ps = {n: patch(f"soundbridge.main.{n}") for n in names}
        m = {n: p.start() for n, p in ps.items()}
        plat = patch("soundbridge.main.sys.platform", "win32")
        plat.start()
        try:
            server = SoundBridgeServer()
            server.peer_ip = "192.0.2.2"
            server._start_streaming()
            m["WasapiLoopbackCapture"].assert_called_once()
            m["WasapiLoopbackCapture"].return_value.start.assert_called_once()
            m["VirtualMicSource"].assert_not_called()
            m["UDPReceiver"].assert_not_called()
            m["ParecCapture"].assert_not_called()
            m["find_monitor_source"].assert_not_called()
            assert server._virtual_mic is None
            assert server._mic_receiver is None
        finally:
            plat.stop()
            for p in ps.values():
                p.stop()
