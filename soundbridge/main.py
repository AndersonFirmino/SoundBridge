"""Entry point for SoundBridge. Orchestrates server and client modes."""

import argparse
import logging
import sys
import threading
import time

import numpy as np

from . import config
from . import protocol
from .audio import AudioCapture, AudioPlayback, VirtualMicSource, find_pulse_monitor
from .network import UDPSender, UDPReceiver, Discovery, Heartbeat
from .state import ConnectionState

logger = logging.getLogger(__name__)


class SoundBridgeServer:
    """Server mode (Linux): captures system audio → sends to client.
    Receives mic from client → virtual PulseAudio source."""

    def __init__(self, gui_callback=None):
        self.peer_ip: str | None = None
        self._state = ConnectionState.DISCONNECTED
        self.gui_callback = gui_callback

        # Audio
        self._audio_capture: AudioCapture | None = None
        self._audio_sender: UDPSender | None = None
        self._mic_receiver: UDPReceiver | None = None
        self._mic_playback: AudioPlayback | None = None
        self._virtual_mic: VirtualMicSource | None = None

        # Network
        self._discovery: Discovery | None = None
        self._heartbeat: Heartbeat | None = None

        # Volume
        self._mic_volume = 1.0

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @connected.setter
    def connected(self, value: bool):
        self._state = ConnectionState.CONNECTED if value else ConnectionState.DISCONNECTED

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

        if self.gui_callback:
            self.gui_callback("connected", ip)

    def _start_streaming(self):
        # Setup heartbeat
        self._heartbeat = Heartbeat(
            target_ip=self.peer_ip,
            on_timeout=self._on_disconnect,
        )
        self._heartbeat.start_sender()
        self._heartbeat.start_monitor()

        # Capture system audio and send to client
        monitor_device = find_pulse_monitor()
        if monitor_device is None:
            logger.warning("No PulseAudio monitor found. "
                           "System audio capture unavailable.")
            logger.info("Tip: Make sure PulseAudio is running.")
        else:
            logger.info("Capturing system audio (device %s)", monitor_device)
            self._audio_sender = UDPSender(self.peer_ip, config.AUDIO_PORT)
            self._audio_capture = AudioCapture(
                callback=self._on_audio_captured,
                channels=config.CHANNELS_STEREO,
                device=monitor_device,
            )
            self._audio_capture.start()

        # Setup virtual mic source (Linux)
        self._virtual_mic = VirtualMicSource()
        self._virtual_mic.start()

        # Find the null-sink device to write mic audio into
        sink_idx = self._virtual_mic.get_sink_device_index()
        if sink_idx is not None:
            self._mic_playback = AudioPlayback(
                channels=config.CHANNELS_MONO,
                device=sink_idx,
            )
            self._mic_playback.start()
            logger.info("Virtual mic source created (device %s)", sink_idx)
        else:
            logger.warning("Could not find virtual mic sink. "
                           "Remote mic will not be available as input device.")

        # Receive mic audio from client
        self._mic_receiver = UDPReceiver(
            port=config.MIC_PORT,
            callback=self._on_mic_received,
        )
        self._mic_receiver.start()
        logger.info("Streaming active.")

    def _on_audio_captured(self, audio_data: np.ndarray):
        if self._audio_sender and self._state == ConnectionState.CONNECTED:
            self._audio_sender.send_audio(
                audio_data, config.PKT_AUDIO_DATA, config.CHANNELS_STEREO
            )

    def _on_mic_received(self, packet: protocol.Packet):
        if packet.pkt_type == config.PKT_MIC_DATA and self._mic_playback:
            audio = protocol.payload_to_audio(packet)
            if self._mic_volume != 1.0:
                audio = (audio.astype(np.float32) * self._mic_volume).astype(np.int16)
            self._mic_playback.feed(audio)

    def _on_disconnect(self):
        if self._state != ConnectionState.CONNECTED:
            return
        logger.info("Client disconnected (heartbeat timeout).")
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()

        if self.gui_callback:
            self.gui_callback("disconnected", None)

        # Restart discovery
        logger.info("Waiting for client...")
        self._state = ConnectionState.SEARCHING
        self._discovery = Discovery(on_peer_found=self._on_peer_found)
        self._discovery.start_listen()

    def stop_streaming(self):
        if self._audio_capture:
            self._audio_capture.stop()
            self._audio_capture = None
        if self._audio_sender:
            self._audio_sender.close()
            self._audio_sender = None
        if self._mic_receiver:
            self._mic_receiver.stop()
            self._mic_receiver = None
        if self._mic_playback:
            self._mic_playback.stop()
            self._mic_playback = None
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None

    def set_mic_volume(self, volume: float):
        self._mic_volume = max(0.0, min(1.0, volume))

    def stop(self):
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()
        if self._virtual_mic:
            self._virtual_mic.stop()
            self._virtual_mic = None
        if self._discovery:
            self._discovery.stop()
            self._discovery = None
        logger.info("Server stopped.")


class SoundBridgeClient:
    """Client mode (Windows): receives system audio → plays on headphone.
    Captures mic → sends to server."""

    def __init__(self, server_ip: str | None = None, gui_callback=None):
        self.server_ip = server_ip
        self._state = ConnectionState.DISCONNECTED
        self.gui_callback = gui_callback

        # Audio
        self._audio_receiver: UDPReceiver | None = None
        self._audio_playback: AudioPlayback | None = None
        self._mic_capture: AudioCapture | None = None
        self._mic_sender: UDPSender | None = None

        # Network
        self._discovery: Discovery | None = None
        self._heartbeat: Heartbeat | None = None

        # Volume
        self._audio_volume = 1.0

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @connected.setter
    def connected(self, value: bool):
        self._state = ConnectionState.CONNECTED if value else ConnectionState.DISCONNECTED

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

        if self.gui_callback:
            self.gui_callback("connected", ip)

    def _start_streaming(self):
        # Setup heartbeat
        self._heartbeat = Heartbeat(
            target_ip=self.server_ip,
            on_timeout=self._on_disconnect,
        )
        self._heartbeat.start_sender()
        self._heartbeat.start_monitor()

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
        logger.info("Receiving system audio → headphone")

        # Capture mic and send to server
        self._mic_sender = UDPSender(self.server_ip, config.MIC_PORT)
        self._mic_capture = AudioCapture(
            callback=self._on_mic_captured,
            channels=config.CHANNELS_MONO,
        )
        self._mic_capture.start()
        logger.info("Capturing mic → server")
        logger.info("Streaming active.")

    def _on_audio_received(self, packet: protocol.Packet):
        if packet.pkt_type == config.PKT_AUDIO_DATA and self._audio_playback:
            audio = protocol.payload_to_audio(packet)
            self._audio_playback.feed(audio)

    def _on_mic_captured(self, audio_data: np.ndarray):
        if self._mic_sender and self._state == ConnectionState.CONNECTED:
            self._mic_sender.send_audio(
                audio_data, config.PKT_MIC_DATA, config.CHANNELS_MONO
            )

    def _on_disconnect(self):
        if self._state != ConnectionState.CONNECTED:
            return
        logger.info("Server disconnected (heartbeat timeout).")
        self._state = ConnectionState.DISCONNECTED
        self.stop_streaming()

        if self.gui_callback:
            self.gui_callback("disconnected", None)

        # Restart discovery
        logger.info("Searching for server...")
        self._state = ConnectionState.SEARCHING
        self._discovery = Discovery(on_peer_found=self._on_server_found)
        self._discovery.start_search()

    def stop_streaming(self):
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
    server = SoundBridgeServer()
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.stop()


def run_client_cli(args):
    """Run client in CLI mode (no GUI)."""
    client = SoundBridgeClient(server_ip=args.ip)
    client.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        client.stop()


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

    # Client mode
    client_parser = subparsers.add_parser(
        "client", aliases=["--client"],
        help="Run as client (Windows — plays audio, captures mic)"
    )
    client_parser.add_argument(
        "--ip", type=str, default=None,
        help="Server IP address (skip auto-discovery)"
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
