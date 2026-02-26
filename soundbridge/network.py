"""UDP streaming, discovery, and heartbeat for SoundBridge."""

import logging
import socket
import struct
import sys
import threading
import time
from typing import Callable

import numpy as np

from . import config
from . import protocol

logger = logging.getLogger(__name__)


def _get_broadcast_addresses() -> list[str]:
    """Detect broadcast addresses for all active network interfaces.

    Uses OS-specific methods: netifaces/fcntl on Linux, ipconfig parsing
    on Windows.  Falls back to 255.255.255.255 if detection fails.
    """
    addrs: list[str] = []

    if sys.platform == "linux":
        try:
            import fcntl
            import array

            # SIOCGIFCONF — get list of interfaces
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Ask for up to 4096 bytes of interface data
            buf = array.array("B", b"\0" * 4096)
            result = fcntl.ioctl(
                sock.fileno(),
                0x8912,  # SIOCGIFCONF
                struct.pack("iL", 4096, buf.buffer_info()[0]),
            )
            out_bytes = struct.unpack("iL", result)[0]
            data = buf.tobytes()[:out_bytes]

            offset = 0
            while offset < len(data):
                iface_name = data[offset : offset + 16].split(b"\0", 1)[0]
                offset += 16 + 16  # skip name + sockaddr

                if not iface_name or iface_name == b"lo":
                    continue

                try:
                    # SIOCGIFBRDADDR — get broadcast address
                    req = struct.pack("256s", iface_name)
                    res = fcntl.ioctl(sock.fileno(), 0x8919, req)
                    bcast_ip = socket.inet_ntoa(res[20:24])
                    if bcast_ip and bcast_ip != "0.0.0.0":
                        addrs.append(bcast_ip)
                except OSError:
                    continue

            sock.close()
        except Exception:
            pass

    elif sys.platform == "win32":
        try:
            import subprocess
            output = subprocess.check_output(
                ["powershell", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | "
                 "Where-Object { $_.PrefixOrigin -ne 'WellKnown' } | "
                 "Select-Object IPAddress, PrefixLength | "
                 "Format-Table -HideTableHeaders"],
                text=True, timeout=5,
            )
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    ip_str, prefix = parts[0], int(parts[1])
                    ip_int = struct.unpack("!I", socket.inet_aton(ip_str))[0]
                    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                    bcast_int = ip_int | (~mask & 0xFFFFFFFF)
                    bcast_ip = socket.inet_ntoa(struct.pack("!I", bcast_int))
                    if bcast_ip != "255.255.255.255" and ip_str != "127.0.0.1":
                        addrs.append(bcast_ip)
        except Exception:
            pass

    # Also try the cross-platform socket approach as fallback
    if not addrs:
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            if local_ip and not local_ip.startswith("127."):
                # Assume /24 subnet as common default
                parts = local_ip.split(".")
                parts[3] = "255"
                addrs.append(".".join(parts))
        except Exception:
            pass

    if not addrs:
        addrs.append("255.255.255.255")

    return list(set(addrs))


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
    """Discovers peer on the LAN via UDP subnet broadcast."""

    def __init__(self, on_peer_found: Callable[[str], None]):
        self.on_peer_found = on_peer_found
        self._running = False
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._sock_send: socket.socket | None = None
        self._sock_recv: socket.socket | None = None
        self._broadcast_addrs: list[str] = []

    def start_ping(self):
        """Start sending discovery pings (client mode)."""
        self._broadcast_addrs = _get_broadcast_addresses()
        logger.info("Discovery: sending PINGs to %s (port %d)",
                     self._broadcast_addrs, config.DISCOVERY_PORT)

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
        ping_count = 0
        while self._running:
            pkt = protocol.encode(config.PKT_DISCOVERY_PING, channels=0, sample_rate=0)
            for bcast in self._broadcast_addrs:
                try:
                    self._sock_send.sendto(pkt, (bcast, config.DISCOVERY_PORT))
                except OSError as e:
                    logger.debug("Discovery: failed to send PING to %s: %s", bcast, e)
            ping_count += 1
            if ping_count % 5 == 0:
                logger.info("Discovery: sent %d rounds of PINGs, still searching...",
                             ping_count)
            time.sleep(config.DISCOVERY_INTERVAL)

    def _listen_pong(self):
        while self._running:
            try:
                data, addr = self._sock_recv.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_DISCOVERY_PONG:
                    logger.info("Discovery: received PONG from %s", addr[0])
                    self.on_peer_found(addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def start_listen(self):
        """Start listening for discovery pings (server mode)."""
        logger.info("Discovery: listening for PINGs on port %d",
                     config.DISCOVERY_PORT)

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
                    logger.info("Discovery: received PING from %s, sending PONG",
                                 addr[0])
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
        current = threading.current_thread()
        if self._send_thread and self._send_thread is not current:
            self._send_thread.join(timeout=2.0)
        if self._check_thread and self._check_thread is not current:
            self._check_thread.join(timeout=2.0)
        if hasattr(self, '_recv_thread') and self._recv_thread and self._recv_thread is not current:
            self._recv_thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
