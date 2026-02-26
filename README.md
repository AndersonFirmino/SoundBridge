# SoundBridge

Audio bridge entre máquinas na mesma LAN via UDP. Projetado para o cenário: Linux (desktop) transmite áudio do sistema para Windows (notebook com headphone), e o microfone do Windows volta como dispositivo de entrada virtual no Linux.

## Arquitetura

```
Linux (Server)                              Windows (Client)
──────────────────────────────────────────────────────────────
System Audio (PulseAudio Monitor)           Headphone Playback
        │                                          ▲
        └──── UDP :4410 (stereo, 48kHz) ───────────┘

Virtual Mic (null-sink monitor)             Mic Capture
        ▲                                          │
        └──── UDP :4411 (mono, 48kHz) ─────────────┘

Discovery   ◄──── UDP :4412 broadcast ────►  Discovery
Heartbeat   ◄──── UDP :4413 ──────────────►  Heartbeat
```

### Protocolo

Pacotes UDP com header binário de 8 bytes:

```
| magic "SB" (2B) | type (1B) | channels (1B) | sample_rate (2B) | payload_size (2B) | PCM data |
```

Tipos de pacote:
- `0x01` — áudio do sistema (stereo)
- `0x02` — áudio do microfone (mono)
- `0x03` — heartbeat (sem payload)
- `0x04` — discovery ping
- `0x05` — discovery pong

### Fluxo de conexão

1. Server inicia e escuta pings de discovery na porta 4412
2. Client envia pings broadcast a cada 2s
3. Server responde com pong para a porta 4413 do client
4. Ambos iniciam streaming de áudio e heartbeat
5. Se heartbeat falha (5s sem resposta), desconecta e reinicia discovery

## Requisitos

- Python 3.12+
- PulseAudio (server Linux)
- Dependências: `sounddevice`, `numpy`, `pystray`, `Pillow`, `pulsectl` (Linux)

## Instalação

```bash
git clone <repo-url>
cd soundbridge
pip install -r requirements.txt
```

Para desenvolvimento (testes):

```bash
pip install pytest
```

## Uso

### Server (Linux)

```bash
# Com GUI (padrão)
python -m soundbridge server

# Sem GUI
python -m soundbridge server --no-gui

# Listar dispositivos de áudio
python -m soundbridge server --list-devices
```

O server captura o áudio do sistema via PulseAudio monitor e cria um dispositivo virtual de microfone (`SoundBridge Remote Microphone`) que aparece como entrada em aplicações como Google Meet, Discord, etc.

### Client (Windows)

```bash
# Com GUI — descobre server automaticamente
python -m soundbridge client

# Sem GUI
python -m soundbridge client --no-gui

# Conectar direto a um IP (sem discovery)
python -m soundbridge client --ip 192.168.1.50
```

O client reproduz o áudio recebido no dispositivo de saída padrão (headphone) e captura o microfone local para enviar ao server.

### GUI

A interface Tkinter mostra status de conexão, IP do peer e controles de volume. O botão "Minimize to Tray" envia para a bandeja do sistema (requer `pystray`). Fechar a janela minimiza para tray ao invés de encerrar.

## Testes

```bash
# Rodar todos os testes
pytest -v tests/

# Rodar um arquivo específico
pytest -v tests/test_protocol.py
```

### Estrutura de testes

```
tests/
├── conftest.py         # fixtures: stereo_frame, mono_frame
├── test_protocol.py    # encode/decode round-trip, rejeição de pacotes inválidos
├── test_audio.py       # find_pulse_monitor, list_devices, buffer, volume
├── test_network.py     # UDPSender com mock, packet types, decode
└── test_state.py       # ConnectionState enum
```

## Estrutura do projeto

```
soundbridge/
├── requirements.txt
├── soundbridge/
│   ├── __init__.py      # versão do pacote
│   ├── __main__.py      # entry point: python -m soundbridge
│   ├── main.py          # Server, Client, CLI — orquestração principal
│   ├── audio.py         # AudioCapture, AudioPlayback, VirtualMicSource
│   ├── network.py       # UDPSender, UDPReceiver, Discovery, Heartbeat
│   ├── protocol.py      # encode/decode do protocolo binário
│   ├── config.py        # constantes (portas, sample rate, packet types)
│   ├── state.py         # ConnectionState enum
│   └── gui.py           # Tkinter + system tray
└── tests/
```

## Configuração

Constantes em `soundbridge/config.py`:

| Constante | Valor | Descrição |
|---|---|---|
| `SAMPLE_RATE` | 48000 | Hz |
| `FRAME_SIZE` | 960 | Samples por frame (20ms) |
| `CHANNELS_STEREO` | 2 | Áudio do sistema |
| `CHANNELS_MONO` | 1 | Microfone |
| `AUDIO_PORT` | 4410 | UDP — áudio do sistema |
| `MIC_PORT` | 4411 | UDP — microfone |
| `DISCOVERY_PORT` | 4412 | UDP — discovery |
| `HEARTBEAT_PORT` | 4413 | UDP — heartbeat |
| `HEARTBEAT_TIMEOUT` | 5.0s | Tempo sem heartbeat = desconexão |

## Branches

- `main` — release estável
- `develop` — staging para próxima release
- `feature/*` — branches de feature mergeadas em develop
