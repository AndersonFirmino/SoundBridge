# Receive system audio on macOS

The macOS client receives Opus audio from a Windows or Linux server and plays it
on its default output device. Windows captures its default output using WASAPI
loopback; Linux uses PulseAudio or PipeWire. Use the CLI on macOS; the tray GUI is
not supported by this setup. The Mac launcher disables microphone capture by
default, so receiving Linux audio does not require a microphone or mic permission.
Microphone return is supported by Linux servers only, not Windows servers.

## Install

Install [Homebrew](https://brew.sh), then clone the repository and run:

```bash
git clone https://github.com/AndersonFirmino/SoundBridge.git
cd SoundBridge
./scripts/mac/install.sh
```

The installer uses Homebrew to install Opus and uv, syncs the Python environment,
checks libopus loading and lists audio devices. The repository's `.python-version`
selects the default Python version. You can select another supported Python with
`uv sync --python <VERSION>` and use the same selection when running uv commands.

On Windows, follow [the server setup](WINDOWS.md). On Linux, follow the
[server instructions](../README.md#server-linux-or-windows). Choose the output
device you want to share as the server default output, and the desired macOS
default output (for example headphones), before connecting.

## Connect automatically on the LAN

Start the server, then run the Mac launcher **without an IP address**:

```bash
./scripts/mac/run-client.sh
```

The client discovers Windows and Linux servers through mDNS on the same network.
The server advertises its primary IPv4 address and extra interfaces in mDNS TXT
records; the client probes these addresses and selects an effective server endpoint
only after a reachability response. Multiple Ethernet,
Wi-Fi, VPN or virtual interfaces do not require choosing an address manually.
Changes to local interfaces refresh discovery while searching, and slow direct DNS
lookups do not block LAN discovery or cancellation.
The launcher runs `client --no-gui --no-mic`. To send a Mac microphone to a Linux
server instead, use `uv run python -m soundbridge client --no-gui` and grant macOS
Microphone permission. Mic failure does not prevent audio reception.

### Network permissions

Allow multicast DNS (UDP 5353, multicast group `224.0.0.251`) on the local network,
UDP 4413 heartbeats in both directions, and inbound UDP 4410 audio on the client.
For optional microphone return, also allow inbound UDP 4411 on the Linux server.
Discovery requests go to server UDP 4413; modern replies return from 4413 to the
client's ephemeral request port. Restricted firewalls must allow stateful UDP
return traffic for those requests, not open all ephemeral ports. Firewall rules
must match the actual Python executable used on each host and the trusted network
profile. Do not expose these unauthenticated UDP services to an
untrusted network.

On macOS, allow Local Network access for the terminal/Python application when
prompted (or review it in System Settings > Privacy & Security > Local Network).
The zeroconf library shares the mDNS port with the system's mDNSResponder; do not
stop mDNSResponder to free port 5353. Guest Wi-Fi, client isolation, multicast
filtering and separate VLANs can prevent discovery even when Internet access works.

### Tailscale / VPN (no mDNS)

Tailscale and many routed VPNs do not carry LAN mDNS. Only in that case, or on a
network that blocks multicast discovery, specify the server's reachable IPv4
address:

```bash
./scripts/mac/run-client.sh <SERVER_VPN_IP>
# Equivalent CLI:
uv run python -m soundbridge client --no-gui --no-mic --ip <SERVER_VPN_IP>
```

Both hosts must have a route to each other and permit the audio/heartbeat flows
above. Following a heartbeat timeout, a client started with `--ip` also probes its
original explicit address while browsing mDNS, so it can reconnect without LAN
multicast. A reconnect is reported only after a server response.

New servers answer discovery probes without starting audio; streaming starts only
when the chosen client sends heartbeats. Older servers remain compatible through
a sequential heartbeat fallback, which can take longer when several announced
addresses are unreachable (including the first explicit `--ip` connection to an
older server). A modern nonce response can validate the server's actual source
address even when `--ip` points to a secondary address. Legacy heartbeats have no
nonce and are accepted only from the exact address being attempted; for an older
server, use the address it actually uses to send to the client. Audio is accepted
only from the validated endpoint.

## Troubleshooting

A connected indicator alone does not prove audio is being captured or received.
Check each stage rather than attributing silence immediately to a firewall:

1. If discovery remains searching, check UDP 5353, Local Network permission and
   whether both hosts share a multicast-capable LAN. The server log lists the
   advertised addresses; the client logs the addresses found and reachable peer.
2. Verify routing in both directions. A `No route to host` error can reflect
   routing, an unavailable host, filtering or OS network permissions; it does not
   identify one cause by itself.
3. Check whether the server log reports a client heartbeat from the expected
   client. If not, inspect UDP 4413 traffic and permissions.
4. Check the server capture/backend logs. Play audio through its default output;
   ensure it is capturing and encoding rather than logging an audio-device error.
5. Verify UDP 4410 packets leave the server for the expected client. If packets
   leave but none arrive, inspect the network and receiving permissions.
6. Check that the client decodes packets and its selected output is audible and
   unmuted.

`tools/test_receiver.py` can measure packet delivery and an optional test tone on
another host. Stop the real client before using it, since both bind UDP 4410.
`NO PACKETS` means only that the receiver did not measure audio from the requested
server; inspect the stages above. The diagnostic does not play audio and cannot
prove macOS playback. See [Windows diagnostics](WINDOWS.md) for timing details.

## Validation scope

Unit tests and a local loopback diagnostic do not establish remote macOS playback.
Verify installation, library loading, automatic discovery, audible output and
reconnection on the actual Mac and network before relying on this path. Do not use
this guide as a claim that every Linux/Windows or macOS device combination has
been tested.
