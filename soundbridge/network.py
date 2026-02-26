"""UDP streaming, discovery, and heartbeat for SoundBridge."""

import socket
import threading
import time
from typing import Callable

import numpy as np

from . import config
from . import protocol


class UDPSender:
    """Sends audio packets over UDP."""

    def __init__(self, target_ip: str, port: int):
        self.target_ip = target_ip
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send_audio(self, audio_data: np.ndarray, pkt_type: int,
                   channels: int = config.CHANNELS_STEREO):
        """Send an audio frame as a UDP packet."""
        packet = protocol.encode(pkt_type, audio_data, channels)
        self.sock.sendto(packet, (self.target_ip, self.port))

    def close(self):
        self.sock.close()


class UDPReceiver:
    """Receives audio packets over UDP in a background thread."""

    def __init__(self, port: int, callback: Callable[[protocol.Packet], None]):
        self.port = port
        self.callback = callback
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(1.0)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        self.sock.bind(("0.0.0.0", self.port))
        self._running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def _receive_loop(self):
        buf_size = config.HEADER_SIZE + (config.FRAME_SIZE * config.CHANNELS_STEREO * config.BYTES_PER_SAMPLE) + 64
        while self._running:
            try:
                data, addr = self.sock.recvfrom(buf_size)
                packet = protocol.decode(data)
                if packet is not None:
                    self.callback(packet)
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self.sock.close()


class Discovery:
    """Discovers peer on the LAN via UDP broadcast."""

    def __init__(self, on_peer_found: Callable[[str], None]):
        self.on_peer_found = on_peer_found
        self._running = False
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._sock_send: socket.socket | None = None
        self._sock_recv: socket.socket | None = None

    def start_ping(self):
        """Start sending discovery pings (client mode)."""
        self._running = True
        self._sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        self._sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_recv.settimeout(1.0)
        self._sock_recv.bind(("0.0.0.0", config.DISCOVERY_PORT + 1))

        self._send_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._recv_thread = threading.Thread(target=self._listen_pong, daemon=True)
        self._send_thread.start()
        self._recv_thread.start()

    def _ping_loop(self):
        while self._running:
            pkt = protocol.encode(config.PKT_DISCOVERY_PING, channels=0, sample_rate=0)
            try:
                self._sock_send.sendto(pkt, (config.BROADCAST_ADDR, config.DISCOVERY_PORT))
            except OSError:
                pass
            time.sleep(config.DISCOVERY_INTERVAL)

    def _listen_pong(self):
        while self._running:
            try:
                data, addr = self._sock_recv.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_DISCOVERY_PONG:
                    self.on_peer_found(addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def start_listen(self):
        """Start listening for discovery pings (server mode)."""
        self._running = True
        self._sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_recv.settimeout(1.0)
        self._sock_recv.bind(("0.0.0.0", config.DISCOVERY_PORT))

        self._sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._recv_thread = threading.Thread(target=self._listen_ping_and_reply, daemon=True)
        self._recv_thread.start()

    def _listen_ping_and_reply(self):
        while self._running:
            try:
                data, addr = self._sock_recv.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_DISCOVERY_PING:
                    pong = protocol.encode(config.PKT_DISCOVERY_PONG, channels=0, sample_rate=0)
                    self._sock_send.sendto(pong, (addr[0], config.DISCOVERY_PORT + 1))
                    self.on_peer_found(addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
        if self._send_thread:
            self._send_thread.join(timeout=2.0)
        if self._recv_thread:
            self._recv_thread.join(timeout=2.0)
        if self._sock_send:
            self._sock_send.close()
        if self._sock_recv:
            self._sock_recv.close()


class Heartbeat:
    """Sends and monitors heartbeat packets."""

    def __init__(self, target_ip: str | None = None,
                 on_timeout: Callable[[], None] | None = None):
        self.target_ip = target_ip
        self.on_timeout = on_timeout
        self._last_received = time.time()
        self._running = False
        self._send_thread: threading.Thread | None = None
        self._check_thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start_sender(self):
        """Start sending heartbeats."""
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()

    def _send_loop(self):
        while self._running:
            if self.target_ip:
                pkt = protocol.encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0)
                try:
                    self._sock.sendto(pkt, (self.target_ip, config.HEARTBEAT_PORT))
                except OSError:
                    pass
            time.sleep(config.HEARTBEAT_INTERVAL)

    def start_monitor(self):
        """Start monitoring for heartbeats (receiver side)."""
        self._running = True
        self._last_received = time.time()

        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        recv_sock.settimeout(1.0)
        recv_sock.bind(("0.0.0.0", config.HEARTBEAT_PORT))

        self._recv_thread = threading.Thread(
            target=self._monitor_loop, args=(recv_sock,), daemon=True
        )
        self._check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self._recv_thread.start()
        self._check_thread.start()

    def _monitor_loop(self, sock: socket.socket):
        while self._running:
            try:
                data, addr = sock.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_HEARTBEAT:
                    self._last_received = time.time()
            except socket.timeout:
                continue
            except OSError:
                break
        sock.close()

    def _check_loop(self):
        while self._running:
            if time.time() - self._last_received > config.HEARTBEAT_TIMEOUT:
                if self.on_timeout:
                    self.on_timeout()
            time.sleep(1.0)

    def record_heartbeat(self):
        """Call when a heartbeat is received externally."""
        self._last_received = time.time()

    def stop(self):
        self._running = False
        if self._send_thread:
            self._send_thread.join(timeout=2.0)
        if self._check_thread:
            self._check_thread.join(timeout=2.0)
        if hasattr(self, '_recv_thread') and self._recv_thread:
            self._recv_thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
