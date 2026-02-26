# SoundBridge

> **Status: WIP** — O discovery e streaming ainda não estão funcionando de ponta a ponta entre Linux e Windows. A estrutura, protocolo, GUI e testes unitários estão prontos, mas a comunicação real na LAN precisa de debug. Veja a seção [Problemas conhecidos](#problemas-conhecidos).

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
- uv (gerenciador de pacotes)
- PulseAudio (server Linux)

## Instalação

```bash
git clone https://github.com/AndersonFirmino/SoundBridge.git
cd SoundBridge
uv sync
```

## Uso

### Server (Linux)

```bash
# Com GUI (padrão)
uv run python -m soundbridge server

# Sem GUI
uv run python -m soundbridge server --no-gui

# Listar dispositivos de áudio
uv run python -m soundbridge server --list-devices
```

O server captura o áudio do sistema via PulseAudio monitor e cria um dispositivo virtual de microfone (`SoundBridge Remote Microphone`) que aparece como entrada em aplicações como Google Meet, Discord, etc.

### Client (Windows)

```bash
# Com GUI — descobre server automaticamente
uv run python -m soundbridge client

# Sem GUI
uv run python -m soundbridge client --no-gui

# Conectar direto a um IP (sem discovery)
uv run python -m soundbridge client --ip 192.168.1.50
```

O client reproduz o áudio recebido no dispositivo de saída padrão (headphone) e captura o microfone local para enviar ao server.

### GUI

Interface CustomTkinter (dark mode) com status de conexão, IP do peer e controles de volume. O botão "Minimize to Tray" envia para a bandeja do sistema (requer `pystray`). Fechar a janela minimiza para tray ao invés de encerrar.

## Testes

```bash
uv run pytest -v tests/
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
├── pyproject.toml      # dependências e build config (uv + hatchling)
├── uv.lock             # lockfile para installs reproduzíveis
├── soundbridge/
│   ├── __init__.py      # versão do pacote
│   ├── __main__.py      # entry point: python -m soundbridge
│   ├── main.py          # Server, Client, CLI — orquestração principal
│   ├── audio.py         # AudioCapture, AudioPlayback, VirtualMicSource
│   ├── network.py       # UDPSender, UDPReceiver, Discovery, Heartbeat
│   ├── protocol.py      # encode/decode do protocolo binário
│   ├── config.py        # constantes (portas, sample rate, packet types)
│   ├── state.py         # ConnectionState enum
│   └── gui.py           # CustomTkinter + system tray
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

## Problemas conhecidos

- **Discovery não funciona de forma confiável entre Linux e Windows.** O broadcast UDP pode ser bloqueado pelo Windows Firewall mesmo com regras adicionadas. Workaround: usar `--ip` para conexão direta.
- **Windows Firewall** bloqueia tráfego UDP de entrada por padrão. É necessário liberar as portas 4410-4413 ou desativar o firewall temporariamente para teste.
- **Streaming de áudio não foi validado end-to-end.** A captura (PulseAudio monitor), protocolo e playback estão implementados mas não foram testados com conexão real funcionando.

## TODO

- [ ] Debug e validação do streaming de áudio end-to-end
- [ ] Tornar o discovery mais robusto (mDNS/Avahi como alternativa ao broadcast UDP)
- [ ] Testar com diferentes configurações de rede e firewall
- [ ] Adicionar codec Opus para compressão de áudio
- [ ] Testar em redes com múltiplas sub-redes

## Branches

- `main` — release estável
- `develop` — staging para próxima release
- `feature/*` — branches de feature mergeadas em develop
