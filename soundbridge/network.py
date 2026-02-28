"""UDP streaming, discovery, and heartbeat for SoundBridge."""

import logging
import socket
import threading
import time
from typing import Callable

import numpy as np
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

from . import config
from . import protocol

logger = logging.getLogger(__name__)


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


def _get_local_ip() -> str:
    """Get the local IP address used for LAN communication."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Discovery:
    """Discovers peer on the LAN via mDNS/zeroconf."""

    def __init__(self, on_peer_found: Callable[[str], None]):
        self.on_peer_found = on_peer_found
        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._service_info: ServiceInfo | None = None
        self._running = False
        self._connect_sock: socket.socket | None = None
        self._listen_thread: threading.Thread | None = None

    def start_listen(self):
        """Register service via mDNS and wait for client heartbeat (server mode)."""
        local_ip = _get_local_ip()
        logger.info("Discovery: registering mDNS service (ip=%s)", local_ip)

        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._service_info = ServiceInfo(
            config.ZEROCONF_SERVICE_TYPE,
            config.ZEROCONF_SERVICE_NAME,
            parsed_addresses=[local_ip],
            port=config.AUDIO_PORT,
            properties={"version": "1"},
        )
        self._zeroconf.register_service(self._service_info)
        logger.info("Discovery: service registered, waiting for client heartbeat...")

        # Listen for incoming heartbeats to detect when a client connects
        self._running = True
        self._connect_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._connect_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._connect_sock.settimeout(1.0)
        self._connect_sock.bind(("0.0.0.0", config.HEARTBEAT_PORT))
        self._listen_thread = threading.Thread(
            target=self._listen_for_connect, daemon=True
        )
        self._listen_thread.start()

    def _listen_for_connect(self):
        """Wait for the first heartbeat from a client."""
        while self._running:
            try:
                data, addr = self._connect_sock.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_HEARTBEAT:
                    logger.info("Discovery: client heartbeat from %s", addr[0])
                    threading.Thread(
                        target=self.on_peer_found, args=(addr[0],),
                        daemon=True,
                    ).start()
                    return
            except socket.timeout:
                continue
            except OSError:
                break

    def start_search(self):
        """Browse for mDNS services (client mode)."""
        logger.info("Discovery: browsing for SoundBridge services...")
        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._browser = ServiceBrowser(
            self._zeroconf,
            config.ZEROCONF_SERVICE_TYPE,
            handlers=[self._on_state_change],
        )

    def _on_state_change(self, zeroconf: Zeroconf, service_type: str,
                         name: str, state_change: ServiceStateChange):
        if state_change != ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses:
            return
        ip = addresses[0]
        logger.info("Discovery: found service '%s' at %s", name, ip)
        # Run callback outside the ServiceBrowser thread so that
        # stop() -> cancel() doesn't try to join the current thread.
        threading.Thread(target=self.on_peer_found, args=(ip,), daemon=True).start()

    def stop(self):
        self._running = False
        if self._browser:
            self._browser.cancel()
            self._browser = None
        if self._connect_sock:
            self._connect_sock.close()
            self._connect_sock = None
        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
            self._listen_thread = None
        if self._service_info and self._zeroconf:
            self._zeroconf.unregister_service(self._service_info)
            self._service_info = None
        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None


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

        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._recv_sock.settimeout(1.0)
        try:
            self._recv_sock.bind(("0.0.0.0", config.HEARTBEAT_PORT))
        except OSError as e:
            logger.error("Heartbeat: failed to bind port %d: %s",
                         config.HEARTBEAT_PORT, e)
            self._recv_sock.close()
            self._recv_sock = None
            return

        self._recv_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self._recv_thread.start()
        self._check_thread.start()

    def _monitor_loop(self):
        while self._running:
            try:
                data, addr = self._recv_sock.recvfrom(128)
                pkt = protocol.decode(data)
                if pkt and pkt.pkt_type == config.PKT_HEARTBEAT:
                    self._last_received = time.time()
            except socket.timeout:
                continue
            except OSError:
                break

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
        if hasattr(self, '_recv_sock') and self._recv_sock:
            self._recv_sock.close()
