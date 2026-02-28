# SoundBridge

> **Status: WIP** — Discovery e conexão entre Linux e Windows funcionam via mDNS (zeroconf). Streaming de áudio do sistema ainda precisa de ajuste para PipeWire. Veja [Problemas conhecidos](#problemas-conhecidos).

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

mDNS (zeroconf)  ◄──── service discovery ──►  mDNS (zeroconf)
Heartbeat        ◄──── UDP :4413 ──────────►  Heartbeat
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

### Fluxo de conexão

1. Server registra serviço mDNS `_soundbridge._udp.local.` e escuta heartbeats na porta 4413
2. Client descobre o server via mDNS (zeroconf ServiceBrowser)
3. Client inicia streaming e envia heartbeat para o server
4. Server recebe o primeiro heartbeat, identifica o IP do client e inicia streaming
5. Se heartbeat falha (5s sem resposta), desconecta e reinicia discovery

## Requisitos

- Python 3.12+
- uv (gerenciador de pacotes)
- PulseAudio ou PipeWire com compatibilidade PulseAudio (server Linux)

## Instalação

```bash
git clone https://github.com/AndersonFirmino/SoundBridge.git
cd SoundBridge
uv sync
```

## Configuração de rede (IMPORTANTE)

A comunicação entre as máquinas usa UDP, que é frequentemente bloqueado por firewalls. **Sem essas configurações, o SoundBridge não conecta.**

### Windows Firewall

Liberar o Python no firewall (PowerShell como administrador):

```powershell
# Liberar o executável do Python (ajuste o caminho se necessário)
netsh advfirewall firewall add rule name="Python SoundBridge" dir=in action=allow program="C:\Users\SEU_USUARIO\AppData\Local\Programs\Python\Python3XX\python.exe" enable=yes protocol=any

# Ou liberar as portas específicas
netsh advfirewall firewall add rule name="SoundBridge UDP In" dir=in action=allow protocol=UDP localport=4410-4413
netsh advfirewall firewall add rule name="SoundBridge UDP Out" dir=out action=allow protocol=UDP remoteport=4410-4413

# Liberar ICMP (ping) para debug
netsh advfirewall firewall add rule name="Allow Ping" dir=in action=allow protocol=ICMPv4
```

### Linux Firewall (iptables)

Se o Linux estiver com firewall ativo, liberar UDP:

```bash
sudo iptables -I INPUT -p udp --dport 4410:4413 -j ACCEPT
```

Para tornar persistente:

```bash
# Debian/Ubuntu/Mint
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

### Verificação de conectividade

Antes de rodar o SoundBridge, verificar se as máquinas se comunicam:

```bash
# 1. Ping (do Linux para o Windows)
ping 192.168.0.X

# 2. Teste UDP — no Linux, escutar:
python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(15); s.bind(('0.0.0.0',4412)); print('listening...'); data,addr=s.recvfrom(128); print(f'OK: {data} from {addr}')"

# No Windows, enviar:
python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'test', ('IP_DO_LINUX', 4412)); print('sent')"
```

Se o teste UDP falhar mas o ping funcionar, o problema é firewall. Se ambos falharem, verificar se as máquinas estão na mesma sub-rede e se o roteador tem **AP Isolation** desativado.

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

```powershell
# Com GUI — duplo clique no soundbridge-client.bat
# Ou via terminal:
uv run python -m soundbridge client

# Sem GUI
uv run python -m soundbridge client --no-gui

# Conectar direto a um IP (sem discovery)
uv run python -m soundbridge client --ip 192.168.0.5
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
├── test_network.py     # UDPSender com mock, Discovery (zeroconf mock), decode
└── test_state.py       # ConnectionState enum
```

## Estrutura do projeto

```
soundbridge/
├── pyproject.toml          # dependências e build config (uv + hatchling)
├── uv.lock                 # lockfile para installs reproduzíveis
├── soundbridge-client.bat  # launcher Windows
├── soundbridge/
│   ├── __init__.py         # versão do pacote
│   ├── __main__.py         # entry point: python -m soundbridge
│   ├── main.py             # Server, Client, CLI — orquestração principal
│   ├── audio.py            # AudioCapture, AudioPlayback, VirtualMicSource
│   ├── network.py          # UDPSender, UDPReceiver, Discovery (zeroconf), Heartbeat
│   ├── protocol.py         # encode/decode do protocolo binário
│   ├── config.py           # constantes (portas, sample rate, packet types)
│   ├── state.py            # ConnectionState enum
│   └── gui.py              # CustomTkinter + system tray
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
| `HEARTBEAT_PORT` | 4413 | UDP — heartbeat |
| `HEARTBEAT_TIMEOUT` | 5.0s | Tempo sem heartbeat = desconexão |

## Problemas conhecidos

- **Áudio do sistema não funciona com PipeWire.** O `sounddevice` lista dispositivos ALSA em vez dos monitores do PipeWire. O `find_pulse_monitor()` não encontra o monitor. Workaround em investigação.
- **Windows Firewall bloqueia UDP por padrão.** É necessário liberar o executável do Python ou as portas 4410-4413. Veja [Configuração de rede](#configuração-de-rede-importante).
- **Linux iptables pode bloquear UDP.** Liberar portas 4410-4413 com `iptables`. Veja [Configuração de rede](#configuração-de-rede-importante).

## TODO

- [ ] Corrigir captura de áudio do sistema com PipeWire (monitor não detectado pelo sounddevice)
- [ ] Validação end-to-end do streaming de áudio
- [ ] Adicionar codec Opus para compressão
- [ ] Testar em redes com múltiplas sub-redes

## Branches

- `main` — release estável
- `develop` — staging para próxima release
- `feature/*` — branches de feature mergeadas em develop
