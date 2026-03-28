"""Entry point for SoundBridge. Orchestrates server and client modes."""

import argparse
import logging
import time

import numpy as np

from . import config
from . import protocol
from .audio import (
    AudioCapture, AudioPlayback, ParecCapture,
    VirtualMicSource, find_monitor_source,
)
from .network import UDPSender, UDPReceiver, Discovery, Heartbeat
from .opus import OpusEncoder, OpusDecoder
from .state import ConnectionState
from .video import (
    LinuxVirtualCameraReceiver,
    VideoSettings,
    WindowsCameraSender,
    list_windows_cameras,
    parse_video_size,
    validate_virtual_camera_device,
)

logger = logging.getLogger(__name__)


class SoundBridgeServer:
    """Server mode (Linux): captures system audio -> sends to client.
    Receives mic from client -> virtual PulseAudio source."""

    def __init__(self, gui_callback=None,
                 video_settings: VideoSettings | None = None):
        self.peer_ip: str | None = None
        self._state = ConnectionState.DISCONNECTED
        self.gui_callback = gui_callback

        # Audio
        self._audio_capture: ParecCapture | None = None
        self._audio_sender: UDPSender | None = None
        self._mic_receiver: UDPReceiver | None = None
        self._virtual_mic: VirtualMicSource | None = None

        # Opus
        self._audio_encoder: OpusEncoder | None = None
        self._mic_decoder: OpusDecoder | None = None
        self._audio_seq = 0
        self._mic_last_seq: int | None = None

        # Network
        self._discovery: Discovery | None = None
        self._heartbeat: Heartbeat | None = None

        # Video
        self._video_settings = video_settings or VideoSettings()
        self._video_receiver: LinuxVirtualCameraReceiver | None = None

        # Volume
        self._mic_volume = 1.0

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @connected.setter
    def connected(self, value: bool):
        self._state = ConnectionState.CONNECTED if value else ConnectionState.DISCONNECTED

    def _emit_gui_event(self, event: str, data=None):
        if self.gui_callback:
            self.gui_callback(event, data)

    def start(self):
        """Start the server: discovery + wait for client."""
        logger.info("Starting server...")
        logger.info("Waiting for client on LAN...")

        self._state = ConnectionState.SEARCHING
        self._discovery = Discovery(on_peer_found=self._on_peer_found)
        self._discovery.start_listen()

    def _on_peer_found(self, ip: str):
        if self._state == ConnectionState.CONNECTED:
            return

        self.peer_ip = ip
        self._state = ConnectionState.CONNECTED
        logger.info("Client found: %s", ip)

        self._discovery.stop()
        self._start_streaming()
        self._emit_gui_event("connected", ip)

    def _start_streaming(self):
        # Setup heartbeat
        self._heartbeat = Heartbeat(
            target_ip=self.peer_ip,
            on_timeout=self._on_disconnect,
        )
        self._heartbeat.start_sender()
        self._heartbeat.start_monitor()

        # Setup Opus encoder/decoder
        self._audio_encoder = OpusEncoder(
            config.SAMPLE_RATE, config.CHANNELS_STEREO, bitrate=128000,
        )
        self._mic_decoder = OpusDecoder(
            config.SAMPLE_RATE, config.CHANNELS_MONO,
        )
        self._audio_seq = 0
        self._mic_last_seq = None

        # Find monitor BEFORE creating null-sink (PipeWire changes default sink)
        monitor_source = find_monitor_source()

        # Setup virtual mic source (Linux) — creates null-sink
        self._virtual_mic = VirtualMicSource()
        self._virtual_mic.start()

        # Capture system audio and send to client
        if monitor_source is None:
            logger.warning("No PulseAudio/PipeWire monitor found. "
                           "System audio capture unavailable.")
            logger.info("Tip: Make sure PulseAudio or PipeWire is running.")
        else:
            logger.info("Capturing system audio (%s)", monitor_source)
            self._audio_sender = UDPSender(self.peer_ip, config.AUDIO_PORT)
            self._audio_capture = ParecCapture(
                callback=self._on_audio_captured,
                channels=config.CHANNELS_STEREO,
                device_name=monitor_source,
            )
            self._audio_capture.start()

        if self._virtual_mic.active:
            logger.info("Virtual mic source created (pipe-source)")
        else:
            logger.warning("Could not create virtual mic. "
                           "Remote mic will not be available as input device.")

        # Receive mic audio from client
        self._mic_receiver = UDPReceiver(
            port=config.MIC_PORT,
            callback=self._on_mic_received,
        )
        self._mic_receiver.start()

        self._start_video_streaming()
        logger.info("Streaming active (Opus codec enabled).")

    def _start_video_streaming(self):
        if not self._video_settings.enabled:
            return

        self._emit_gui_event(
            "virtual_camera_starting",
            {"message": "Preparing virtual camera..."},
        )

        receiver = LinuxVirtualCameraReceiver(
            settings=self._video_settings,
            on_error=self._on_video_error,
        )
        if not receiver.start():
            message = receiver.last_error or "Failed to start virtual camera."
            logger.warning("Video receiver unavailable: %s", message)
            self._emit_gui_event(
                "virtual_camera_error",
                {"message": message},
            )
            return

        self._video_receiver = receiver
        logger.info("Virtual camera ready at %s", self._video_settings.virtual_device)
        self._emit_gui_event(
            "virtual_camera_ready",
            {
                "message": self._video_settings.virtual_device,
                "details": self._video_settings.virtual_device,
            },
        )

    def _stop_video_streaming(self):
        if self._video_receiver:
            self._video_receiver.stop()
            self._video_receiver = None

    def _on_video_error(self, message: str):
        logger.error("Virtual camera error: %s", message)
        self._emit_gui_event("virtual_camera_error", {"message": message})

    def _on_audio_captured(self, audio_data: np.ndarray):
        if self._audio_sender and self._state == ConnectionState.CONNECTED:
            opus_data = self._audio_encoder.encode(audio_data)
            self._audio_sender.send_audio(
                opus_data, config.PKT_AUDIO_DATA,
                config.CHANNELS_STEREO, seq=self._audio_seq,
            )
            self._audio_seq = (self._audio_seq + 1) % 65536

    def _on_mic_received(self, packet: protocol.Packet):
        if packet.pkt_type != config.PKT_MIC_DATA or not self._virtual_mic:
            return

        # Detect gaps and apply PLC
        if self._mic_last_seq is not None:
            expected = (self._mic_last_seq + 1) % 65536
            gap = (packet.seq - expected) % 65536
            if 0 < gap < 100:
                logger.debug("PLC: mic gap of %d packets", gap)
                for _ in range(gap):
                    plc_frame = self._mic_decoder.plc(config.FRAME_SIZE)
                    if self._mic_volume != 1.0:
                        plc_frame = (plc_frame.astype(np.float32) * self._mic_volume).astype(np.int16)
                    self._virtual_mic.feed(plc_frame)
        self._mic_last_seq = packet.seq

        pcm = self._mic_decoder.decode(packet.payload)
        if self._mic_volume != 1.0:
            pcm = (pcm.astype(np.float32) * self._mic_volume).astype(np.int16)
        self._virtual_mic.feed(pcm)

    def _on_disconnect(self):
        if self._state != ConnectionState.CONNECTED:
            return
        logger.info("Client disconnected (heartbeat timeout).")
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()
        self._emit_gui_event("disconnected", None)

        # Restart discovery
        logger.info("Waiting for client...")
        self._state = ConnectionState.SEARCHING
        self._discovery = Discovery(on_peer_found=self._on_peer_found)
        self._discovery.start_listen()

    def stop_streaming(self):
        self._stop_video_streaming()
        if self._audio_capture:
            self._audio_capture.stop()
            self._audio_capture = None
        if self._audio_sender:
            self._audio_sender.close()
            self._audio_sender = None
        if self._mic_receiver:
            self._mic_receiver.stop()
            self._mic_receiver = None
        if self._virtual_mic:
            self._virtual_mic.stop()
            self._virtual_mic = None
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None
        if self._audio_encoder:
            self._audio_encoder.destroy()
            self._audio_encoder = None
        if self._mic_decoder:
            self._mic_decoder.destroy()
            self._mic_decoder = None

    def set_video_settings(self, settings: VideoSettings):
        previous_settings = self._video_settings
        self._video_settings = settings

        if not self.connected:
            return

        if not previous_settings.enabled and settings.enabled:
            self._start_video_streaming()
        elif previous_settings.enabled and not settings.enabled:
            self._stop_video_streaming()
            self._emit_gui_event(
                "video_stopped",
                {"message": "Virtual camera disabled."},
            )
        elif previous_settings.enabled and settings.enabled and previous_settings != settings:
            self._stop_video_streaming()
            self._start_video_streaming()

    def check_virtual_camera_setup(self) -> tuple[bool, str | None]:
        return validate_virtual_camera_device(self._video_settings.virtual_device)

    def set_mic_volume(self, volume: float):
        self._mic_volume = max(0.0, min(1.0, volume))

    def stop(self):
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()
        if self._discovery:
            self._discovery.stop()
            self._discovery = None
        logger.info("Server stopped.")


class SoundBridgeClient:
    """Client mode (Windows): receives system audio -> plays on headphone.
    Captures mic -> sends to server."""

    def __init__(self, server_ip: str | None = None, gui_callback=None,
                 video_settings: VideoSettings | None = None):
        self.server_ip = server_ip
        self._state = ConnectionState.DISCONNECTED
        self.gui_callback = gui_callback

        # Audio
        self._audio_receiver: UDPReceiver | None = None
        self._audio_playback: AudioPlayback | None = None
        self._mic_capture: AudioCapture | None = None
        self._mic_sender: UDPSender | None = None

        # Opus
        self._audio_decoder: OpusDecoder | None = None
        self._mic_encoder: OpusEncoder | None = None
        self._mic_seq = 0
        self._audio_last_seq: int | None = None

        # Network
        self._discovery: Discovery | None = None
        self._heartbeat: Heartbeat | None = None

        # Video
        self._video_settings = video_settings or VideoSettings()
        self._camera_sender: WindowsCameraSender | None = None

        # Volume
        self._audio_volume = 1.0

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @connected.setter
    def connected(self, value: bool):
        self._state = ConnectionState.CONNECTED if value else ConnectionState.DISCONNECTED

    def _emit_gui_event(self, event: str, data=None):
        if self.gui_callback:
            self.gui_callback(event, data)

    def start(self):
        """Start the client: discover server or connect to given IP."""
        logger.info("Starting client...")

        if self.server_ip:
            logger.info("Connecting to %s...", self.server_ip)
            self._on_server_found(self.server_ip)
        else:
            logger.info("Searching for server on LAN...")
            self._state = ConnectionState.SEARCHING
            self._discovery = Discovery(on_peer_found=self._on_server_found)
            self._discovery.start_search()

    def _on_server_found(self, ip: str):
        if self._state == ConnectionState.CONNECTED:
            return

        self.server_ip = ip
        self._state = ConnectionState.CONNECTED
        logger.info("Server found: %s", ip)

        if self._discovery:
            self._discovery.stop()
            self._discovery = None

        self._start_streaming()
        self._emit_gui_event("connected", ip)

    def _start_streaming(self):
        # Setup heartbeat
        self._heartbeat = Heartbeat(
            target_ip=self.server_ip,
            on_timeout=self._on_disconnect,
        )
        self._heartbeat.start_sender()
        self._heartbeat.start_monitor()

        # Setup Opus decoder/encoder
        self._audio_decoder = OpusDecoder(
            config.SAMPLE_RATE, config.CHANNELS_STEREO,
        )
        self._mic_encoder = OpusEncoder(
            config.SAMPLE_RATE, config.CHANNELS_MONO, bitrate=64000,
        )
        self._mic_seq = 0
        self._audio_last_seq = None

        # Receive system audio from server and play it
        self._audio_playback = AudioPlayback(
            channels=config.CHANNELS_STEREO,
        )
        self._audio_playback.start()

        self._audio_receiver = UDPReceiver(
            port=config.AUDIO_PORT,
            callback=self._on_audio_received,
        )
        self._audio_receiver.start()
        logger.info("Receiving system audio -> headphone (Opus)")

        # Capture mic and send to server
        self._mic_sender = UDPSender(self.server_ip, config.MIC_PORT)
        self._mic_capture = AudioCapture(
            callback=self._on_mic_captured,
            channels=config.CHANNELS_MONO,
        )
        self._mic_capture.start()
        logger.info("Capturing mic -> server (Opus)")

        self._start_video_streaming()
        logger.info("Streaming active (Opus codec enabled).")

    def _start_video_streaming(self):
        if not self._video_settings.enabled or not self.server_ip:
            return

        self._emit_gui_event(
            "video_starting",
            {"message": "Starting camera..."},
        )

        sender = WindowsCameraSender(
            target_ip=self.server_ip,
            settings=self._video_settings,
            on_error=self._on_video_error,
        )
        if not sender.start():
            message = sender.last_error or "Failed to start camera stream."
            logger.warning("Camera sender unavailable: %s", message)
            self._emit_gui_event("video_error", {"message": message})
            return

        self._camera_sender = sender
        camera_name = sender.camera_name or "Default camera"
        logger.info("Sharing webcam (%s)", camera_name)
        self._emit_gui_event(
            "video_streaming",
            {"message": camera_name, "details": camera_name},
        )

    def _stop_video_streaming(self):
        if self._camera_sender:
            self._camera_sender.stop()
            self._camera_sender = None

    def _on_video_error(self, message: str):
        logger.error("Camera sender error: %s", message)
        self._emit_gui_event("video_error", {"message": message})

    def _on_audio_received(self, packet: protocol.Packet):
        if packet.pkt_type != config.PKT_AUDIO_DATA or not self._audio_playback:
            return

        # Detect gaps and apply PLC
        if self._audio_last_seq is not None:
            expected = (self._audio_last_seq + 1) % 65536
            gap = (packet.seq - expected) % 65536
            if 0 < gap < 100:
                logger.debug("PLC: audio gap of %d packets", gap)
                for _ in range(gap):
                    plc_frame = self._audio_decoder.plc(config.FRAME_SIZE)
                    self._audio_playback.feed(plc_frame)
        self._audio_last_seq = packet.seq

        pcm = self._audio_decoder.decode(packet.payload)
        self._audio_playback.feed(pcm)

    def _on_mic_captured(self, audio_data: np.ndarray):
        if self._mic_sender and self._state == ConnectionState.CONNECTED:
            opus_data = self._mic_encoder.encode(audio_data)
            self._mic_sender.send_audio(
                opus_data, config.PKT_MIC_DATA,
                config.CHANNELS_MONO, seq=self._mic_seq,
            )
            self._mic_seq = (self._mic_seq + 1) % 65536

    def _on_disconnect(self):
        if self._state != ConnectionState.CONNECTED:
            return
        logger.info("Server disconnected (heartbeat timeout).")
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()
        self._emit_gui_event("disconnected", None)

        # Restart discovery
        logger.info("Searching for server...")
        self._state = ConnectionState.SEARCHING
        self._discovery = Discovery(on_peer_found=self._on_server_found)
        self._discovery.start_search()

    def stop_streaming(self):
        self._stop_video_streaming()
        if self._audio_receiver:
            self._audio_receiver.stop()
            self._audio_receiver = None
        if self._audio_playback:
            self._audio_playback.stop()
            self._audio_playback = None
        if self._mic_capture:
            self._mic_capture.stop()
            self._mic_capture = None
        if self._mic_sender:
            self._mic_sender.close()
            self._mic_sender = None
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None
        if self._audio_decoder:
            self._audio_decoder.destroy()
            self._audio_decoder = None
        if self._mic_encoder:
            self._mic_encoder.destroy()
            self._mic_encoder = None

    def set_video_settings(self, settings: VideoSettings):
        previous_settings = self._video_settings
        self._video_settings = settings

        if not self.connected:
            return

        if not previous_settings.enabled and settings.enabled:
            self._start_video_streaming()
        elif previous_settings.enabled and not settings.enabled:
            self._stop_video_streaming()
            self._emit_gui_event(
                "video_stopped",
                {"message": "Camera sharing disabled."},
            )
        elif previous_settings.enabled and settings.enabled and previous_settings != settings:
            self._stop_video_streaming()
            self._start_video_streaming()

    def list_cameras(self) -> list[str]:
        return list_windows_cameras()

    def set_audio_volume(self, volume: float):
        self._audio_volume = max(0.0, min(1.0, volume))
        if self._audio_playback:
            self._audio_playback.set_volume(self._audio_volume)

    def stop(self):
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()
        if self._discovery:
            self._discovery.stop()
            self._discovery = None
        logger.info("Client stopped.")


def run_server_cli(args):
    """Run server in CLI mode (no GUI)."""
    server = SoundBridgeServer(video_settings=_server_video_settings_from_args(args))
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.stop()


def run_client_cli(args):
    """Run client in CLI mode (no GUI)."""
    client = SoundBridgeClient(
        server_ip=args.ip,
        video_settings=_client_video_settings_from_args(args),
    )
    client.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        client.stop()


def _client_video_settings_from_args(args) -> VideoSettings:
    width, height = parse_video_size(args.video_size)
    return VideoSettings(
        enabled=args.webcam,
        camera_name=args.camera_device or None,
        width=width,
        height=height,
        fps=args.video_fps,
        video_port=config.VIDEO_PORT,
        virtual_device=config.VIDEO_DEFAULT_DEVICE,
    )


def _server_video_settings_from_args(args) -> VideoSettings:
    return VideoSettings(
        enabled=args.webcam,
        width=config.VIDEO_DEFAULT_WIDTH,
        height=config.VIDEO_DEFAULT_HEIGHT,
        fps=config.VIDEO_DEFAULT_FPS,
        video_port=config.VIDEO_PORT,
        virtual_device=args.virtual_camera_device,
    )


def main():
    parser = argparse.ArgumentParser(
        prog="soundbridge",
        description="SoundBridge — Audio bridge between machines over LAN",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Server mode
    server_parser = subparsers.add_parser(
        "server", aliases=["--server"],
        help="Run as server (Linux — captures system audio, receives mic)"
    )
    server_parser.add_argument(
        "--webcam", action="store_true",
        help="Expose a virtual camera on Linux"
    )
    server_parser.add_argument(
        "--virtual-camera-device", type=str,
        default=config.VIDEO_DEFAULT_DEVICE,
        help="Linux virtual camera device path"
    )

    # Client mode
    client_parser = subparsers.add_parser(
        "client", aliases=["--client"],
        help="Run as client (Windows — plays audio, captures mic)"
    )
    client_parser.add_argument(
        "--ip", type=str, default=None,
        help="Server IP address (skip auto-discovery)"
    )
    client_parser.add_argument(
        "--webcam", action="store_true",
        help="Share the local webcam to the Linux server"
    )
    client_parser.add_argument(
        "--camera-device", type=str, default=None,
        help="DirectShow camera name to capture"
    )
    client_parser.add_argument(
        "--video-size", type=str,
        default=f"{config.VIDEO_DEFAULT_WIDTH}x{config.VIDEO_DEFAULT_HEIGHT}",
        help="Webcam size in WIDTHxHEIGHT format"
    )
    client_parser.add_argument(
        "--video-fps", type=int, default=config.VIDEO_DEFAULT_FPS,
        help="Webcam frames per second"
    )
    client_parser.add_argument(
        "--list-cameras", action="store_true",
        help="List available Windows cameras and exit"
    )

    # Common
    for p in [server_parser, client_parser]:
        p.add_argument(
            "--no-gui", action="store_true",
            help="Run without GUI (CLI only)"
        )
        p.add_argument(
            "--list-devices", action="store_true",
            help="List available audio devices and exit"
        )

    args = parser.parse_args()

    if hasattr(args, "video_size"):
        try:
            parse_video_size(args.video_size)
        except ValueError as exc:
            parser.error(str(exc))

    if hasattr(args, "video_fps") and args.video_fps <= 0:
        parser.error("video fps must be positive")

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(message)s",
    )

    if args.list_devices:
        from .audio import list_input_devices, list_output_devices
        print("=== Input Devices ===")
        for dev in list_input_devices():
            print(f"  [{dev['index']}] {dev['name']} ({dev['channels']}ch)")
        print("\n=== Output Devices ===")
        for dev in list_output_devices():
            print(f"  [{dev['index']}] {dev['name']} ({dev['channels']}ch)")
        return

    if getattr(args, "list_cameras", False):
        print("=== Cameras ===")
        cameras = list_windows_cameras()
        if not cameras:
            print("  No cameras found.")
        else:
            for index, name in enumerate(cameras):
                print(f"  [{index}] {name}")
        return

    if args.no_gui:
        if args.mode in ("server", "--server"):
            run_server_cli(args)
        else:
            run_client_cli(args)
    else:
        from .gui import run_gui
        if args.mode in ("server", "--server"):
            run_gui("server", args)
        else:
            run_gui("client", args)


if __name__ == "__main__":
    main()
