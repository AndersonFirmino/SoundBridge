"""Discovery regressions; native UDP lifecycle tests use isolated loopback ports."""

import socket
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from zeroconf import ServiceInfo, ServiceStateChange as Change, NonUniqueNameException

from soundbridge import config, protocol
from soundbridge.network import (
    UDPSender, UDPReceiver, Discovery, Heartbeat, _LegacyAttempt, _Probe,
    _get_local_addresses, _address_properties,
)


class TestUDPSender:

    @patch("soundbridge.network.socket.socket")
    def test_send_audio_sends_correct_packet(self, socket_cls):
        sender = UDPSender("192.168.1.100", config.AUDIO_PORT)
        sender.send_audio(b"\x00" * 160, config.PKT_AUDIO_DATA,
                          config.CHANNELS_STEREO, seq=42)
        data, dest = socket_cls.return_value.sendto.call_args.args
        packet = protocol.decode(data)
        assert dest == ("192.168.1.100", config.AUDIO_PORT)
        assert packet.pkt_type == config.PKT_AUDIO_DATA
        assert packet.seq == 42
        assert packet.channels == config.CHANNELS_STEREO
        assert packet.payload == b"\x00" * 160

    @patch("soundbridge.network.socket.socket")
    def test_send_audio_default_seq_zero(self, socket_cls):
        sender = UDPSender("192.168.1.100", config.AUDIO_PORT)
        sender.send_audio(b"\x00" * 80, config.PKT_MIC_DATA, config.CHANNELS_MONO)
        assert protocol.decode(socket_cls.return_value.sendto.call_args.args[0]).seq == 0


class TestDiscoveryServer:

    @pytest.mark.parametrize("primary", [None, "198.51.100.10"])
    @patch("soundbridge.network.ifaddr.get_adapters")
    def test_all_interfaces_without_internet_and_historical_primary(self, adapters, primary):
        adapters.return_value = [MagicMock(ips=[
            MagicMock(ip=ip, is_IPv4=v4) for ip, v4 in [
                ("192.0.2.10", True), ("198.51.100.10", True),
                ("192.0.2.10", True), ("169.254.0.10", True),
                ("127.0.0.1", True), ("0.0.0.0", True), (("fe80::1", 0, 1), False),
            ]
        ])]
        with patch("soundbridge.network._get_default_route_ip", return_value=primary):
            addresses = _get_local_addresses()
        assert set(addresses) == {"192.0.2.10", "198.51.100.10", "169.254.0.10"}
        assert len(addresses) == 3
        if primary:
            assert addresses[0] == primary

    @patch("soundbridge.network._get_local_addresses", return_value=["192.0.2.10", "198.51.100.10"])
    @patch("soundbridge.network.Zeroconf")
    @patch("soundbridge.network.threading.Thread")
    @patch("soundbridge.network.socket.socket")
    def test_advertisement_preserves_old_client_and_stop_unregisters(self, socket_cls, thread, zc_cls, addresses):
        discovery = Discovery(MagicMock())
        discovery.start_listen()
        info = zc_cls.return_value.register_service.call_args.args[0]
        assert info.name == config.ZEROCONF_SERVICE_NAME
        assert info.parsed_addresses() == ["192.0.2.10"]
        assert info.properties[b"addresses"] == b"192.0.2.10,198.51.100.10"
        assert info.properties[b"discovery"] == b"probe-v1"
        discovery.stop()
        zc_cls.return_value.unregister_service.assert_called_once_with(info)
        zc_cls.return_value.close.assert_called_once()

    @patch("soundbridge.network.threading.Thread")
    def test_passive_probes_leave_two_servers_idle(self, thread):
        request = protocol.encode(config.PKT_DISCOVERY_REQUEST, b"x" * 16)
        for _ in range(2):
            discovery = Discovery(MagicMock())
            discovery._running = True
            discovery._connect_sock = MagicMock()
            received = [False]
            def receive(*args):
                if not received[0]:
                    received[0] = True
                    return request, ("192.0.2.20", 12345)
                discovery._running = False
                raise OSError("stop listener")
            discovery._connect_sock.recvfrom.side_effect = receive
            discovery._listen_for_connect()
            response, addr = discovery._connect_sock.sendto.call_args.args
            assert protocol.decode(response).pkt_type == config.PKT_DISCOVERY_RESPONSE
            assert protocol.decode(response).payload == b"x" * 16
            assert addr == ("192.0.2.20", 12345)
        thread.assert_not_called()

    @patch("soundbridge.network.threading.Thread")
    def test_listener_errors_and_hairpin_do_not_prevent_old_client_commit(self, thread):
        discovery = Discovery(MagicMock())
        discovery._running = True
        discovery._local_ips = {"192.0.2.10"}
        discovery._connect_sock = MagicMock()
        own = ("192.0.2.10", 12345)
        peer = ("192.0.2.20", 12345)
        request = protocol.encode(config.PKT_DISCOVERY_REQUEST, b"x" * 16)
        discovery._connect_sock.recvfrom.side_effect = [
            (request, own), (protocol.encode(config.PKT_HEARTBEAT, b"discovery-request"), own),
            (request, peer), ConnectionResetError(10054, "abandoned client port"),
            (request, peer), (protocol.encode(config.PKT_HEARTBEAT), peer),
        ]
        discovery._connect_sock.sendto.side_effect = [OSError("route changed"), None]
        with patch("soundbridge.network._get_local_addresses", return_value=[own[0]]):
            discovery._listen_for_connect()
        assert discovery._connect_sock.recvfrom.call_count == 6
        assert discovery._connect_sock.sendto.call_count == 2
        thread.assert_called_once_with(target=discovery._notify_peer,
                                       args=(peer[0], discovery._generation), daemon=True)
        # The explicit loopback smoke uses an empty heartbeat and remains supported.
        thread.reset_mock()
        discovery._connect_sock.recvfrom.side_effect = [(protocol.encode(config.PKT_HEARTBEAT), own)]
        with patch("soundbridge.network._get_local_addresses", return_value=[own[0]]):
            discovery._listen_for_connect()
        thread.assert_called_once_with(target=discovery._notify_peer,
                                       args=(own[0], discovery._generation), daemon=True)


class TestDiscoveryClient:

    @staticmethod
    def update(discovery, name, state, addresses=(), properties=None):
        zc = MagicMock()
        zc.get_service_info.return_value.parsed_addresses.return_value = addresses
        zc.get_service_info.return_value.properties = properties or {}
        discovery._running = True
        with patch("soundbridge.network._get_local_addresses", return_value=list(discovery._local_ips)):
            discovery._on_state_change(zc, config.ZEROCONF_SERVICE_TYPE, name, state)

    @staticmethod
    def run_worker(discovery, clock, receive):
        """Drive the real worker with real protocol packets, not constant nonces."""
        modern, legacy = MagicMock(), MagicMock()
        for sock in (modern, legacy):
            sock.__enter__.return_value = sock
        modern.recvfrom.side_effect = lambda n: receive("modern", modern, legacy)
        legacy.recvfrom.side_effect = lambda n: receive("legacy", modern, legacy)
        def readable(*args):
            clock[0] += 0.5
            return [modern, legacy], [], []
        def make_thread(*, target, args, daemon):
            thread = MagicMock()
            thread.start.side_effect = lambda: target(*args)
            return thread
        discovery._running = True
        with patch("soundbridge.network._get_local_addresses", return_value=list(discovery._local_ips)), \
                patch("soundbridge.network.socket.socket", side_effect=[modern, legacy]), \
                patch("soundbridge.network.select.select", side_effect=readable), \
                patch("soundbridge.network.time.monotonic", side_effect=lambda: clock[0]), \
                patch("soundbridge.network.threading.Thread", side_effect=make_thread):
            discovery._probe_candidates()
        return modern, legacy

    def test_modern_correlation_accepts_delayed_alias_response_and_reuses_nonce(self):
        discovery = Discovery(MagicMock())
        discovery._direct_ips = {"192.0.2.10"}
        clock = [0.0]
        def receive(kind, modern, legacy):
            if kind == "legacy" or clock[0] < 2:
                raise socket.timeout()
            first = protocol.decode(modern.sendto.call_args_list[0].args[0])
            return protocol.encode(config.PKT_DISCOVERY_RESPONSE, first.payload), ("198.51.100.10", config.HEARTBEAT_PORT)
        modern, legacy = self.run_worker(discovery, clock, receive)
        nonces = {protocol.decode(c.args[0]).payload for c in modern.sendto.call_args_list}
        assert len(modern.sendto.call_args_list) >= 2
        assert len(nonces) == 1  # A real random nonce, stable across retransmissions.
        discovery.on_peer_found.assert_called_once_with("198.51.100.10")
        legacy.sendto.assert_not_called()

    @pytest.mark.parametrize("invalid", ["expired", "retired", "wrong_nonce"])
    def test_uncorrelated_or_expired_modern_response_never_selects(self, invalid):
        discovery = Discovery(MagicMock())
        discovery._service_addresses = {"one": {"192.0.2.10"}}
        discovery._modern_services = {"one"}
        clock = [0.0]
        reads = [0]
        def receive(kind, modern, legacy):
            if kind == "legacy":
                raise socket.timeout()
            reads[0] += 1
            if reads[0] > 1:
                discovery._running = False
                raise socket.timeout()
            nonce = protocol.decode(modern.sendto.call_args_list[0].args[0]).payload
            if invalid == "expired":
                clock[0] = config.HEARTBEAT_TIMEOUT + 1
            elif invalid == "retired":
                self.update(discovery, "one", Change.Removed)
            else:
                nonce = b"not-the-issued-token"
            return protocol.encode(config.PKT_DISCOVERY_RESPONSE, nonce), ("198.51.100.10", config.HEARTBEAT_PORT)
        self.run_worker(discovery, clock, receive)
        discovery.on_peer_found.assert_not_called()

    def test_legacy_correlation_ignores_foreign_late_and_reflected_heartbeats(self):
        discovery = Discovery(MagicMock())
        discovery._direct_ips = {"192.0.2.10"}
        clock = [0.0]
        responses = iter([
            (protocol.encode(config.PKT_HEARTBEAT), ("203.0.113.88", 12345)),
            (protocol.encode(config.PKT_HEARTBEAT), ("198.51.100.10", 12345)),
            (protocol.encode(config.PKT_HEARTBEAT, b"discovery-request"), ("192.0.2.10", 12345)),
            (protocol.encode(config.PKT_HEARTBEAT), ("192.0.2.10", 12345)),
        ])
        def receive(kind, modern, legacy):
            if kind == "modern" or discovery._legacy_attempt is None:
                raise socket.timeout()
            return next(responses)
        modern, legacy = self.run_worker(discovery, clock, receive)
        discovery.on_peer_found.assert_called_once_with("192.0.2.10")
        assert legacy.sendto.call_count == 1
        assert legacy.sendto.call_args.args[1][0] == "192.0.2.10"
        assert all(protocol.decode(c.args[0]).pkt_type == config.PKT_DISCOVERY_REQUEST
                   for c in modern.sendto.call_args_list)

    def test_legacy_attempts_cool_down_and_ignore_previous_candidate(self):
        discovery = Discovery(MagicMock())
        discovery._direct_ips = {"192.0.2.10", "198.51.100.10"}
        clock = [0.0]
        send_times = []
        late = [True]
        def receive(kind, modern, legacy):
            if kind == "modern" or legacy.sendto.call_count < 2:
                if legacy.sendto.call_count == 1 and not send_times:
                    send_times.append(clock[0])
                raise socket.timeout()
            if len(send_times) == 1:
                send_times.append(clock[0])
            if late[0]:
                late[0] = False
                return protocol.encode(config.PKT_HEARTBEAT), ("192.0.2.10", 12345)
            return protocol.encode(config.PKT_HEARTBEAT), ("198.51.100.10", 12345)
        _, legacy = self.run_worker(discovery, clock, receive)
        discovery.on_peer_found.assert_called_once_with("198.51.100.10")
        assert legacy.sendto.call_count == 2
        assert send_times[1] - send_times[0] >= 2 * config.HEARTBEAT_TIMEOUT

    def test_direct_dns_recovers_and_changes_target_without_mdns(self):
        discovery = Discovery(MagicMock())
        discovery._running = True
        cancel = MagicMock()
        waits = [0]
        def wait(interval):
            waits[0] += 1
            if discovery._direct_ips == {"192.0.2.10"}:
                discovery._send_probes(MagicMock(), 0)
            return waits[0] == 3
        cancel.wait.side_effect = wait
        with patch("soundbridge.network.socket.gethostbyname", side_effect=[
                socket.gaierror(socket.EAI_AGAIN, "temporary failure"),
                "192.0.2.10", "198.51.100.10",
        ]) as resolve:
            discovery._resolve_direct("server.example.invalid", discovery._generation, cancel)
        assert resolve.call_count == 3
        assert all(c.args == ("server.example.invalid",) for c in resolve.call_args_list)
        assert discovery._direct_ips == {"198.51.100.10"}
        assert "192.0.2.10" not in discovery._pending_probes
        def receive(kind, modern, legacy):
            if kind == "legacy":
                raise socket.timeout()
            nonce = protocol.decode(modern.sendto.call_args.args[0]).payload
            return protocol.encode(config.PKT_DISCOVERY_RESPONSE, nonce), ("198.51.100.10", config.HEARTBEAT_PORT)
        self.run_worker(discovery, [0.0], receive)
        discovery.on_peer_found.assert_called_once_with("198.51.100.10")

    def test_lifecycle_replaces_service_addresses_and_scopes_legacy_lease(self):
        discovery = Discovery(MagicMock())
        discovery._running = True
        discovery._local_ips = {"192.0.2.99"}
        discovery._direct_ips = {"203.0.113.1"}
        self.update(discovery, "one", Change.Added, ["192.0.2.10", "192.0.2.99", "127.0.0.1", "0.0.0.0"])
        self.update(discovery, "two", Change.Added, ["192.0.2.10"])
        self.update(discovery, "one", Change.Updated, ["198.51.100.10"])
        assert discovery._candidate_ips == {"192.0.2.10", "198.51.100.10", "203.0.113.1"}
        attempt = _LegacyAttempt("192.0.2.10", {"two"}, False, 5, 10)
        discovery._legacy_attempt = attempt
        self.update(discovery, "two", Change.Removed)
        self.update(discovery, "one", Change.Updated, ["198.51.100.10"])
        heartbeat = protocol.decode(protocol.encode(config.PKT_HEARTBEAT))
        assert discovery._accept_response(heartbeat, ("192.0.2.10", 12345), False, 4) == "192.0.2.10"
        assert discovery._accept_response(heartbeat, ("192.0.2.10", 12345), False, 6) is None
        self.update(discovery, "two", Change.Updated, ["198.51.100.20"])
        assert not attempt.valid
        assert discovery._legacy_attempt is attempt  # Still blocks other commits until cooldown.
        assert discovery._accept_response(heartbeat, ("192.0.2.10", 12345), False, 4) is None
        self.update(discovery, "one", Change.Removed)
        self.update(discovery, "two", Change.Removed)
        assert discovery._candidate_ips == {"203.0.113.1"}

    def test_modern_capability_and_txt_chunks_preserve_all_interfaces(self):
        addresses = [f"192.0.2.{i}" for i in range(1, 100)]
        properties = {"discovery": "probe-v1", **_address_properties(addresses)}
        assert all(len(k) + 1 + len(v) <= 255 for k, v in properties.items())
        info = ServiceInfo(config.ZEROCONF_SERVICE_TYPE, config.ZEROCONF_SERVICE_NAME,
                           parsed_addresses=addresses[:1], port=config.AUDIO_PORT,
                           properties=properties)
        discovery = Discovery(MagicMock())
        discovery._local_ips = set()
        discovery._direct_ips = {addresses[0]}
        zc = MagicMock()
        zc.get_service_info.return_value = info
        discovery._running = True
        with patch("soundbridge.network._get_local_addresses", return_value=[]):
            discovery._on_state_change(zc, config.ZEROCONF_SERVICE_TYPE, "modern", Change.Added)
        assert discovery._candidate_ips == set(addresses)
        assert discovery._modern_ips == set(addresses)
        discovery._on_state_change(zc, config.ZEROCONF_SERVICE_TYPE, "modern", Change.Removed)
        assert discovery._candidate_ips == {addresses[0]}
        assert discovery._modern_ips == {addresses[0]}

    def test_modern_responses_keep_working_after_legacy_timeout(self):
        discovery = Discovery(MagicMock())
        discovery._service_addresses = {"old": {"192.0.2.10"}, "modern": {"198.51.100.10"}}
        discovery._modern_services = {"modern"}
        clock = [0.0]
        def receive(kind, modern, legacy):
            if kind == "legacy" or clock[0] < 15 or discovery._legacy_attempt is not None:
                raise socket.timeout()
            request = next(c for c in reversed(modern.sendto.call_args_list)
                           if c.args[1][0] == "198.51.100.10")
            return protocol.encode(config.PKT_DISCOVERY_RESPONSE, protocol.decode(request.args[0]).payload), ("198.51.100.10", config.HEARTBEAT_PORT)
        _, legacy = self.run_worker(discovery, clock, receive)
        assert legacy.sendto.call_count == 1
        assert legacy.sendto.call_args.args[1][0] == "192.0.2.10"
        discovery.on_peer_found.assert_called_once_with("198.51.100.10")

    @patch("soundbridge.network.ServiceBrowser")
    @patch("soundbridge.network.Zeroconf")
    @patch("soundbridge.network.threading.Thread")
    def test_start_and_stop_browser(self, thread, zc_cls, browser_cls):
        discovery = Discovery(MagicMock())
        discovery.start_search()
        browser_cls.assert_called_once()
        discovery.stop()
        browser_cls.return_value.cancel.assert_called_once()
        zc_cls.return_value.close.assert_called_once()


class TestDiscoveryLifecycle:

    @patch("soundbridge.network.threading.Thread")
    def test_identity_tracks_both_interface_change_directions(self, thread):
        a, b = "192.0.2.10", "198.51.100.10"
        current = [a]
        with patch("soundbridge.network._get_local_addresses", side_effect=lambda: list(current)):
            server = Discovery(MagicMock())
            server._running = True
            server._connect_sock = MagicMock()
            packets = iter([
                (protocol.encode(config.PKT_DISCOVERY_REQUEST, b"x" * 16), (b, 12345)),
                (protocol.encode(config.PKT_HEARTBEAT, b"discovery-request"), (b, 12345)),
                (protocol.encode(config.PKT_DISCOVERY_REQUEST, b"x" * 16), (a, 12345)),
                (protocol.encode(config.PKT_HEARTBEAT, b"discovery-request"), (a, 12345)),
            ])
            def receive(*args):
                current[:] = [b]  # B is now local; A is a legitimate remote peer.
                return next(packets)
            server._connect_sock.recvfrom.side_effect = receive
            server._listen_for_connect()
            assert server._connect_sock.sendto.call_count == 1
            thread.assert_called_once_with(target=server._notify_peer,
                                           args=(a, server._generation), daemon=True)
            current[:] = [a]
            client = Discovery(MagicMock())
            client._running = True
            client._service_addresses = {"peer": {a, b}}
            assert client._candidate_ips == {b}
            current[:] = [b]
            client._refresh_identity(client._generation)
            assert client._candidate_ips == {a}  # No new mDNS event is required.
            client._direct_ips = {a}
            client._pending_probes[a] = _Probe(b"x" * 16, 10, 0)
            client._legacy_attempt = _LegacyAttempt(a, {"peer"}, True, 10, 20)
            current[:] = [a]
            response = protocol.decode(protocol.encode(config.PKT_DISCOVERY_RESPONSE, b"x" * 16))
            assert client._accept_response(response, (a, config.HEARTBEAT_PORT), True, 1) is None
            assert a not in client._pending_probes
            assert not client._legacy_attempt.valid
            heartbeat = protocol.decode(protocol.encode(config.PKT_HEARTBEAT))
            assert client._accept_response(heartbeat, (a, 12345), False, 1) is None
            client._legacy_attempt = None  # Its cooldown was covered separately.
            client._pending_probes[b] = _Probe(b"x" * 16, 10, 0)
            assert client._accept_response(response, (b, config.HEARTBEAT_PORT), True, 1) == b

    @patch("soundbridge.network.threading.Thread")
    @patch("soundbridge.network.socket.socket")
    def test_hotplug_reopens_advertiser_and_browser_multicast_sockets(self, socket_cls, thread):
        a, b = "192.0.2.10", "198.51.100.10"
        current = [a]
        zcs = [MagicMock() for _ in range(4)]
        browsers = [MagicMock(), MagicMock()]
        with patch("soundbridge.network._get_local_addresses", side_effect=lambda: list(current)), \
                patch("soundbridge.network.Zeroconf", side_effect=zcs), \
                patch("soundbridge.network.ServiceBrowser", side_effect=browsers):
            server = Discovery(MagicMock())
            server.start_listen()
            old_info = server._service_info
            current[:] = [b]
            server._refresh_advertisement(server._generation)
            zcs[0].unregister_service.assert_called_once_with(old_info)
            zcs[0].close.assert_called_once()
            new_info = zcs[1].register_service.call_args.args[0]
            assert new_info.parsed_addresses() == [b]
            assert new_info.properties[b"addresses"] == b.encode()
            client = Discovery(MagicMock())
            client.start_search()
            current[:] = [a]
            addresses = client._refresh_identity(client._generation)
            client._refresh_browser(client._generation, addresses)
            browsers[0].cancel.assert_called_once()
            zcs[2].close.assert_called_once()
            assert client._zeroconf is zcs[3]
            server.stop()
            client.stop()

    @pytest.mark.parametrize("role", ["server", "client"])
    @patch("soundbridge.network.threading.Thread")
    @patch("soundbridge.network.socket.socket")
    def test_hotplug_retry_survives_constructor_registration_and_address_rollback(self, socket_cls, thread, role):
        a, b = "192.0.2.10", "198.51.100.10"
        current = [a]
        old_zc, failed_zc, live_zc = MagicMock(), MagicMock(), MagicMock()
        failed_zc.register_service.side_effect = NonUniqueNameException("transient registration conflict")
        old_browser, live_browser = MagicMock(), MagicMock()
        with patch("soundbridge.network._get_local_addresses", side_effect=lambda: list(current)), \
                patch("soundbridge.network.Zeroconf", side_effect=[
                    old_zc, OSError("interface not ready"), failed_zc, live_zc,
                ]) as constructor, \
                patch("soundbridge.network.ServiceBrowser", side_effect=[
                    old_browser, Exception("temporary browser failure"), live_browser,
                ]):
            discovery = Discovery(MagicMock())
            if role == "server":
                discovery.start_listen()
                refresh = lambda: discovery._refresh_advertisement(discovery._generation)
            else:
                discovery.start_search()
                refresh = lambda: discovery._refresh_browser(discovery._generation,
                    discovery._refresh_identity(discovery._generation))
            current[:] = [b]
            refresh()  # Constructor fails after the old stack was closed.
            assert discovery._running and discovery._zeroconf is None
            current[:] = [a]
            refresh()  # Returning to A must not hide the failed/absent stack.
            assert discovery._running and discovery._zeroconf is None
            failed_zc.close.assert_called_once()
            refresh()  # Same A, registration/browser creation now succeeds.
            assert constructor.call_count == 4
            assert discovery._zeroconf is live_zc
            if role == "server":
                assert discovery._service_info.parsed_addresses() == [a]
            else:
                assert discovery._browser is live_browser
            discovery.stop()

    def test_blocked_dns_does_not_block_mdns_io_and_late_result_is_discarded(self):
        discovery = Discovery(MagicMock())
        discovery._running = True
        discovery._service_addresses = {"mdns": {"192.0.2.10"}}
        discovery._modern_services = {"mdns"}
        entered, release = threading.Event(), threading.Event()
        def resolve(host):
            entered.set()
            release.wait()
            return "203.0.113.10"
        resolver = threading.Thread(target=discovery._resolve_direct,
            args=("slow.example.invalid", discovery._generation, discovery._cancel), daemon=True)
        clock = [0.0]
        def receive(kind, modern, legacy):
            if kind == "legacy" or clock[0] < 2:
                raise socket.timeout()
            nonce = protocol.decode(modern.sendto.call_args_list[0].args[0]).payload
            return protocol.encode(config.PKT_DISCOVERY_RESPONSE, nonce), ("192.0.2.10", config.HEARTBEAT_PORT)
        with patch("soundbridge.network.socket.gethostbyname", side_effect=resolve):
            resolver.start()
            try:
                assert entered.wait(1)
                TestDiscoveryClient.run_worker(discovery, clock, receive)
                discovery.on_peer_found.assert_called_once_with("192.0.2.10")
                assert resolver.is_alive()  # Lookup has not returned, but I/O converged.
                discovery.stop()
                with patch("soundbridge.network.threading.Thread"):
                    discovery.start_search(direct_ip="new.example.invalid", browse=False)
                discovery._apply_direct("198.51.100.10", discovery._generation)
                release.set()
                resolver.join(timeout=1)
                assert not resolver.is_alive()
                assert discovery._direct_ips == {"198.51.100.10"}
            finally:
                release.set()
                resolver.join(timeout=1)
                discovery.stop()

    def test_restart_rejects_previous_service_nonce_dns_and_callback(self):
        discovery = Discovery(MagicMock())
        with patch("soundbridge.network.threading.Thread"), \
                patch("soundbridge.network.Zeroconf"), \
                patch("soundbridge.network.ServiceBrowser") as browser, \
                patch("soundbridge.network._get_local_addresses", return_value=[]):
            discovery.start_search()
            old_generation = discovery._generation
            old_handler = browser.call_args.kwargs["handlers"][0]
            zc = MagicMock()
            zc.get_service_info.return_value.parsed_addresses.return_value = ["192.0.2.10"]
            zc.get_service_info.return_value.properties = {}
            old_handler(zc, config.ZEROCONF_SERVICE_TYPE, "old", Change.Added)
            discovery._send_probes(MagicMock(), 0, old_generation)
            nonce = discovery._pending_probes["192.0.2.10"].nonce
            discovery.stop()
            discovery.start_search(direct_ip="198.51.100.10", browse=False)
            generation = discovery._generation
            discovery._apply_direct("198.51.100.10", generation)
            old_handler(zc, config.ZEROCONF_SERVICE_TYPE, "old", Change.Added)
            discovery._apply_direct("192.0.2.10", old_generation)
            response = protocol.decode(protocol.encode(config.PKT_DISCOVERY_RESPONSE, nonce))
            assert discovery._accept_response(response, ("192.0.2.10", config.HEARTBEAT_PORT), True, 1, old_generation) is None
            assert discovery._accept_response(response, ("192.0.2.10", config.HEARTBEAT_PORT), True, 1, generation) is None
            discovery._notify_peer("192.0.2.10", old_generation)
            discovery.on_peer_found.assert_not_called()
            assert discovery._candidate_ips == {"198.51.100.10"}
            assert not discovery._pending_probes
            discovery._notify_peer("198.51.100.10", generation)
            discovery.on_peer_found.assert_called_once_with("198.51.100.10")
            discovery.stop()

    def test_stop_joins_io_while_old_native_dns_remains_blocked(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        discovery = Discovery(MagicMock())
        def resolve(host):
            if host == "old.example.invalid":
                entered.set()
                release.wait()
                return "127.0.0.2"
            return "127.0.0.3"
        with patch.object(config, "HEARTBEAT_PORT", port), \
                patch("soundbridge.network._get_local_addresses", return_value=[]), \
                patch("soundbridge.network.socket.gethostbyname", side_effect=resolve):
            old_dns = new_dns = None
            try:
                discovery.start_search(direct_ip="old.example.invalid", browse=False)
                old_io, old_dns = discovery._probe_thread, discovery._resolver_thread
                assert entered.wait(1)
                def stop():
                    discovery.stop()
                    stopped.set()
                stopper = threading.Thread(target=stop)
                stopper.start()
                # Three select periods of margin; DNS is deliberately not released.
                assert stopped.wait(1.5)
                stopper.join(timeout=1)
                assert not old_io.is_alive() and old_dns.is_alive()
                discovery.start_search(direct_ip="new.example.invalid", browse=False)
                new_dns = discovery._resolver_thread
                deadline = time.monotonic() + 1
                while discovery._direct_ips != {"127.0.0.3"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert discovery._direct_ips == {"127.0.0.3"}
                release.set()
                old_dns.join(timeout=1)
                assert not old_dns.is_alive()
                assert discovery._direct_ips == {"127.0.0.3"}
                discovery.on_peer_found.assert_not_called()
            finally:
                release.set()
                discovery.stop()
                for resolver in (old_dns, new_dns):
                    if resolver is not None:
                        resolver.join(timeout=1)


class TestReceiverMeasurements:

    def test_burst_loss_counts_missing_packets_not_gap_events(self):
        from tools.test_receiver import SequenceStats
        stats = SequenceStats()
        for seq in range(500):
            if not 200 <= seq < 250:
                stats.add(seq)
        assert stats.gap_events == 1
        assert stats.missing == 50
        assert stats.missing / (len(stats.seen) + stats.missing) == 0.1

    def test_wrap_duplicates_and_late_packets_do_not_inflate_loss_or_rate(self):
        from tools.test_receiver import SequenceStats
        stats = SequenceStats()
        for seq in (65534, 0, 0, 65535, 1):
            stats.add(seq)
        assert len(stats.seen) == 4
        assert stats.missing == 0
        assert stats.duplicates == 1
        assert stats.reordered == 1

    def test_short_tone_sample_is_rejected_with_explanation(self):
        from tools.test_receiver import tone_ratio
        with pytest.raises(ValueError, match="insufficient audio"):
            tone_ratio(np.zeros(480), 440)


class TestHeartbeat:

    def test_heartbeat_packet_type(self):
        packet = protocol.decode(protocol.encode(config.PKT_HEARTBEAT, channels=0, sample_rate=0))
        assert packet.pkt_type == config.PKT_HEARTBEAT
        assert packet.seq == 0


class TestUDPReceiverDecode:

    @patch("soundbridge.network.socket.socket")
    def test_only_selected_audio_source_reaches_decoder(self, socket_cls):
        raw = protocol.encode(config.PKT_AUDIO_DATA, b"audio")
        socket_cls.return_value.recvfrom.side_effect = [
            (raw, ("198.51.100.10", 12345)), (raw, ("192.0.2.10", 12345)), OSError("stop"),
        ]
        callback = MagicMock()
        receiver = UDPReceiver(config.AUDIO_PORT, callback, expected_ip="192.0.2.10")
        receiver._running = True
        receiver._receive_loop()
        callback.assert_called_once()
        assert callback.call_args.args[0].payload == b"audio"

    def test_decode_called_on_valid_data(self):
        packet = protocol.decode(protocol.encode(config.PKT_AUDIO_DATA, b"\x00" * 160,
                                                 config.CHANNELS_STEREO, seq=7))
        assert packet.pkt_type == config.PKT_AUDIO_DATA
        assert packet.seq == 7
        assert packet.payload == b"\x00" * 160

    def test_decode_returns_none_on_garbage(self):
        assert protocol.decode(b"garbage data that is not a packet") is None

    def test_buf_size_fits_opus(self):
        assert config.HEADER_SIZE + 1024 == 1034
