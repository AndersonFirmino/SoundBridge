![SoundBridge](soundbridge_pic.png)

Audio bridge entre maquinas na mesma LAN via UDP. O server (Linux) captura audio do sistema e envia para o client (Windows), que reproduz no headphone. O microfone do Windows volta como dispositivo de entrada virtual no Linux, e a webcam do Windows tambem pode ser exposta como camera virtual no Linux para apps como Discord, Google Meet, OBS, etc.

## Features

- **Codec Opus** — compressao de ~1920 bytes PCM para ~160 bytes Opus por frame, zero fragmentacao IP
- **FEC + PLC** — Forward Error Correction e Packet Loss Concealment via libopus, audio suave em vez de clicks quando pacotes se perdem
- **Jitter buffer adaptativo** — RFC 3550, ajusta profundidade do buffer automaticamente baseado no jitter da rede
- **Sequence numbers** — deteccao de gaps para acionar PLC nos frames perdidos
- **Virtual mic via pipe-source** — FIFO direto para PipeWire, latencia minima (~42ms buffer), Discord/Meet veem como microfone real
- **Compartilhamento opcional de webcam** — webcam do Windows -> `ffmpeg` -> UDP -> camera virtual Linux via `v4l2loopback`
- **Zero config** — discovery automatico via mDNS (zeroconf), sem IPs manuais
- **Heartbeat** — deteccao de desconexao em 5s, reconexao automatica

## Arquitetura

```
Linux (Server)                              Windows (Client)
──────────────────────────────────────────────────────────────
System Audio (parec)                        Headphone Playback
        │ Opus encode                          ▲ Opus decode
        └──── UDP :4410 (stereo, 48kHz) ───────┘

Virtual Mic (pipe-source FIFO)              Mic Capture
        ▲ Opus decode                          │ Opus encode
        └──── UDP :4411 (mono, 48kHz) ─────────┘

Virtual Camera (/dev/videoX)                Webcam Capture (ffmpeg/dshow)
        ▲ ffmpeg decode                        │ ffmpeg encode
        └──── UDP :4412 (H.264/MPEG-TS) ───────┘

mDNS (zeroconf)  ◄──── service discovery ──►  mDNS (zeroconf)
Heartbeat        ◄──── UDP :4413 ──────────►  Heartbeat
```

### Protocolo

Pacotes UDP com header binario de 10 bytes:

```
| magic "SB" (2B) | type (1B) | channels (1B) | sample_rate (2B) | seq (2B) | payload_size (2B) | Opus data |
```

Tipos de pacote:
- `0x01` — audio do sistema (stereo, Opus 128kbps)
- `0x02` — audio do microfone (mono, Opus 64kbps)
- `0x03` — heartbeat (sem payload)

### Fluxo de conexao

1. Server registra servico mDNS `_soundbridge._udp.local.` e escuta heartbeats na porta 4413
2. Client descobre o server via mDNS (zeroconf ServiceBrowser)
3. Client inicia streaming e envia heartbeat para o server
4. Server recebe o primeiro heartbeat, identifica o IP do client e inicia streaming
5. Se heartbeat falha (5s sem resposta), desconecta e reinicia discovery

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (gerenciador de pacotes)
- **Linux**: `libopus0`, PipeWire (ou PulseAudio), `ffmpeg`, `v4l2loopback-dkms`
- **Windows**: `opus.dll` (ja inclusa no projeto)

```bash
# Linux — instalar libopus (provavelmente ja instalado via PipeWire)
sudo apt install libopus0

# Suporte a webcam no Linux
sudo apt install ffmpeg v4l2loopback-dkms

# Criar a camera virtual usada pelo SoundBridge
sudo modprobe v4l2loopback video_nr=10 card_label="SoundBridge Camera" exclusive_caps=1
```

No Windows, instale `ffmpeg` e garanta que ele esteja disponivel no `PATH`.

## Instalacao

```bash
git clone https://github.com/AndersonFirmino/SoundBridge.git
cd SoundBridge
uv sync
```

## Configuracao de rede

A comunicacao usa UDP nas portas 4410-4413. Firewalls bloqueiam por padrao.

### Windows Firewall

```powershell
# PowerShell como administrador — liberar portas
netsh advfirewall firewall add rule name="SoundBridge UDP In" dir=in action=allow protocol=UDP localport=4410-4413
netsh advfirewall firewall add rule name="SoundBridge UDP Out" dir=out action=allow protocol=UDP remoteport=4410-4413
```

### Linux Firewall

```bash
sudo iptables -I INPUT -p udp --dport 4410:4413 -j ACCEPT

# Tornar persistente (Debian/Ubuntu)
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

## Uso

### Server (Linux)

```bash
# Com GUI
# Os controles de webcam ficam direto na janela.
uv run soundbridge server

# Sem GUI
uv run soundbridge server --no-gui

# Sem GUI + camera virtual
uv run soundbridge server --no-gui --webcam

# Camera virtual em um device customizado
uv run soundbridge server --webcam --virtual-camera-device /dev/video10

# Listar dispositivos de audio
uv run soundbridge server --list-devices
```

### Client (Windows)

```powershell
# Com GUI — duplo clique no soundbridge-client.bat
# Ou via terminal:
# Os controles de webcam ficam direto na janela.
uv run soundbridge client

# Sem GUI
uv run soundbridge client --no-gui

# Sem GUI + compartilhamento de webcam
uv run soundbridge client --no-gui --webcam

# Conectar direto a um IP (sem discovery)
uv run soundbridge client --ip 192.168.0.5

# Escolher camera, resolucao e FPS
uv run soundbridge client --webcam --camera-device "Integrated Camera" --video-size 1280x720 --video-fps 30

# Listar cameras disponiveis
uv run soundbridge client --list-cameras
```

## Notas sobre webcam

- O caminho de webcam e separado do protocolo de audio; o audio continua funcionando mesmo se a webcam falhar ao iniciar.
- No modo GUI, voce nao precisa usar `--webcam`; basta ativar a webcam ou a camera virtual pelos controles da janela.
- A GUI do client permite ativar a webcam, escolher camera e selecionar resolucao/FPS.
- A GUI do server permite ativar a camera virtual e verificar o setup de `/dev/videoX`.
- No modo GUI, trocar camera, resolucao, FPS ou device virtual reinicia so o pipeline de video e mantem o audio conectado.

## Testes

```bash
uv run pytest -v tests/
```

```
tests/
├── conftest.py         # fixtures: frames PCM e payloads bytes
├── test_opus.py        # roundtrip encode/decode, PLC, compression ratio
├── test_protocol.py    # header 10 bytes, seq number, encode/decode
├── test_audio.py       # jitter buffer, prebuffering, devices, parec/pacat
├── test_main.py        # integracao de lifecycle para audio + webcam
├── test_network.py     # UDPSender, Discovery (zeroconf), heartbeat
├── test_video.py       # builders de ffmpeg, validacao e lifecycle de video
└── test_state.py       # ConnectionState enum
```

## Estrutura do projeto

```
soundbridge/
├── pyproject.toml          # dependencias e build config (uv + hatchling)
├── soundbridge/
│   ├── main.py             # Server, Client, CLI
│   ├── opus.py             # ctypes wrapper libopus (encoder/decoder/PLC)
│   ├── audio.py            # Capture, Playback (jitter buffer), VirtualMicSource (pipe-source)
│   ├── network.py          # UDPSender, UDPReceiver, Discovery (zeroconf), Heartbeat
│   ├── protocol.py         # encode/decode do protocolo binario (10B header + seq)
│   ├── config.py           # constantes (portas, sample rate, packet types)
│   ├── state.py            # ConnectionState enum
│   ├── video.py            # helpers ffmpeg, sender de webcam, receiver de camera virtual
│   ├── gui.py              # CustomTkinter + system tray
│   └── opus.dll            # libopus 1.5.2 win-x64 (bundled)
└── tests/
```

## Configuracao

| Constante | Valor | Descricao |
|---|---|---|
| `SAMPLE_RATE` | 48000 | Hz |
| `FRAME_SIZE` | 480 | Samples por frame (10ms) |
| `CHANNELS_STEREO` | 2 | Audio do sistema |
| `CHANNELS_MONO` | 1 | Microfone |
| `AUDIO_PORT` | 4410 | UDP — audio do sistema |
| `MIC_PORT` | 4411 | UDP — microfone |
| `VIDEO_PORT` | 4412 | UDP — stream da webcam |
| `HEARTBEAT_PORT` | 4413 | UDP — heartbeat |
| `HEARTBEAT_TIMEOUT` | 5.0s | Tempo sem heartbeat = desconexao |

## Branches

- `main` — release estavel
- `develop` — staging para proxima release
- `feature/*` — branches de feature mergeadas em develop

## Licenca

MIT
