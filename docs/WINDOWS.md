# Windows system-audio server

Install [uv](https://docs.astral.sh/uv/), clone the repository, and run `uv sync`
from the checkout. The repository keeps its existing Python version selection;
Python 3.12 or later is required. Windows installs the conditional `soundcard`
dependency for WASAPI loopback capture. Linux capture still uses PulseAudio or
PipeWire and `parec`.

Choose the Windows default output device, then start the server:

```powershell
uv run python -m soundbridge server --no-gui
```

Alternatively, use `soundbridge-server.bat` for the GUI. The server captures the
default Windows output; it does not create a virtual microphone on Windows.
Microphone return is supported on Linux servers only. The Mac launcher disables
mic capture by default; other clients can use `--no-mic` for playback only.

## Automatic LAN discovery

Start a macOS client as described in [the macOS guide](MAC.md), without an IP.
For a Windows client receiving from a Linux or Windows server, run:

```powershell
uv run python -m soundbridge client --no-gui
```

Or double-click `soundbridge-client.bat` for the Windows GUI. Discovery uses mDNS
on the same LAN; no address is required. Servers advertise a primary IPv4 address plus extra interfaces in mDNS TXT
records, and clients select an effective endpoint after a round-trip reachability probe,
rather than assuming the first address belongs to a reachable network. New probes
do not start audio on servers that were not selected. Older clients still connect
with heartbeats, and new clients fall back to sequential heartbeat attempts for
older servers. The fallback can take longer when addresses are unreachable; the
receiver accepts audio only from the validated server endpoint. Modern nonce
responses can validate a secondary address with a different reply/source address;
legacy heartbeats must come from the exact address attempted. For an older server,
use the address it actually uses to send to the client.

Allow inbound UDP 5353 for mDNS on both hosts (multicast `224.0.0.251`), inbound
UDP 4413 for heartbeats on both hosts, and inbound UDP 4410 for audio on the client.
Modern discovery requests go to server UDP 4413 and replies return from 4413 to
the client's ephemeral request port. Restrictive policies must permit established
UDP return traffic, rather than opening all ephemeral ports. If outbound traffic
is restricted, permit the matching outbound multicast, heartbeat and audio flows. Optional microphone return needs inbound UDP 4411 on
the Linux server. Scope permissions to the trusted network; these services are
unauthenticated.

Windows Firewall rules must apply to the actual Python executable used by that
installation, including `pythonw.exe` when running without a console, not to a
presumed system Python. Inspect the CLI executable with
`uv run python -c "import sys; print(sys.executable)"`. Check the firewall's network
profile as well as its application and port rules. Guest Wi-Fi, AP isolation,
multicast filtering or different VLANs can block discovery. Silence alone does
not prove filtering. On macOS, see [network permissions](MAC.md#network-permissions).

## Tailscale / VPN (no mDNS)

Tailscale and many routed VPNs do not forward LAN mDNS. For these networks (or if
multicast is unavailable), supply the server's reachable IPv4 address explicitly:

```powershell
uv run python -m soundbridge client --no-gui --ip <SERVER_VPN_IP>
```

On macOS, use `./scripts/mac/run-client.sh <SERVER_VPN_IP>`. Both hosts still need
routes and firewall permissions for UDP audio and heartbeats. After a heartbeat
timeout, the client retries the explicit address as well as browsing mDNS; it
waits for a server response before reconnecting. The first direct connection also
validates the effective source endpoint; an older server may need the slower
sequential heartbeat fallback.

## Packet and tone diagnostic

Use a second host for network and reconnect checks. Install the checkout and its
dependencies there. `tools/test_receiver.py` decodes audio but **does not play it**;
a PASS measures delivery and, if requested, a spectral tone, not audible playback.
Stop any real client on the receiver host before binding its UDP 4410 port.

The following processes must overlap. In three separate terminals:

1. **Windows terminal A:** run the server command above and leave it running.
2. **Windows terminal B:** start a delayed tone. It waits ten seconds, then plays
   for thirty seconds; leave it running while performing step 3 immediately:

   ```powershell
   uv run python tools/play_tone.py --freq 440 --delay 10 --seconds 30
   ```

3. **Receiver terminal C (another host):** immediately start the receiver,
   replacing the placeholder with the Windows address. Its 20-second measurement
   overlaps the tone for at least ten seconds when started within that delay:

   ```bash
   uv run python tools/test_receiver.py --server-ip <WINDOWS_SERVER_IP> --seconds 20 --expect-tone 440
   ```

The receiver prints unique packets/s, missing packets, loss percentage, gap
events, duplicates, reordering, PCM peak and tone ratio. PASS requires at least
80 unique packets/s, at most 1% missing packets and a tone ratio of at least 20.
If startup takes longer than the tone, repeat with a longer tone duration, not
by treating silence as a routing diagnosis. For troubleshooting stages, see
[the macOS guide](MAC.md#troubleshooting).

For a **local smoke test only**, terminal C can instead run on Windows with
`--server-ip 127.0.0.1`. This is not a remote E2E or reconnect test: the current
heartbeat implementation can receive its own packets on one host. Restart the
server after a loopback test before connecting a remote client.

To check reconnects, stop the remote receiver, wait for the server's heartbeat
timeout and return to discovery, then run it again without restarting the server.
Repeat at least twice. For actual playback validation, also run the real client
on the destination host and verify its audio output, including after server restart.
