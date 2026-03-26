# Plano de Implementacao: Camera Windows -> Linux

## Objetivo

Adicionar ao SoundBridge o compartilhamento de camera do Windows para o Linux, mantendo o audio atual intacto.

Resultado esperado:

- no client Windows, o usuario ativa a webcam local
- o video e enviado pela rede local em um pipeline separado do audio
- no server Linux, o stream vira uma camera virtual local
- apps como OBS, navegadores e Discord no Linux enxergam a camera como um `/dev/videoX`
- se o video falhar, o audio continua funcionando

## Decisao de arquitetura

### Abordagem escolhida

Usar Python como plano de controle e `ffmpeg` como hot path de video.

Pipeline alvo do MVP:

1. o Windows captura a webcam via DirectShow usando `ffmpeg`
2. o Windows codifica em H.264 com preset de baixa latencia
3. o Windows envia o stream para uma porta UDP dedicada
4. o Linux recebe via `ffmpeg`
5. o Linux decodifica e publica em uma camera virtual via `v4l2loopback`

### Motivos

- o projeto ja trabalha bem com subprocessos nativos em `soundbridge/audio.py`
- o protocolo atual em `soundbridge/protocol.py` foi feito para audio e nao deve ser esticado para video no MVP
- `v4l2loopback` e o caminho mais compativel para expor webcam virtual no Linux
- tirar o video do hot path Python reduz risco, CPU e manutencao

### O que nao entra no MVP

- transportar video no protocolo binario atual de audio
- preview de video dentro da GUI
- sincronismo A/V fino
- multiplas cameras
- multiplos peers de video
- aceleracao por hardware como requisito obrigatorio

## Requisitos funcionais

### Server Linux

- aceitar conexao de video do peer atual
- expor uma camera virtual local em `/dev/videoX`
- permitir habilitar ou desabilitar a camera virtual
- continuar operando audio mesmo se o video falhar
- mostrar na GUI o estado real da camera virtual e do stream

### Client Windows

- listar webcams disponiveis
- permitir escolher camera
- permitir habilitar ou desabilitar o compartilhamento da camera
- permitir configurar resolucao e FPS basicos
- reiniciar o pipeline se houver reconnect da sessao
- mostrar na GUI o estado real do envio de video

### CLI

- habilitar ou desabilitar webcam por flags
- permitir selecionar camera, resolucao, FPS e device virtual
- permitir listar cameras do Windows

## Requisitos nao funcionais

- audio atual nao pode regredir
- falha de `ffmpeg` ou `v4l2loopback` deve gerar erro claro, sem crash geral
- reconnect deve reconstruir o pipeline de video
- pipeline de video deve ser opcional
- comportamento default do projeto continua sem webcam ativa

## Reuso do que ja existe no projeto

### Reaproveitamento direto

- `soundbridge/main.py`
  - ciclo de vida `start`, `_start_streaming`, `stop_streaming`, `stop`
  - callbacks para GUI
- `soundbridge/network.py`
  - discovery via mDNS
  - heartbeat e reconexao
- `soundbridge/gui.py`
  - infraestrutura de janela, tray e acoplamento com `gui_callback`
- `soundbridge/config.py`
  - centralizacao de portas e defaults

### O que fica intocado no MVP

- `soundbridge/protocol.py`
- `soundbridge/opus.py`
- fluxo de audio em `soundbridge/audio.py`

## Design tecnico detalhado

### Plano de controle

O plano de controle continua sendo o proprio SoundBridge:

- descoberta do peer
- heartbeat
- start e stop de pipeline
- sinalizacao de erro para GUI e CLI
- configuracao mantida em memoria nos objetos do bridge e da GUI durante a execucao

Nao sera criado um protocolo novo de video no Python para o MVP.

Nao havera persistencia de configuracao no MVP. Ao reiniciar o app, valem os defaults ou os argumentos de CLI usados naquela execucao.

### Decisao explicita de GUI no MVP

A GUI atual sobe o bridge automaticamente ao abrir a janela. Para manter o comportamento existente e evitar uma refatoracao maior antes da implementacao do video, o MVP vai preservar esse modelo.

Regras do MVP:

- o bridge continua iniciando automaticamente ao abrir a GUI
- os controles de video refletem a configuracao atual em memoria
- ativar ou desativar webcam ou camera virtual pode iniciar ou parar o pipeline em runtime
- trocar camera, resolucao, FPS ou device virtual enquanto conectado nao sera aplicado ao vivo no MVP; a nova configuracao vale no proximo reconnect ou apos reiniciar manualmente a conexao

### Plano de dados de video

O video roda em um canal proprio, fora do protocolo de audio.

Defaults propostos:

- `VIDEO_PORT = 4412`
- codec: `H.264`
- transporte inicial: `mpegts` sobre `udp`
- resolucao default: `1280x720`
- FPS default: `30`
- device virtual default no Linux: `/dev/video10`

Motivo do `mpegts over udp` no MVP:

- setup simples
- evita implementar fragmentacao e reassembly em Python
- e suficiente para validar o produto rapidamente

Se depois houver necessidade, a fase 2 pode migrar para RTP/SDP.

### Dependencias operacionais

#### Windows

- `ffmpeg` instalado e acessivel no `PATH`
- permissao de camera no Windows

#### Linux

- `ffmpeg`
- `v4l2loopback-dkms`
- modulo `v4l2loopback` carregado
- preferencia por `exclusive_caps=1`

Exemplo de setup esperado no Linux:

```bash
sudo modprobe v4l2loopback video_nr=10 card_label="SoundBridge Camera" exclusive_caps=1
```

## Comandos de referencia do pipeline

### Sender Windows

Exemplo conceitual:

```bash
ffmpeg -f dshow -video_size 1280x720 -framerate 30 -i video="Camera Name" -an -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -f mpegts udp://SERVER_IP:4412
```

### Receiver Linux

Exemplo conceitual:

```bash
ffmpeg -fflags nobuffer -flags low_delay -i udp://0.0.0.0:4412?listen=1 -an -f v4l2 /dev/video10
```

Observacao:

- os argumentos exatos podem precisar de ajuste de pixel format e buffers durante o spike
- o plano de implementacao abaixo ja preve instrumentacao para evoluir esses comandos sem refatorar a arquitetura

## Mudancas de codigo por arquivo

### 1. `soundbridge/config.py`

Adicionar:

- `VIDEO_PORT = 4412`
- `VIDEO_DEFAULT_WIDTH = 1280`
- `VIDEO_DEFAULT_HEIGHT = 720`
- `VIDEO_DEFAULT_FPS = 30`
- `VIDEO_DEFAULT_CODEC = "h264"`
- `VIDEO_DEFAULT_DEVICE = "/dev/video10"`
- `VIRTUAL_CAMERA_LABEL = "SoundBridge Camera"`
- timeouts e retry defaults para o pipeline de video

Exemplos:

- `VIDEO_START_TIMEOUT`
- `VIDEO_RESTART_DELAY`
- `VIDEO_STATUS_POLL_INTERVAL`

### 2. Novo arquivo `soundbridge/video.py`

Responsabilidades:

- listar cameras no Windows
- montar comandos do `ffmpeg`
- iniciar e parar subprocessos de video
- monitorar saida e falhas do processo
- validar prerequisitos no Linux
- expor estado simples para `main.py`

Classes sugeridas:

#### `VideoSettings`

Campos sugeridos:

- `enabled: bool`
- `camera_name: str | None`
- `width: int`
- `height: int`
- `fps: int`
- `server_ip: str | None`
- `video_port: int`
- `virtual_device: str`

#### `FFmpegProcess`

Responsabilidades:

- encapsular `subprocess.Popen` de forma simples, sem mixins
- iniciar thread de leitura de `stderr`
- guardar ultimas linhas uteis para diagnostico
- matar processo com cleanup seguro
- expor `running`, `returncode`, `last_error`

Observacao de estilo:

- preferir classes concretas pequenas, no mesmo estilo dos wrappers de processo existentes em `soundbridge/audio.py`
- evitar hierarquias ou abstractions pesadas no MVP

#### `WindowsCameraSender`

Metodos:

- `start()`
- `stop()`
- `is_running()`
- `_build_command()`

Regras:

- falhar cedo se `ffmpeg` nao existir
- falhar cedo se a camera escolhida nao existir
- aceitar camera default se nenhuma for escolhida
- delegar gerenciamento de processo para `FFmpegProcess` ou para uma rotina privada simples, sem criar uma mini-framework interna

#### `LinuxVirtualCameraReceiver`

Metodos:

- `start()`
- `stop()`
- `is_running()`
- `_build_command()`
- `check_prerequisites()`

Regras:

- validar se `/dev/videoX` existe
- validar se o modulo `v4l2loopback` esta disponivel
- reportar erro claro se o device nao puder ser aberto
- tratar pixel format, formato bruto de saida e tamanho de frame como requisitos do spike, nao como ajuste secundario

#### Funcoes utilitarias

- `list_windows_cameras() -> list[str]`
- `find_ffmpeg() -> str | None`
- `validate_virtual_camera_device(path: str) -> tuple[bool, str | None]`

### 3. `soundbridge/main.py`

Objetivo: integrar video no ciclo de vida existente, sem misturar com o protocolo de audio.

#### Mudancas de construtor e API

As classes atuais recebem poucos argumentos. Para manter o estilo do projeto, a expansao deve ser pequena e explicita.

Assinaturas sugeridas:

- `SoundBridgeServer(gui_callback=None, video_settings: VideoSettings | None = None)`
- `SoundBridgeClient(server_ip: str | None = None, gui_callback=None, video_settings: VideoSettings | None = None)`

Regras:

- `video_settings` pode ser `None` quando o recurso estiver desativado
- evitar criar uma camada extra de configuracao persistente antes da necessidade real

#### Mudancas em `SoundBridgeServer`

Novos atributos:

- `_video_enabled`
- `_video_receiver`
- `_video_settings`

Novos comportamentos:

- se camera virtual estiver habilitada, iniciar receiver dentro de `_start_streaming()`
- parar receiver dentro de `stop_streaming()`
- limpar receiver em `_on_disconnect()` junto com o fluxo atual
- disparar eventos de GUI para estado de camera virtual

Novos metodos sugeridos:

- `_start_video_streaming()`
- `_stop_video_streaming()`
- `_emit_gui_event(event, data=None)`

Eventos esperados:

- `virtual_camera_starting`
- `virtual_camera_ready`
- `video_streaming`
- `video_stopped`
- `video_error`

#### Mudancas em `SoundBridgeClient`

Novos atributos:

- `_video_enabled`
- `_camera_sender`
- `_video_settings`

Novos comportamentos:

- se webcam estiver habilitada, iniciar sender dentro de `_start_streaming()`
- parar sender dentro de `stop_streaming()`
- limpar sender em `_on_disconnect()` junto com o fluxo atual
- disparar eventos de GUI para estado da camera

Novos metodos sugeridos:

- `_start_video_streaming()`
- `_stop_video_streaming()`
- `set_camera_enabled(enabled: bool)`
- `set_camera_settings(...)`

Eventos esperados:

- `video_starting`
- `video_streaming`
- `video_stopped`
- `video_error`

#### Mudancas em `run_server_cli` e `run_client_cli`

- passar configuracao de video para as classes
- mostrar logs claros quando video estiver habilitado

#### Observacao sobre peer atual

O app atual segue um modelo trust-based em LAN e nao faz filtragem estrita de origem em todos os receivers de audio. O MVP de video deve seguir o mesmo padrao de simplicidade.

Regra do MVP:

- a sessao de video acompanha o peer conectado e o estado de conexao do app
- filtragem rigida por IP de origem fica fora do MVP, a menos que o spike mostre necessidade tecnica imediata

#### Mudancas no parser CLI

No server:

- `--webcam`
- `--virtual-camera-device`

No client:

- `--webcam`
- `--camera-device`
- `--video-size`
- `--video-fps`
- `--list-cameras`

Regra de parsing sugerida:

- `--video-size` no formato `WIDTHxHEIGHT`

### 4. `soundbridge/gui.py`

Objetivo: adicionar controle e observabilidade de video, com layout simples e sem preview.

#### Mudancas estruturais

- aumentar altura da janela para acomodar uma nova secao de video
- manter o layout em blocos `Connection`, `Volume`, `Video`, `Actions`
- nao mudar o fluxo de tray
- respeitar o modelo atual de auto-start da GUI
- esconder ou desabilitar controles que nao fazem sentido para o modo atual

#### Client GUI

Adicionar secao `Video` com:

- checkbox `Share Webcam`
- dropdown `Camera`
- dropdown `Resolution`
- dropdown `FPS`
- botao `Refresh Cameras`
- label de status de video

Estados de status no client:

- `Camera: Off`
- `Camera: Starting`
- `Camera: Streaming`
- `Camera: Error - <motivo>`

Regras de UX no client:

- ao marcar `Share Webcam`, salvar estado local e iniciar o sender quando houver conexao
- ao desmarcar, parar sender sem desligar o audio
- durante streaming, travar camera/resolucao/FPS para evitar troca no meio da transmissao
- em erro, manter controles habilitados para retry manual
- camera, resolucao e FPS alterados durante conexao entram em vigor no proximo reconnect do MVP
- esta secao existe apenas no modo `client`

#### Server GUI

Adicionar secao `Virtual Camera` com:

- checkbox `Expose Virtual Camera`
- campo read-only com o device atual
- botao `Check Setup`
- label de status de camera virtual

Estados de status no server:

- `Virtual Camera: Disabled`
- `Virtual Camera: Preparing`
- `Virtual Camera: Ready`
- `Virtual Camera: Receiving Video`
- `Virtual Camera: Error - <motivo>`

Regras de UX no server:

- ao marcar `Expose Virtual Camera`, preparar ou validar o device virtual
- se o device existir, indicar pronto antes mesmo de haver frames
- quando frames chegarem, mudar para `Receiving Video`
- se faltar `v4l2loopback`, mostrar erro sem derrubar conexao
- mudancas no device virtual durante conexao entram em vigor no proximo reconnect do MVP
- esta secao existe apenas no modo `server`

#### Novos atributos sugeridos na GUI

- `self.video_status_var`
- `self.virtual_camera_status_var`
- `self.webcam_enabled_var`
- `self.virtual_camera_enabled_var`
- `self.camera_var`
- `self.resolution_var`
- `self.fps_var`

#### Novos metodos sugeridos na GUI

- `_build_video_ui()`
- `_refresh_camera_list()`
- `_on_webcam_toggle()`
- `_on_virtual_camera_toggle()`
- `_collect_video_settings()`
- `_apply_video_event(event, data)`

#### Mudancas em metodos existentes

##### `_build_ui()`

- criar a secao `Video`
- reorganizar ordem dos blocos para manter leitura facil

##### `_start_bridge()`

- passar configuracao inicial de video ao criar `SoundBridgeServer` e `SoundBridgeClient`
- manter o comportamento de auto-start ja existente

##### `_handle_bridge_event()`

- manter eventos `connected` e `disconnected`
- adicionar manipulacao dos eventos de video

##### `_toggle_connection()`

- preservar configuracao da GUI entre reconnects

### 5. `README.md` e `README.pt-BR.md`

Adicionar:

- novo overview de webcam sharing
- setup Linux de `v4l2loopback`
- setup Windows de `ffmpeg`
- flags novas de CLI
- troubleshooting de video

### 6. `soundbridge-client.bat`

Possivel ajuste futuro:

- documentar ou facilitar `--webcam`

Nao e obrigatorio no primeiro commit se o batch atual continuar funcional.

## Fluxo de estados

### Client

1. start do app
2. busca ou conecta ao server
3. quando o peer conecta, entra em `_start_streaming()`
4. inicia heartbeat e audio atual
5. se webcam habilitada, sobe `WindowsCameraSender`
6. GUI recebe `video_starting`
7. se processo estabiliza, GUI recebe `video_streaming`
8. em erro, GUI recebe `video_error`

### Server

1. start do app
2. espera client
3. quando o peer conecta, entra em `_start_streaming()`
4. inicia heartbeat e audio atual
5. se camera virtual habilitada, valida device
6. sobe `LinuxVirtualCameraReceiver`
7. GUI recebe `virtual_camera_ready`
8. ao receber fluxo, GUI recebe `video_streaming`
9. em erro, GUI recebe `video_error`

## Plano de implementacao por fases

### Fase 0 - Spike tecnico

Objetivo: validar a cadeia de video fora do app.

Checklist:

- validar comando de listagem de cameras no Windows
- validar sender manual com `ffmpeg`
- validar `v4l2loopback` no Linux
- validar receiver manual com `ffmpeg`
- validar o device final em OBS e navegador no Linux
- validar defaults iniciais de resolucao, FPS e pixel format
- fechar o comando final exato de sender e receiver para o MVP
- provar qual `pix_fmt` e qual formato de saida realmente funcionam com `v4l2loopback`
- confirmar compatibilidade em OBS, Chrome ou Chromium e, se possivel, Discord

Saida esperada:

- um par de comandos manualmente funcional
- lista de flags finais do `ffmpeg` a usar no codigo
- definicao explicita do formato de saida para `/dev/videoX`

### Fase 1 - Backend de video

Objetivo: criar a camada de video desacoplada da GUI.

Checklist:

- criar `soundbridge/video.py`
- implementar `find_ffmpeg`
- implementar `list_windows_cameras`
- implementar `WindowsCameraSender`
- implementar `LinuxVirtualCameraReceiver`
- implementar leitura de `stderr` para diagnostico
- implementar status interno e tratamento de stop seguro
- manter o design simples e concreto, no estilo dos wrappers atuais de `audio.py`

Critério de aceite:

- classes de video sobem e param corretamente em teste local com mocks

### Fase 2 - Integracao em `main.py`

Objetivo: encaixar video no lifecycle do app.

Checklist:

- adicionar configuracao de video em server e client
- integrar `_start_video_streaming`
- integrar `_stop_video_streaming`
- manter audio intacto
- garantir cleanup em reconnect e stop
- emitir eventos de GUI para video
- aplicar start e stop de video apenas dentro do lifecycle atual de `_start_streaming()`, `stop_streaming()` e `_on_disconnect()`

Critério de aceite:

- desconectar e reconectar recria audio e video sem duplicar processos

### Fase 3 - CLI

Objetivo: permitir uso completo sem GUI.

Checklist:

- adicionar flags novas no parser
- adicionar `--list-cameras`
- validar parse de `--video-size`
- conectar args ao `SoundBridgeServer` e `SoundBridgeClient`

Critério de aceite:

- usuario consegue ativar webcam via CLI sem GUI

### Fase 4 - GUI

Objetivo: expor controle de webcam e camera virtual de forma clara.

Checklist:

- criar bloco `Video` no client
- criar bloco `Virtual Camera` no server
- adicionar estados visuais novos
- adicionar refresh de cameras
- manter estado da GUI em reconnect
- travar controles durante streaming ativo
- manter o modelo de auto-start e aplicar configuracoes estruturais no proximo reconnect do MVP

Critério de aceite:

- usuario consegue ativar e acompanhar o estado do video sem usar terminal

### Fase 5 - Testes, docs e polimento

Objetivo: fechar release interna do recurso.

Checklist:

- testes unitarios de `video.py`
- testes de integracao de lifecycle
- atualizacao de READMEs
- troubleshooting inicial
- checklist manual de compatibilidade

Critério de aceite:

- docs suficientes para reproduzir setup do zero

## Plano de testes

### Testes unitarios

Criar `tests/test_video.py` com cobertura para:

- parsing da saida de `ffmpeg -list_devices`
- montagem de comando do sender
- montagem de comando do receiver
- validacao de prerequisitos no Linux
- stop seguro de processo ja encerrado
- captura de erro em `stderr`

### Testes de integracao

Criar `tests/test_main.py` com mocks para:

- start do server com video habilitado
- start do client com video habilitado
- reconnect limpando sender e receiver
- falha de `ffmpeg` sem quebrar audio
- falha de camera virtual sem quebrar sessao

Prioridade:

- `tests/test_video.py` e `tests/test_main.py` entram no MVP
- testes automatizados de GUI ficam fora do MVP, salvo validacoes pequenas e de baixo custo

## Diretrizes de implementacao alinhadas ao repo

- preferir classes pequenas e concretas a mixins ou hierarquias profundas
- manter `main.py` como dono do lifecycle e do reconnect
- manter `video.py` como modulo de subprocessos e validacoes de plataforma
- manter `gui.py` reagindo por eventos simples via `gui_callback`
- implementar `start()` e `stop()` de forma idempotente e restart-safe
- usar `pytest` + `unittest.mock` no mesmo estilo da suite atual
- evitar criar subsistemas de configuracao persistente, event bus ou framework de processos no MVP

### Testes manuais obrigatorios

#### Fluxo base

- audio sem webcam continua funcionando
- audio + webcam juntos funcionam
- desligar webcam nao derruba audio

#### Client Windows

- webcam integrada
- webcam USB
- camera inexistente selecionada
- camera em uso por outro app
- permissao de camera negada

#### Server Linux

- `v4l2loopback` ausente
- `/dev/video10` ausente
- device ocupado
- modulo carregado com `exclusive_caps=1`

#### Compatibilidade

- OBS Studio
- Chromium ou Chrome
- Firefox
- Discord web ou desktop

#### Resiliencia

- reconnect por perda de heartbeat
- restart do client
- restart do server
- unplug da webcam durante streaming
- sleep/resume simples

## Eventos de GUI padronizados

Para evitar ifs espalhados, padronizar payloads de evento.

Eventos sugeridos:

- `connected`
- `disconnected`
- `video_starting`
- `video_streaming`
- `video_stopped`
- `video_error`
- `virtual_camera_starting`
- `virtual_camera_ready`
- `virtual_camera_error`

Payload sugerido:

```python
{
    "message": "texto curto",
    "details": "texto opcional",
}
```

## Logs e observabilidade

O MVP deve produzir logs legiveis para diagnostico rapido.

Logs minimos esperados:

- camera selecionada
- comando iniciado de sender ou receiver
- porta e device virtual usados
- inicio e parada do processo
- erro resumido vindo de `stderr`
- reconnect limpando pipeline antigo

Nao e necessario logar cada frame.

## Riscos e mitigacoes

### 1. `ffmpeg` nao encontrado

Mitigacao:

- validar no start
- mostrar erro claro em GUI e CLI

### 2. `v4l2loopback` ausente ou mal configurado

Mitigacao:

- validar antes de subir receiver
- documentar modulo e exemplo de `modprobe`

### 3. Browser ou Discord nao aceitam o device

Mitigacao:

- usar `exclusive_caps=1`
- testar format/pix_fmt durante spike
- manter defaults conservadores

### 4. Processo de video trava ou sai silenciosamente

Mitigacao:

- monitorar retorno do subprocesso
- guardar ultimas linhas de `stderr`
- permitir retry simples pelo usuario

### 5. Reconnect deixa processo zumbi

Mitigacao:

- cleanup centralizado
- garantir `stop()` idempotente

## Definicao de pronto do MVP

O MVP esta pronto quando:

- o recurso pode ser ativado por CLI e GUI
- o client Windows lista cameras e envia uma camera selecionada
- o server Linux publica a camera em `/dev/videoX`
- OBS e um navegador no Linux conseguem abrir a camera virtual
- reconnect restaura o pipeline de video
- ausencia de prerequisito gera erro claro sem quebrar o audio

## Ordem de execucao recomendada

1. spike tecnico fora do app
2. `soundbridge/video.py`
3. integracao em `soundbridge/main.py`
4. flags de CLI
5. testes de backend
6. GUI
7. docs e checklist manual

## Proximo passo recomendado

Comecar pela Fase 0 e, assim que os comandos finais do `ffmpeg` forem validados, implementar `soundbridge/video.py` antes de tocar na GUI.
