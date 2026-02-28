# SoundBridge — Regras do Projeto

## Git Flow

Branch model: gitflow.

- `main` — release estável, só recebe merges de `develop`
- `develop` — staging, recebe merges de `feature/*`
- `feature/*` — branches de feature, criadas a partir de `develop`
- `fix/*` — branches de bugfix, criadas a partir de `develop`

Fluxo:
1. `git checkout develop`
2. `git checkout -b feature/nome-da-feature`
3. Implementa, commita
4. Merge em `develop`
5. Quando estável, merge `develop` em `main`

**Nunca commitar direto na `main` ou `develop`.**

## Stack

- Python 3.12+
- `uv` como gerenciador de pacotes (não pip/poetry)
- Testes: `pytest` (`uv run pytest -v tests/`)

## Rede

- Discovery: zeroconf (mDNS), serviço `_soundbridge._udp.local.`
- Streaming: UDP raw com protocolo binário custom (portas 4410-4413)
- Heartbeat: UDP porta 4413, timeout 5s

## Firewall (necessário para funcionar)

- **Windows**: liberar `python.exe` no firewall ou portas UDP 4410-4413
- **Linux**: `sudo iptables -I INPUT -p udp --dport 4410:4413 -j ACCEPT`
