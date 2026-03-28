"""Tests for main lifecycle integration with video helpers."""

import importlib
import importlib.util
import sys
import types
from unittest.mock import MagicMock, patch


def _install_test_stubs():
    if importlib.util.find_spec("numpy") is None:
        numpy_mod = types.ModuleType("numpy")
        numpy_mod.ndarray = object
        numpy_mod.int16 = int
        numpy_mod.float32 = float
        sys.modules.setdefault("numpy", numpy_mod)

    if importlib.util.find_spec("sounddevice") is None:
        sounddevice_mod = types.ModuleType("sounddevice")
        sounddevice_mod.InputStream = type("InputStream", (), {})
        sounddevice_mod.OutputStream = type("OutputStream", (), {})
        sounddevice_mod.query_devices = lambda: []
        sys.modules.setdefault("sounddevice", sounddevice_mod)

    if importlib.util.find_spec("zeroconf") is None:
        zeroconf_mod = types.ModuleType("zeroconf")
        zeroconf_mod.IPVersion = type("IPVersion", (), {"V4Only": object()})
        zeroconf_mod.ServiceBrowser = object
        zeroconf_mod.ServiceInfo = object
        zeroconf_mod.ServiceStateChange = type(
            "ServiceStateChange",
            (),
            {"Added": object(), "Removed": object()},
        )
        zeroconf_mod.Zeroconf = object
        sys.modules.setdefault("zeroconf", zeroconf_mod)


_install_test_stubs()

main = importlib.import_module("soundbridge.main")
video = importlib.import_module("soundbridge.video")
state = importlib.import_module("soundbridge.state")

SoundBridgeClient = main.SoundBridgeClient
SoundBridgeServer = main.SoundBridgeServer
ConnectionState = state.ConnectionState
VideoSettings = video.VideoSettings


class TestSoundBridgeClientVideo:

    @patch("soundbridge.main.WindowsCameraSender")
    @patch("soundbridge.main.AudioCapture")
    @patch("soundbridge.main.UDPSender")
    @patch("soundbridge.main.UDPReceiver")
    @patch("soundbridge.main.AudioPlayback")
    @patch("soundbridge.main.OpusEncoder")
    @patch("soundbridge.main.OpusDecoder")
    @patch("soundbridge.main.Heartbeat")
    def test_start_streaming_starts_camera_sender_when_enabled(
        self,
        mock_heartbeat_cls,
        mock_decoder_cls,
        mock_encoder_cls,
        mock_playback_cls,
        mock_receiver_cls,
        mock_sender_cls,
        mock_capture_cls,
        mock_camera_sender_cls,
    ):
        gui_callback = MagicMock()
        video_settings = VideoSettings(enabled=True, camera_name="USB Camera")
        mock_camera_sender = MagicMock()
        mock_camera_sender.start.return_value = True
        mock_camera_sender.camera_name = "USB Camera"
        mock_camera_sender_cls.return_value = mock_camera_sender

        client = SoundBridgeClient(
            server_ip="192.168.1.50",
            gui_callback=gui_callback,
            video_settings=video_settings,
        )
        client._state = ConnectionState.CONNECTED

        client._start_streaming()

        mock_camera_sender_cls.assert_called_once()
        mock_camera_sender.start.assert_called_once()
        gui_callback.assert_any_call(
            "video_starting",
            {"message": "Starting camera..."},
        )
        gui_callback.assert_any_call(
            "video_streaming",
            {"message": "USB Camera", "details": "USB Camera"},
        )

    def test_set_video_settings_stops_sender_when_disabled(self):
        gui_callback = MagicMock()
        client = SoundBridgeClient(gui_callback=gui_callback)
        client._state = ConnectionState.CONNECTED
        client._video_settings = VideoSettings(enabled=True)
        camera_sender = MagicMock()
        client._camera_sender = camera_sender

        client.set_video_settings(VideoSettings(enabled=False))

        camera_sender.stop.assert_called_once()
        gui_callback.assert_called_with(
            "video_stopped",
            {"message": "Camera sharing disabled."},
        )

    def test_set_video_settings_restarts_sender_when_active_settings_change(self):
        client = SoundBridgeClient()
        client._state = ConnectionState.CONNECTED
        client._video_settings = VideoSettings(enabled=True, fps=30)
        camera_sender = MagicMock()
        client._camera_sender = camera_sender

        with patch.object(client, "_start_video_streaming") as start_video:
            client.set_video_settings(VideoSettings(enabled=True, fps=15))

        camera_sender.stop.assert_called_once()
        start_video.assert_called_once()


class TestSoundBridgeServerVideo:

    @patch("soundbridge.main.LinuxVirtualCameraReceiver")
    @patch("soundbridge.main.UDPReceiver")
    @patch("soundbridge.main.VirtualMicSource")
    @patch("soundbridge.main.find_monitor_source", return_value=None)
    @patch("soundbridge.main.OpusDecoder")
    @patch("soundbridge.main.OpusEncoder")
    @patch("soundbridge.main.Heartbeat")
    def test_start_streaming_starts_virtual_camera_when_enabled(
        self,
        mock_heartbeat_cls,
        mock_encoder_cls,
        mock_decoder_cls,
        mock_find_monitor,
        mock_virtual_mic_cls,
        mock_receiver_cls,
        mock_video_receiver_cls,
    ):
        gui_callback = MagicMock()
        video_settings = VideoSettings(enabled=True, virtual_device="/dev/video10")
        mock_virtual_mic = MagicMock()
        mock_virtual_mic.active = True
        mock_virtual_mic_cls.return_value = mock_virtual_mic
        mock_video_receiver = MagicMock()
        mock_video_receiver.start.return_value = True
        mock_video_receiver.last_error = None
        mock_video_receiver_cls.return_value = mock_video_receiver

        server = SoundBridgeServer(
            gui_callback=gui_callback,
            video_settings=video_settings,
        )
        server.peer_ip = "192.168.1.20"
        server._state = ConnectionState.CONNECTED

        server._start_streaming()

        mock_video_receiver_cls.assert_called_once()
        mock_video_receiver.start.assert_called_once()
        gui_callback.assert_any_call(
            "virtual_camera_starting",
            {"message": "Preparing virtual camera..."},
        )
        gui_callback.assert_any_call(
            "virtual_camera_ready",
            {"message": "/dev/video10", "details": "/dev/video10"},
        )

    def test_set_video_settings_stops_receiver_when_disabled(self):
        gui_callback = MagicMock()
        server = SoundBridgeServer(gui_callback=gui_callback)
        server._state = ConnectionState.CONNECTED
        server._video_settings = VideoSettings(enabled=True)
        video_receiver = MagicMock()
        server._video_receiver = video_receiver

        server.set_video_settings(VideoSettings(enabled=False))

        video_receiver.stop.assert_called_once()
        gui_callback.assert_called_with(
            "video_stopped",
            {"message": "Virtual camera disabled."},
        )

    def test_set_video_settings_restarts_receiver_when_active_settings_change(self):
        server = SoundBridgeServer()
        server._state = ConnectionState.CONNECTED
        server._video_settings = VideoSettings(enabled=True, virtual_device="/dev/video10")
        video_receiver = MagicMock()
        server._video_receiver = video_receiver

        with patch.object(server, "_start_video_streaming") as start_video:
            server.set_video_settings(
                VideoSettings(enabled=True, virtual_device="/dev/video11"),
            )

        video_receiver.stop.assert_called_once()
        start_video.assert_called_once()
