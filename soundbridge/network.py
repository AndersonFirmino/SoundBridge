"""UDP streaming, discovery, and heartbeat for SoundBridge."""

from dataclasses import dataclass
import ipaddress
import logging
import secrets
import select
import socket
import threading
import time
from typing import Callable

import ifaddr
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

    def send_audio(self, payload: bytes, pkt_type: int,
                   channels: int = config.CHANNELS_STEREO, seq: int = 0):
        """Send an encoded audio payload as a UDP packet."""
        packet = protocol.encode(pkt_type, payload, channels, seq=seq)
        self.sock.sendto(packet, (self.target_ip, self.port))

    def close(self):
        self.sock.close()


class UDPReceiver:
    """Receives audio packets over UDP in a background thread."""

    def __init__(self, port: int, callback: Callable[[protocol.Packet], None],
                 expected_ip: str | None = None):
        self.port = port
        self.callback = callback
        self.expected_ip = socket.gethostbyname(expected_ip) if expected_ip else None
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
        buf_size = config.HEADER_SIZE + 1024  # Opus max ~1275 bytes
        while self._running:
            try:
                data, addr = self.sock.recvfrom(buf_size)
                if self.expected_ip is not None and addr[0] != self.expected_ip:
                    continue
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


def _get_default_route_ip() -> str | None:
    """Keep the historical primary address for clients that read only one A record."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # Route lookup only; no packet is sent.
            return sock.getsockname()[0]
    except OSError:
        return None


def _get_local_addresses() -> list[str]:
    """Advertise every IPv4 interface, not only the Internet default route."""
    addresses = {
        ip.ip
        for adapter in ifaddr.get_adapters()
        for ip in adapter.ips
        if ip.is_IPv4 and not ipaddress.ip_address(ip.ip).is_loopback
        and not ipaddress.ip_address(ip.ip).is_unspecified
    }
    primary = _get_default_route_ip()
    return sorted(addresses, key=lambda ip: (ip != primary, ip)) or ["127.0.0.1"]


def _address_properties(addresses: list[str]) -> dict[str, str]:
    """Split interface addresses across DNS TXT's 255-byte character strings."""
    properties = {}
    key = "addresses"
    value = ""
    for ip in addresses:
        combined = f"{value},{ip}" if value else ip
        if len(key) + 1 + len(combined) > 255:
            properties[key] = value
            key = f"addresses-{len(properties)}"
            value = ip
        else:
            value = combined
    properties[key] = value
    return properties


@dataclass
class _Probe:
    nonce: bytes
    expires: float
    first_sent: float


@dataclass
class _LegacyAttempt:
    ip: str
    services: set[str]
    direct: bool
    expires: float
    release_at: float
    valid: bool = True


class Discovery:
    """Discover peers with passive probes, then commit only the selected peer."""

    def __init__(self, on_peer_found: Callable[[str], None]):
        self.on_peer_found = on_peer_found
        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._service_info: ServiceInfo | None = None
        self._running = False
        self._connect_sock: socket.socket | None = None
        self._listen_thread: threading.Thread | None = None
        self._search_sockets: tuple[socket.socket, ...] = ()
        self._probe_thread: threading.Thread | None = None
        self._resolver_thread: threading.Thread | None = None
        self._peer_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._cancel = threading.Event()
        self._browse_addresses: list[str] | None = None
        self._advertised_addresses: list[str] | None = None
        self._local_ips = set(_get_local_addresses())
        self._direct_ips: set[str] = set()
        self._service_addresses: dict[str, set[str]] = {}
        self._modern_services: set[str] = set()
        self._known_modern_ips: set[str] = set()
        self._pending_probes: dict[str, _Probe] = {}
        self._legacy_attempt: _LegacyAttempt | None = None

    @property
    def _candidate_ips(self) -> set[str]:
        return self._direct_ips.union(*self._service_addresses.values()) - self._local_ips

    @property
    def _modern_ips(self) -> set[str]:
        return (self._known_modern_ips & self._direct_ips).union(*(
            self._service_addresses[name] for name in self._modern_services
        )) & self._candidate_ips

    def _prune_candidates(self):
        """Caller holds the lock: retired addresses authorize no modern reply."""
        candidates = self._candidate_ips
        self._pending_probes = {
            ip: probe for ip, probe in self._pending_probes.items() if ip in candidates
        }
        self._known_modern_ips.intersection_update(candidates)

    def _active(self, generation: int) -> bool:
        return (generation == self._generation and self._running
                and not self._cancel.is_set())

    def _new_generation(self):
        """Lifecycle lock is held; no state belongs to a previous operation."""
        with self._peer_lock:
            self._generation += 1
            self._cancel = threading.Event()
            self._running = True
            self._direct_ips.clear()
            self._service_addresses.clear()
            self._modern_services.clear()
            self._known_modern_ips.clear()
            self._pending_probes.clear()
            self._legacy_attempt = None
        return self._generation

    def _refresh_identity(self, generation: int) -> list[str] | None:
        if not self._active(generation):
            return None
        addresses = _get_local_addresses()
        with self._peer_lock:
            if not self._active(generation):
                return None
            self._local_ips = set(addresses)
            self._prune_candidates()
            if self._legacy_attempt is not None and self._legacy_attempt.ip in self._local_ips:
                self._legacy_attempt.valid = False
        return addresses

    def _notify_peer(self, ip: str, generation: int):
        # Delivery and start/stop are serialized. A callback may call stop()
        # reentrantly; no peer lock or I/O worker is held during delivery.
        with self._lifecycle_lock:
            if self._active(generation):
                self.on_peer_found(ip)

    def _register_service(self, addresses: list[str]):
        logger.info("Discovery: registering mDNS service (addresses=%s)", addresses)
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        info = ServiceInfo(
            config.ZEROCONF_SERVICE_TYPE, config.ZEROCONF_SERVICE_NAME,
            # DNS A ordering is not stable. Preserve old addresses[0] clients.
            parsed_addresses=addresses[:1], port=config.AUDIO_PORT,
            properties={"version": "1", "discovery": "probe-v1",
                        **_address_properties(addresses)},
        )
        try:
            zc.register_service(info)
        except Exception:
            zc.close()
            raise
        self._zeroconf, self._service_info = zc, info
        self._advertised_addresses = list(addresses)

    def _refresh_advertisement(self, generation: int, addresses: list[str] | None = None):
        addresses = addresses if addresses is not None else self._refresh_identity(generation)
        if addresses is None or (addresses == self._advertised_addresses
                                 and self._zeroconf is not None):
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Never make shutdown join a worker waiting for this lock.
        try:
            if self._active(generation) and self._advertised_addresses is not None:
                old_zc, old_info = self._zeroconf, self._service_info
                self._zeroconf = self._service_info = None
                if old_zc:
                    try:
                        old_zc.unregister_service(old_info)
                    finally:
                        old_zc.close()
                # Zeroconf's multicast sockets also snapshot interfaces, so a
                # TXT update alone would not join a newly available network.
                self._register_service(addresses)
        except Exception as exc:
            logger.warning("Discovery: advertisement refresh retry: %s", exc)
            # Keep the last advertised address list: the next round retries.
        finally:
            self._lifecycle_lock.release()

    def start_listen(self):
        """Register mDNS and wait for a client heartbeat (server mode)."""
        with self._lifecycle_lock:
            self.stop()
            generation = self._new_generation()
            addresses = self._refresh_identity(generation)
            self._register_service(addresses)
            self._connect_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._connect_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._connect_sock.settimeout(1.0)
            self._connect_sock.bind(("0.0.0.0", config.HEARTBEAT_PORT))
            self._listen_thread = threading.Thread(target=self._listen_for_connect,
                                                   args=(generation,), daemon=True)
            self._listen_thread.start()
            logger.info("Discovery: service registered, waiting for client heartbeat...")

    def _listen_for_connect(self, generation: int | None = None):
        """Answer passive probes; only heartbeats commit an audio stream."""
        generation = self._generation if generation is None else generation
        sock = self._connect_sock
        if sock is None:
            return
        while self._active(generation):
            try:
                data, addr = sock.recvfrom(128)
                packet = protocol.decode(data)
                if packet is None:
                    continue
                addresses = self._refresh_identity(generation)
                if addresses is None:
                    return
                local = addr[0] in self._local_ips or ipaddress.ip_address(addr[0]).is_loopback
                if packet.pkt_type == config.PKT_DISCOVERY_REQUEST:
                    if len(packet.payload) == 16 and not local:
                        sock.sendto(protocol.encode(config.PKT_DISCOVERY_RESPONSE,
                                    packet.payload, channels=0, sample_rate=0), addr)
                elif packet.pkt_type == config.PKT_HEARTBEAT:
                    if packet.payload and local:
                        continue  # Legacy probe via NAT hairpin, not a remote client.
                    logger.info("Discovery: client heartbeat from %s", addr[0])
                    threading.Thread(target=self._notify_peer, args=(addr[0], generation),
                                     daemon=True).start()
                    return
                self._refresh_advertisement(generation, addresses)
            except socket.timeout:
                self._refresh_advertisement(generation)
            except OSError as exc:
                if not self._active(generation):
                    break
                # Windows can deliver ICMP for an abandoned ephemeral probe
                # port as reset 10054 on the next receive. Keep listening.
                logger.debug("Discovery: listener retry: %s", exc)

    def _open_browser(self, generation: int, addresses: list[str]):
        old_browser, old_zc = self._browser, self._zeroconf
        self._browser = self._zeroconf = None
        try:
            if old_browser:
                old_browser.cancel()
        finally:
            if old_zc:
                old_zc.close()
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        def handler(zeroconf, service_type, name, state_change):
            self._on_state_change(zeroconf, service_type, name, state_change, generation)
        try:
            browser = ServiceBrowser(zc, config.ZEROCONF_SERVICE_TYPE, handlers=[handler])
        except Exception:
            zc.close()
            raise
        self._zeroconf, self._browser = zc, browser
        self._browse_addresses = list(addresses)

    def _refresh_browser(self, generation: int, addresses: list[str]):
        if self._browse_addresses is None or (addresses == self._browse_addresses
                                             and self._zeroconf is not None
                                             and self._browser is not None):
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if self._active(generation):
                self._open_browser(generation, addresses)
        except Exception as exc:
            logger.warning("Discovery: browser refresh retry: %s", exc)
        finally:
            self._lifecycle_lock.release()

    def start_search(self, direct_ip: str | None = None, browse: bool = True):
        """One I/O worker; native DNS runs independently and cannot delay stop."""
        with self._lifecycle_lock:
            self.stop()
            generation = self._new_generation()
            addresses = self._refresh_identity(generation)
            if browse:
                logger.info("Discovery: browsing for SoundBridge services...")
                self._open_browser(generation, addresses)
            if direct_ip:
                self._resolver_thread = threading.Thread(target=self._resolve_direct,
                    args=(direct_ip, generation, self._cancel), daemon=True)
                self._resolver_thread.start()
            self._probe_thread = threading.Thread(target=self._probe_candidates,
                                                  args=(generation,), daemon=True)
            self._probe_thread.start()

    def _apply_direct(self, ip: str, generation: int):
        with self._peer_lock:
            if not self._active(generation):
                return
            self._direct_ips = {ip}
            attempt = self._legacy_attempt
            if attempt is not None:
                attempt.direct = attempt.ip == ip
                if not attempt.direct and not attempt.services:
                    attempt.valid = False
            self._prune_candidates()

    def _resolve_direct(self, host: str, generation: int, cancel: threading.Event):
        # Native gethostbyname cannot be interrupted portably. Only this daemon
        # can wait for it; the captured generation drops any late result. There
        # is at most one lookup in flight per search, never one per retry tick.
        while self._active(generation):
            try:
                ip = socket.gethostbyname(host)
            except OSError as exc:
                if self._active(generation):
                    logger.debug("Discovery: direct address resolution retry: %s", exc)
            else:
                self._apply_direct(ip, generation)
            if cancel.wait(config.HEARTBEAT_INTERVAL):
                return

    def _send_probes(self, sock: socket.socket, now: float, generation: int | None = None):
        generation = self._generation if generation is None else generation
        with self._peer_lock:
            if not self._active(generation):
                return
            requests = []
            for ip in self._candidate_ips:
                probe = self._pending_probes.get(ip)
                if probe is None or now >= probe.expires:
                    first_sent = probe.first_sent if probe is not None else now
                    probe = _Probe(secrets.token_bytes(16), now + config.HEARTBEAT_TIMEOUT,
                                   first_sent)
                    self._pending_probes[ip] = probe
                requests.append((ip, probe.nonce))
        for ip, nonce in requests:
            try:
                sock.sendto(protocol.encode(config.PKT_DISCOVERY_REQUEST, nonce,
                            channels=0, sample_rate=0), (ip, config.HEARTBEAT_PORT))
            except OSError as exc:
                logger.debug("Discovery: probe to %s failed: %s", ip, exc)

    def _begin_legacy(self, now: float, tried: set[str],
                      generation: int | None = None) -> _LegacyAttempt | None:
        """Legacy heartbeats also start streaming: permit only one at a time."""
        generation = self._generation if generation is None else generation
        with self._peer_lock:
            if not self._active(generation) or self._legacy_attempt is not None:
                return None
            eligible = {
                ip for ip, probe in self._pending_probes.items()
                if ip not in self._modern_ips
                and now >= probe.first_sent + config.HEARTBEAT_TIMEOUT
            }
            tried.intersection_update(eligible)
            available = eligible - tried
            if not available:
                tried.clear()
                return None
            ip = min(available)
            tried.add(ip)
            attempt = _LegacyAttempt(
                ip, {name for name, addresses in self._service_addresses.items() if ip in addresses},
                ip in self._direct_ips, now + config.HEARTBEAT_TIMEOUT,
                now + 2 * config.HEARTBEAT_TIMEOUT,
            )
            self._legacy_attempt = attempt
            return attempt

    def _accept_response(self, packet: protocol.Packet, addr: tuple[str, int],
                         modern: bool, now: float, generation: int | None = None) -> str | None:
        generation = self._generation if generation is None else generation
        if self._refresh_identity(generation) is None:
            return None
        with self._peer_lock:
            if not self._active(generation):
                return None
            if modern:
                if (self._legacy_attempt is None and addr[1] == config.HEARTBEAT_PORT
                        and packet.pkt_type == config.PKT_DISCOVERY_RESPONSE
                        and any(packet.payload == probe.nonce and now < probe.expires
                                for probe in self._pending_probes.values())):
                    return addr[0]  # Nonce correlates a secondary/alias source too.
            else:
                attempt = self._legacy_attempt
                if (attempt is not None and attempt.valid and now < attempt.expires
                        and addr[0] == attempt.ip
                        and packet.pkt_type == config.PKT_HEARTBEAT and not packet.payload):
                    return addr[0]  # Legacy has no nonce: never learn a different host.
        return None

    def _probe_candidates(self, generation: int | None = None):
        """One worker owns probing, legacy lease/cooldown and the single result."""
        generation = self._generation if generation is None else generation
        peer_ip = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as modern, \
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as legacy:
                with self._peer_lock:
                    if not self._active(generation):
                        return
                    self._search_sockets = (modern, legacy)
                modern.bind(("0.0.0.0", 0))
                legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                legacy.bind(("0.0.0.0", config.HEARTBEAT_PORT))
                for sock in (modern, legacy):
                    sock.settimeout(0.5)
                next_send = 0.0
                legacy_ready_at = 0.0
                tried: set[str] = set()
                while self._active(generation):
                    now = time.monotonic()
                    with self._peer_lock:
                        if not self._active(generation):
                            return
                        attempt = self._legacy_attempt
                        if attempt is not None and now >= attempt.release_at:
                            self._legacy_attempt = None
                            # Give late/new modern replies a full validity window
                            # before another provisional legacy stream can begin.
                            legacy_ready_at = now + config.HEARTBEAT_TIMEOUT
                    if now >= next_send:
                        addresses = self._refresh_identity(generation)
                        if addresses is None:
                            return
                        self._refresh_browser(generation, addresses)
                        now = time.monotonic()
                        self._send_probes(modern, now, generation)
                        next_send = now + config.HEARTBEAT_INTERVAL
                    try:
                        readable, _, _ = select.select([modern, legacy], [], [], 0.5)
                    except (OSError, ValueError) as exc:
                        if not self._active(generation):
                            return
                        logger.debug("Discovery: select retry: %s", exc)
                        continue
                    for sock in readable:
                        try:
                            data, addr = sock.recvfrom(128)
                        except OSError as exc:
                            if not self._active(generation):
                                return
                            logger.debug("Discovery: response retry: %s", exc)
                            continue
                        packet = protocol.decode(data)
                        if packet is not None:
                            peer_ip = self._accept_response(packet, addr, sock is modern,
                                                            time.monotonic(), generation)
                            if peer_ip is not None:
                                break
                    if peer_ip is not None:
                        break
                    now = time.monotonic()
                    if self._active(generation) and self._legacy_attempt is None and now >= legacy_ready_at:
                        attempt = self._begin_legacy(now, tried, generation)
                        if attempt is not None:
                            try:
                                # Reflected requests have payload; old server replies do not.
                                legacy.sendto(protocol.encode(config.PKT_HEARTBEAT,
                                              b"discovery-request", channels=0, sample_rate=0),
                                              (attempt.ip, config.HEARTBEAT_PORT))
                            except OSError as exc:
                                logger.debug("Discovery: legacy probe retry: %s", exc)
                                with self._peer_lock:
                                    if self._active(generation):
                                        self._legacy_attempt = None
        except OSError as exc:
            if self._active(generation):
                logger.warning("Discovery probe failed: %s", exc)
        finally:
            with self._peer_lock:
                if self._generation == generation:
                    self._search_sockets = ()
                    self._legacy_attempt = None
        if peer_ip is not None and self._active(generation):
            logger.info("Discovery: reachable server at %s", peer_ip)
            threading.Thread(target=self._notify_peer, args=(peer_ip, generation), daemon=True).start()

    def _on_state_change(self, zeroconf: Zeroconf, service_type: str,
                         name: str, state_change: ServiceStateChange,
                         generation: int | None = None):
        generation = self._generation if generation is None else generation
        if not self._active(generation):
            return
        modern = False
        if state_change == ServiceStateChange.Removed:
            addresses = set()
        elif state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            info = zeroconf.get_service_info(service_type, name)
            if info is None:
                return
            raw_addresses = list(info.parsed_addresses(IPVersion.V4Only))
            for key, extra in info.properties.items():
                if (key == b"addresses" or key.startswith(b"addresses-")) and isinstance(extra, bytes):
                    raw_addresses.extend(extra.decode("ascii", errors="ignore").split(","))
            addresses = set()
            for raw in raw_addresses:
                try:
                    ip = ipaddress.IPv4Address(raw)
                except ipaddress.AddressValueError:
                    continue
                if not ip.is_loopback and not ip.is_unspecified:
                    addresses.add(str(ip))
            modern = info.properties.get(b"discovery") == b"probe-v1"
            logger.info("Discovery: found service '%s' at %s", name, sorted(addresses))
        else:
            return
        local_addresses = _get_local_addresses()
        with self._peer_lock:
            if not self._active(generation):
                return
            self._local_ips = set(local_addresses)
            if addresses:
                self._service_addresses[name] = addresses
            else:
                self._service_addresses.pop(name, None)
            if modern and addresses:
                self._modern_services.add(name)
                self._known_modern_ips.update(addresses)
            else:
                self._modern_services.discard(name)
                if state_change != ServiceStateChange.Removed:
                    self._known_modern_ips.difference_update(addresses)
            self._prune_candidates()
            attempt = self._legacy_attempt
            # Removed is expected during legacy commit. Preserve only this
            # bounded lease; an owner's real address update can invalidate it.
            if (attempt is not None and state_change != ServiceStateChange.Removed
                    and name in attempt.services and (attempt.ip not in addresses or modern)):
                attempt.services.discard(name)
                if not attempt.services and not attempt.direct:
                    attempt.valid = False

    def stop(self):
        with self._lifecycle_lock:
            with self._peer_lock:
                self._running = False
                self._generation += 1
                self._cancel.set()
                self._direct_ips.clear()
                self._service_addresses.clear()
                self._modern_services.clear()
                self._known_modern_ips.clear()
                self._pending_probes.clear()
                self._legacy_attempt = None
            for sock in (*self._search_sockets, self._connect_sock):
                if sock is not None:
                    sock.close()
            if self._probe_thread:
                self._probe_thread.join(timeout=2.0)
                self._probe_thread = None
            if self._browser:
                self._browser.cancel()
                self._browser = None
            if self._listen_thread:
                self._listen_thread.join(timeout=2.0)
                self._listen_thread = None
            self._search_sockets = ()
            self._connect_sock = None
            # A native DNS call may still be pending in its daemon. It owns its
            # old cancel event/generation and is deliberately not joined here.
            self._resolver_thread = None
            self._browse_addresses = self._advertised_addresses = None
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
        self._check_thread = threading.Thread(
            target=self._check_loop, daemon=True
        )
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
