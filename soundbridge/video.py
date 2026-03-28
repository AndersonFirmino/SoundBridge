"""Video helpers for SoundBridge webcam sharing."""

from collections import deque
from dataclasses import dataclass
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

from . import config

logger = logging.getLogger(__name__)


def find_ffmpeg() -> str | None:
    """Return the ffmpeg executable path if available."""
    return shutil.which("ffmpeg")


def parse_video_size(value: str) -> tuple[int, int]:
    """Parse WIDTHxHEIGHT strings used by CLI and GUI."""
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", value.lower())
    if not match:
        raise ValueError("video size must use WIDTHxHEIGHT")

    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("video size must be positive")

    return width, height


def format_video_size(width: int, height: int) -> str:
    return f"{width}x{height}"


def _parse_dshow_video_devices(output: str) -> list[str]:
    devices: list[str] = []
    in_video_section = False

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            in_video_section = False
            continue
        if not in_video_section or "Alternative name" in line:
            continue

        match = re.search(r'"([^"]+)"', line)
        if match:
            name = match.group(1)
            if name not in devices:
                devices.append(name)

    return devices


def list_windows_cameras() -> list[str]:
    """List DirectShow video capture devices on Windows."""
    if sys.platform != "win32":
        return []

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-list_devices",
                "true",
                "-f",
                "dshow",
                "-i",
                "dummy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    output = f"{result.stdout}\n{result.stderr}"
    return _parse_dshow_video_devices(output)


def validate_virtual_camera_device(path: str) -> tuple[bool, str | None]:
    """Validate the Linux v4l2loopback device used for webcam output."""
    if sys.platform != "linux":
        return False, "Virtual camera output is supported only on Linux."

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found in PATH."

    if not os.path.exists("/sys/module/v4l2loopback"):
        return False, "v4l2loopback kernel module is not loaded."

    if not os.path.exists(path):
        return False, f"Virtual camera device not found: {path}"

    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        return False, str(exc)

    if not stat.S_ISCHR(mode):
        return False, f"Virtual camera path is not a device: {path}"

    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        return False, f"Cannot access virtual camera device: {exc}"
    else:
        os.close(fd)

    return True, None


@dataclass(slots=True)
class VideoSettings:
    enabled: bool = False
    camera_name: str | None = None
    width: int = config.VIDEO_DEFAULT_WIDTH
    height: int = config.VIDEO_DEFAULT_HEIGHT
    fps: int = config.VIDEO_DEFAULT_FPS
    video_port: int = config.VIDEO_PORT
    virtual_device: str = config.VIDEO_DEFAULT_DEVICE


class FFmpegProcess:
    """Small subprocess wrapper for ffmpeg video pipelines."""

    def __init__(self, command: list[str], label: str,
                 on_exit=None):
        self.command = command
        self.label = label
        self._on_exit = on_exit
        self._process: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stopping = False
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def start(self) -> bool:
        if self.is_running():
            return True

        self.last_error = None
        self._stderr_lines.clear()
        self._stopping = False

        logger.info("Starting %s", self.label)
        logger.debug("%s command: %s", self.label, " ".join(self.command))

        try:
            process = subprocess.Popen(
                self.command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self.last_error = str(exc)
            return False

        with self._lock:
            self._process = process

        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            daemon=True,
        )
        self._stderr_thread.start()

        self._watch_thread = threading.Thread(
            target=self._watch_process,
            args=(process,),
            daemon=True,
        )
        self._watch_thread.start()

        deadline = time.monotonic() + config.VIDEO_START_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.last_error = self._build_error_message(process.returncode)
                self.stop()
                return False
            time.sleep(0.05)

        return True

    def _read_stderr(self, process: subprocess.Popen):
        if not process.stderr:
            return

        for line in process.stderr:
            text = line.strip()
            if not text:
                continue
            self._stderr_lines.append(text)
            logger.debug("%s: %s", self.label, text)

    def _watch_process(self, process: subprocess.Popen):
        returncode = process.wait()
        with self._lock:
            unexpected_exit = not self._stopping and self._process is process
            if self._process is process:
                self._process = None

        if unexpected_exit:
            self.last_error = self._build_error_message(returncode)
            logger.error("%s stopped: %s", self.label, self.last_error)
            if self._on_exit:
                self._on_exit(self.last_error)

    def _build_error_message(self, returncode: int | None) -> str:
        for line in reversed(self._stderr_lines):
            lowered = line.lower()
            if "error" in lowered or "fail" in lowered:
                return line
        if self._stderr_lines:
            return self._stderr_lines[-1]
        if returncode is None:
            return "ffmpeg exited unexpectedly"
        return f"ffmpeg exited with code {returncode}"

    def stop(self):
        with self._lock:
            process = self._process
            self._process = None
            self._stopping = True

        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=config.VIDEO_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            else:
                process.wait()

            if process.stderr:
                process.stderr.close()

        if self._stderr_thread:
            self._stderr_thread.join(timeout=1)
            self._stderr_thread = None

        if self._watch_thread:
            self._watch_thread.join(timeout=1)
            self._watch_thread = None

    def is_running(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None


class WindowsCameraSender:
    """Streams a Windows webcam to the Linux peer using ffmpeg."""

    def __init__(self, target_ip: str, settings: VideoSettings,
                 on_error=None):
        self.target_ip = target_ip
        self.settings = settings
        self._on_error = on_error
        self._process: FFmpegProcess | None = None
        self._camera_name: str | None = None
        self.last_error: str | None = None

    @property
    def camera_name(self) -> str | None:
        return self._camera_name or self.settings.camera_name

    def start(self) -> bool:
        if sys.platform != "win32":
            self.last_error = "Webcam capture is supported only on Windows."
            return False

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.last_error = "ffmpeg not found in PATH."
            return False

        camera_name = self.settings.camera_name
        cameras = list_windows_cameras()
        if not camera_name:
            if not cameras:
                self.last_error = "No camera found."
                return False
            camera_name = cameras[0]

        if cameras and camera_name not in cameras:
            self.last_error = f"Camera not found: {camera_name}"
            return False

        self._camera_name = camera_name
        self._process = FFmpegProcess(
            self._build_command(ffmpeg, camera_name),
            label="webcam sender",
            on_exit=self._handle_process_error,
        )
        started = self._process.start()
        self.last_error = self._process.last_error
        return started

    def _build_command(self, ffmpeg: str, camera_name: str) -> list[str]:
        return [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "dshow",
            "-rtbufsize",
            "64M",
            "-video_size",
            format_video_size(self.settings.width, self.settings.height),
            "-framerate",
            str(self.settings.fps),
            "-i",
            f"video={camera_name}",
            "-an",
            "-c:v",
            config.VIDEO_DEFAULT_CODEC,
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            config.VIDEO_DEFAULT_PIX_FMT,
            "-f",
            "mpegts",
            f"udp://{self.target_ip}:{self.settings.video_port}?pkt_size={config.VIDEO_UDP_PKT_SIZE}",
        ]

    def _handle_process_error(self, message: str):
        self.last_error = message
        if self._on_error:
            self._on_error(message)

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running()

    def stop(self):
        if self._process:
            self._process.stop()
            self.last_error = self._process.last_error
            self._process = None


class LinuxVirtualCameraReceiver:
    """Receives video over UDP and publishes it as a virtual webcam."""

    def __init__(self, settings: VideoSettings, on_error=None):
        self.settings = settings
        self._on_error = on_error
        self._process: FFmpegProcess | None = None
        self.last_error: str | None = None

    def check_prerequisites(self) -> tuple[bool, str | None]:
        return validate_virtual_camera_device(self.settings.virtual_device)

    def start(self) -> bool:
        ok, error = self.check_prerequisites()
        if not ok:
            self.last_error = error
            return False

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.last_error = "ffmpeg not found in PATH."
            return False

        self._process = FFmpegProcess(
            self._build_command(ffmpeg),
            label="virtual camera receiver",
            on_exit=self._handle_process_error,
        )
        started = self._process.start()
        self.last_error = self._process.last_error
        return started

    def _build_command(self, ffmpeg: str) -> list[str]:
        return [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-i",
            f"udp://0.0.0.0:{self.settings.video_port}?listen=1&fifo_size=1000000&overrun_nonfatal=1",
            "-an",
            "-vf",
            f"format={config.VIDEO_DEFAULT_PIX_FMT}",
            "-vcodec",
            "rawvideo",
            "-pix_fmt",
            config.VIDEO_DEFAULT_PIX_FMT,
            "-f",
            "v4l2",
            self.settings.virtual_device,
        ]

    def _handle_process_error(self, message: str):
        self.last_error = message
        if self._on_error:
            self._on_error(message)

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running()

    def stop(self):
        if self._process:
            self._process.stop()
            self.last_error = self._process.last_error
            self._process = None
