"""Tests for video helpers and ffmpeg process wrappers."""

import stat
from unittest.mock import MagicMock, patch

from soundbridge import config
from soundbridge.video import (
    FFmpegProcess,
    LinuxVirtualCameraReceiver,
    VideoSettings,
    WindowsCameraSender,
    list_windows_cameras,
    parse_video_size,
    validate_virtual_camera_device,
)


class TestParseVideoSize:

    def test_parse_valid_size(self):
        assert parse_video_size("1280x720") == (1280, 720)

    def test_reject_invalid_size(self):
        try:
            parse_video_size("720p")
        except ValueError as exc:
            assert "WIDTHxHEIGHT" in str(exc)
        else:
            raise AssertionError("Expected ValueError for invalid size")


class TestListWindowsCameras:

    @patch("soundbridge.video.find_ffmpeg", return_value="ffmpeg")
    @patch("soundbridge.video.subprocess.run")
    def test_parses_dshow_video_devices(self, mock_run, mock_ffmpeg):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr=(
                '[dshow @ 1234] DirectShow video devices\n'
                '[dshow @ 1234]  "Integrated Camera"\n'
                '[dshow @ 1234]     Alternative name "@device_pnp"\n'
                '[dshow @ 1234]  "USB Camera"\n'
                '[dshow @ 1234] DirectShow audio devices\n'
            ),
        )

        with patch("soundbridge.video.sys.platform", "win32"):
            assert list_windows_cameras() == ["Integrated Camera", "USB Camera"]


class TestVirtualCameraValidation:

    @patch("soundbridge.video.find_ffmpeg", return_value="ffmpeg")
    @patch("soundbridge.video.os.close")
    @patch("soundbridge.video.os.open", return_value=10)
    @patch("soundbridge.video.os.stat")
    @patch("soundbridge.video.os.path.exists")
    def test_validate_virtual_camera_device_success(
        self,
        mock_exists,
        mock_stat,
        mock_open,
        mock_close,
        mock_ffmpeg,
    ):
        mock_exists.side_effect = [True, True]
        mock_stat.return_value.st_mode = stat.S_IFCHR

        with patch("soundbridge.video.sys.platform", "linux"):
            ok, error = validate_virtual_camera_device("/dev/video10")

        assert ok is True
        assert error is None
        mock_open.assert_called_once()
        mock_close.assert_called_once_with(10)

    @patch("soundbridge.video.find_ffmpeg", return_value="ffmpeg")
    @patch("soundbridge.video.os.path.exists", return_value=False)
    def test_validate_virtual_camera_device_requires_module(
        self,
        mock_exists,
        mock_ffmpeg,
    ):
        with patch("soundbridge.video.sys.platform", "linux"):
            ok, error = validate_virtual_camera_device("/dev/video10")

        assert ok is False
        assert error == "v4l2loopback kernel module is not loaded."


class TestFFmpegProcess:

    @patch("soundbridge.video.subprocess.Popen", side_effect=OSError("missing ffmpeg"))
    def test_start_reports_os_error(self, mock_popen):
        process = FFmpegProcess(["ffmpeg"], label="video test")
        assert process.start() is False
        assert process.last_error == "missing ffmpeg"


class TestWindowsCameraSender:

    def test_build_command_contains_expected_flags(self):
        settings = VideoSettings(width=640, height=360, fps=15, video_port=4412)
        sender = WindowsCameraSender("192.168.1.50", settings)

        command = sender._build_command("ffmpeg", "USB Camera")

        assert command[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        assert "video=USB Camera" in command
        assert "640x360" in command
        assert "15" in command
        assert any(
            part == "udp://192.168.1.50:4412?pkt_size=1316"
            for part in command
        )

    @patch("soundbridge.video.FFmpegProcess")
    @patch("soundbridge.video.list_windows_cameras", return_value=["Integrated Camera"])
    @patch("soundbridge.video.find_ffmpeg", return_value="ffmpeg")
    def test_start_uses_first_camera_when_none_selected(
        self,
        mock_ffmpeg,
        mock_cameras,
        mock_process_cls,
    ):
        mock_process = MagicMock()
        mock_process.start.return_value = True
        mock_process.last_error = None
        mock_process_cls.return_value = mock_process

        with patch("soundbridge.video.sys.platform", "win32"):
            sender = WindowsCameraSender("192.168.1.50", VideoSettings(enabled=True))
            assert sender.start() is True

        assert sender.camera_name == "Integrated Camera"
        mock_process.start.assert_called_once()


class TestLinuxVirtualCameraReceiver:

    def test_build_command_contains_virtual_camera_output(self):
        settings = VideoSettings(
            width=1280,
            height=720,
            fps=30,
            video_port=config.VIDEO_PORT,
            virtual_device="/dev/video10",
        )
        receiver = LinuxVirtualCameraReceiver(settings)

        command = receiver._build_command("ffmpeg")

        assert command[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        assert any("udp://0.0.0.0:4412?listen=1" in part for part in command)
        assert "format=yuv420p" in command
        assert "/dev/video10" == command[-1]
        assert "rawvideo" in command

    @patch("soundbridge.video.validate_virtual_camera_device", return_value=(False, "missing device"))
    def test_start_fails_when_prerequisites_fail(self, mock_validate):
        receiver = LinuxVirtualCameraReceiver(VideoSettings(enabled=True))

        assert receiver.start() is False
        assert receiver.last_error == "missing device"
