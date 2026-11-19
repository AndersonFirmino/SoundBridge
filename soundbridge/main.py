"""Entry point for SoundBridge. Orchestrates server and client modes."""

import argparse
import logging
import sys
import threading
import time

import numpy as np

from . import config
from . import protocol
from .audio import (
    AudioCapture, AudioPlayback, ParecCapture,
    VirtualMicSource, WasapiLoopbackCapture, find_monitor_source,
)
from .network import UDPSender, UDPReceiver, Discovery, Heartbeat
from .opus import OpusEncoder, OpusDecoder
from .state import ConnectionState

logger = logging.getLogger(__name__)


def _begin_discovery(owner, generation, callback, *, server=False, **search_args):
    # Construction may enumerate interfaces and block. Publish only if the
    # operation still owns this generation when construction finishes.
    with owner._discovery_lock:
        if generation != owner._search_generation:
            return
    discovery = Discovery(on_peer_found=lambda ip: callback(ip, generation))
    with owner._discovery_lock:
        if generation == owner._search_generation:
            owner._state = ConnectionState.SEARCHING
            owner._discovery = discovery
            if server:
                discovery.start_listen()
            else:
                discovery.start_search(**search_args)
            return
    discovery.stop()


def _invalidate_discovery(owner, expected=None):
    with owner._discovery_lock:
        if expected is not None and (expected != owner._search_generation or not owner.connected):
            return None
        with owner._stream_lock:
            owner._search_generation += 1
            resources = _detach_stream_resources(owner)
            # A new start is allowed only after all old refs were detached.
            owner._state = ConnectionState.DISCONNECTED
            return owner._search_generation, resources, owner._discovery


def _cancel_discovery(owner, discovery):
    if discovery:
        discovery.stop()
    with owner._discovery_lock:
        if owner._discovery is discovery:
            owner._discovery = None


def _stream_callback(owner, callback):
    generation = owner._stream_generation
    def guarded(*args):
        with owner._stream_lock:
            if (owner._stream_generation == generation
                    and owner._search_generation == generation and owner.connected):
                callback(*args)
    return guarded


def _detach_stream_resources(owner, generation=None):
    """Swap refs under the stream lock; invalidation also holds lifecycle lock."""
    with owner._stream_lock:
        if generation is not None and generation != owner._stream_generation:
            return []
        resources = [(getattr(owner, name), method) for name, method in owner._STREAM_RESOURCES]
        for name, _ in owner._STREAM_RESOURCES:
            setattr(owner, name, None)
        owner._stream_generation = None
        return resources


def _close_stream_resources(resources):
    for resource, method in resources:
        if resource is not None:
            try:
                getattr(resource, method)()
            except Exception:
                logger.exception("Could not release stream resource (%s)", method)


class SoundBridgeServer:
    """Server mode (Linux/Windows): captures system audio -> sends to client.
    Linux only: receives mic from client -> virtual PulseAudio source."""

    _STREAM_RESOURCES = (
        ("_audio_capture", "stop"), ("_audio_sender", "close"),
        ("_mic_receiver", "stop"), ("_virtual_mic", "stop"),
        ("_heartbeat", "stop"), ("_audio_encoder", "destroy"),
        ("_mic_decoder", "destroy"),
    )

    def __init__(self, gui_callback=None):
        self.peer_ip: str | None = None
        self._discovery_lock = threading.RLock()
        self._search_generation = 0
        self._connected_generation = 0
        self._stream_generation: int | None = None
        self._stream_lock = threading.RLock()
        self._state = ConnectionState.DISCONNECTED
        self.gui_callback = gui_callback

        # Audio
        self._audio_capture: ParecCapture | WasapiLoopbackCapture | None = None
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
        if self._discovery or self.connected or self._stream_generation is not None:
            self.stop()
        with self._discovery_lock:
            self._search_generation += 1
            generation = self._search_generation
            self._state = ConnectionState.SEARCHING
        _begin_discovery(self, generation, self._on_peer_found, server=True)

    def _on_peer_found(self, ip: str, generation: int | None = None):
        generation = self._search_generation if generation is None else generation
        if generation != self._search_generation or self._state != ConnectionState.SEARCHING:
            return

        self.peer_ip = ip
        self._state = ConnectionState.CONNECTED
        logger.info("Client found: %s", ip)
        discovery = self._discovery
        try:
            if discovery:
                discovery.stop()
            if generation != self._search_generation:
                return
            self._connected_generation = generation
            self._start_streaming()
            if generation == self._search_generation and self.gui_callback:
                self.gui_callback("connected", ip)
        finally:
            if self._discovery is discovery:
                self._discovery = None

    def _start_streaming(self):
        with self._stream_lock:
            if self._connected_generation != self._search_generation:
                return
            self._stream_generation = self._connected_generation
            self._open_streams()

    def _open_streams(self):
        # Setup heartbeat
        generation = self._connected_generation
        self._heartbeat = Heartbeat(
            target_ip=self.peer_ip,
            on_timeout=lambda: self._on_disconnect(generation),
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

        if sys.platform == "win32":
            self._audio_sender = UDPSender(self.peer_ip, config.AUDIO_PORT)
            self._audio_capture = WasapiLoopbackCapture(
                callback=_stream_callback(self, self._on_audio_captured),
                channels=config.CHANNELS_STEREO,
            )
            self._audio_capture.start()
            logger.info("Capturing system audio (WASAPI loopback)")
        elif sys.platform == "linux":
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
                    callback=_stream_callback(self, self._on_audio_captured),
                    channels=config.CHANNELS_STEREO,
                    device_name=monitor_source,
                )
                self._audio_capture.start()

            if self._virtual_mic.active:
                logger.info("Virtual mic source created (pipe-source)")
            else:
                logger.warning("Could not create virtual mic. "
                               "Remote mic will not be available as input device.")
        else:
            logger.warning("System audio capture not supported on %s",
                           sys.platform)

        if sys.platform == "linux":
            # Receive mic audio from client
            self._mic_receiver = UDPReceiver(
                port=config.MIC_PORT,
                callback=_stream_callback(self, self._on_mic_received),
            )
            self._mic_receiver.start()
        else:
            logger.info("Remote mic not supported on this platform; skipping")
        logger.info("Streaming active (Opus codec enabled).")

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

    def _on_disconnect(self, generation: int | None = None):
        generation = self._search_generation if generation is None else generation
        operation = _invalidate_discovery(self, generation)
        if operation is None:
            return
        generation, resources, discovery = operation
        _cancel_discovery(self, discovery)
        _close_stream_resources(resources)
        logger.info("Client disconnected (heartbeat timeout).")
        if generation != self._search_generation:
            return

        if self.gui_callback:
            self.gui_callback("disconnected", None)

        # Restart discovery
        if generation != self._search_generation:
            return
        logger.info("Waiting for client...")
        _begin_discovery(self, generation, self._on_peer_found, server=True)

    def stop_streaming(self, generation: int | None = None):
        _close_stream_resources(_detach_stream_resources(self, generation))

    def set_mic_volume(self, volume: float):
        self._mic_volume = max(0.0, min(1.0, volume))

    def stop(self):
        generation, resources, discovery = _invalidate_discovery(self)
        _cancel_discovery(self, discovery)
        _close_stream_resources(resources)
        with self._discovery_lock:
            if generation == self._search_generation:
                self._state = ConnectionState.DISCONNECTED
                logger.info("Server stopped.")


class SoundBridgeClient:
    """Client mode (Windows/macOS/Linux): receives system audio -> plays on headphone.
    Optionally captures mic -> sends to server."""

    _STREAM_RESOURCES = (
        ("_audio_receiver", "stop"), ("_audio_playback", "stop"),
        ("_mic_capture", "stop"), ("_mic_sender", "close"),
        ("_heartbeat", "stop"), ("_audio_decoder", "destroy"),
        ("_mic_encoder", "destroy"),
    )

    def __init__(self, server_ip: str | None = None, gui_callback=None,
                 mic_enabled: bool = True):
        self.server_ip = server_ip
        self._explicit_server_ip = server_ip
        self._discovery_lock = threading.RLock()
        self._search_generation = 0
        self._connected_generation = 0
        self._stream_generation: int | None = None
        self._stream_lock = threading.RLock()
        self.mic_enabled = mic_enabled
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

        # Diagnostics
        self._stat_packets = 0
        self._stat_plc = 0
        self._stat_last_log = time.monotonic()

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
        if self._discovery or self.connected or self._stream_generation is not None:
            self.stop()
        with self._discovery_lock:
            self._search_generation += 1
            generation = self._search_generation
            self._state = ConnectionState.SEARCHING
        if self.server_ip:
            logger.info("Connecting to %s...", self.server_ip)
            _begin_discovery(self, generation, self._on_server_found,
                             direct_ip=self.server_ip, browse=False)
        else:
            logger.info("Searching for server on LAN...")
            _begin_discovery(self, generation, self._on_server_found)

    def _on_server_found(self, ip: str, generation: int | None = None):
        generation = self._search_generation if generation is None else generation
        if generation != self._search_generation or self._state != ConnectionState.SEARCHING:
            return

        self.server_ip = ip
        self._state = ConnectionState.CONNECTED
        logger.info("Server found: %s", ip)
        discovery = self._discovery
        try:
            if discovery:
                discovery.stop()
            if generation != self._search_generation:
                return
            self._connected_generation = generation
            self._start_streaming()
            if generation == self._search_generation and self.gui_callback:
                self.gui_callback("connected", ip)
        finally:
            # Keep the owner reachable until setup ends: concurrent stop()
            # cancels/joins its serialized callback before clearing streams.
            if self._discovery is discovery:
                self._discovery = None

    def _start_streaming(self):
        with self._stream_lock:
            if self._connected_generation != self._search_generation:
                return
            self._stream_generation = self._connected_generation
            self._open_streams()

    def _open_streams(self):
        # Setup heartbeat
        generation = self._connected_generation
        self._heartbeat = Heartbeat(
            target_ip=self.server_ip,
            on_timeout=lambda: self._on_disconnect(generation),
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
        self._stat_packets = 0
        self._stat_plc = 0
        self._stat_last_log = time.monotonic()

        # Receive system audio from server and play it
        self._audio_playback = AudioPlayback(
            channels=config.CHANNELS_STEREO,
        )
        self._audio_playback.start()

        self._audio_receiver = UDPReceiver(
            port=config.AUDIO_PORT,
            callback=_stream_callback(self, self._on_audio_received),
            expected_ip=self.server_ip,
        )
        self._audio_receiver.start()
        logger.info("Receiving system audio -> headphone (Opus)")

        # Capture mic and send to server
        if not self.mic_enabled:
            logger.info("Mic capture disabled (--no-mic)")
        else:
            try:
                self._mic_sender = UDPSender(self.server_ip, config.MIC_PORT)
                self._mic_capture = AudioCapture(
                    callback=_stream_callback(self, self._on_mic_captured),
                    channels=config.CHANNELS_MONO,
                )
                self._mic_capture.start()
                logger.info("Capturing mic -> server (Opus)")
            except Exception as e:
                logger.warning("Mic capture unavailable: %s; continuing without mic", e)
                if self._mic_capture is not None:
                    try:
                        self._mic_capture.stop()
                    except Exception:
                        logger.exception("Could not clean up failed mic capture")
                    finally:
                        self._mic_capture = None
                if self._mic_sender:
                    self._mic_sender.close()
                self._mic_sender = None
        logger.info("Streaming active (Opus codec enabled).")

    def _on_audio_received(self, packet: protocol.Packet):
        if packet.pkt_type != config.PKT_AUDIO_DATA or not self._audio_playback:
            return

        now = time.monotonic()
        self._stat_packets += 1

        # Detect gaps and apply PLC
        if self._audio_last_seq is not None:
            expected = (self._audio_last_seq + 1) % 65536
            gap = (packet.seq - expected) % 65536
            if 0 < gap < 100:
                logger.debug("PLC: audio gap of %d packets", gap)
                for _ in range(gap):
                    plc_frame = self._audio_decoder.plc(config.FRAME_SIZE)
                    self._audio_playback.feed(plc_frame)
                    self._stat_plc += 1
        self._audio_last_seq = packet.seq

        elapsed = now - self._stat_last_log
        if elapsed >= 5.0:
            logger.info("Audio: %d packets, %d PLC frames in last %.1fs",
                        self._stat_packets, self._stat_plc, elapsed)
            self._stat_packets = 0
            self._stat_plc = 0
            self._stat_last_log = now

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

    def _on_disconnect(self, generation: int | None = None):
        generation = self._search_generation if generation is None else generation
        operation = _invalidate_discovery(self, generation)
        if operation is None:
            return
        generation, resources, discovery = operation
        _cancel_discovery(self, discovery)
        _close_stream_resources(resources)
        logger.info("Server disconnected (heartbeat timeout).")
        if generation != self._search_generation:
            return

        if self.gui_callback:
            self.gui_callback("disconnected", None)

        # Preserve mDNS recovery if the original address changes, while also
        # probing an explicit IP on networks where multicast is unavailable.
        # A probe is not a connection: validate the effective server endpoint.
        if generation != self._search_generation:
            return
        logger.info("Searching for server...")
        _begin_discovery(self, generation, self._on_server_found,
                         direct_ip=self._explicit_server_ip)

    def stop_streaming(self, generation: int | None = None):
        _close_stream_resources(_detach_stream_resources(self, generation))

    def set_audio_volume(self, volume: float):
        self._audio_volume = max(0.0, min(1.0, volume))
        if self._audio_playback:
            self._audio_playback.set_volume(self._audio_volume)

    def stop(self):
        generation, resources, discovery = _invalidate_discovery(self)
        _cancel_discovery(self, discovery)
        _close_stream_resources(resources)
        with self._discovery_lock:
            if generation == self._search_generation:
                self._state = ConnectionState.DISCONNECTED
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
    client = SoundBridgeClient(server_ip=args.ip,
                                 mic_enabled=not args.no_mic)
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
        help="Run as server (captures system audio: Linux via PulseAudio/PipeWire, Windows via WASAPI loopback)"
    )

    # Client mode
    client_parser = subparsers.add_parser(
        "client", aliases=["--client"],
        help="Run as client (plays received audio; Windows/macOS/Linux)"
    )
    client_parser.add_argument(
        "--ip", type=str, default=None,
        help="Server IP address (skip auto-discovery)"
    )
    client_parser.add_argument(
        "--no-mic", action="store_true",
        help="Do not capture/send microphone"
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
