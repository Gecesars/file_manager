# Arquitetura do File Manager

## Objetivos e fronteiras

O File Manager consolida metadados, mas mantém os sistemas de origem autônomos.
Os coletores continuam donos dos SQLite e dos `.torrent`; o File Manager não
inicia, pausa, reinicia ou encerra esses processos.

As responsabilidades são separadas:

- **catalog-sync**: abre somente as fontes permitidas, cria snapshots SQLite
  consistentes e faz UPSERT no catálogo;
- **PostgreSQL**: estado canônico do catálogo, sessões, jobs e auditoria;
- **Redis**: histórico curto de métricas e notificações, nunca estado canônico;
- **torrent-engine**: valida metainfo e materializa índices seletivos;
- **gdrive-source**: inventaria blobs, serve Range e executa download/upload;
- **transcoder**: sonda codecs e cria HLS sem receber caminhos arbitrários;
- **control**: API e políticas; não executa FFmpeg ou libtorrent;
- **gateway**: única porta publicada, autorização e entrega de artefatos HLS.

## Ingestão SQLite sem interromper escritores

As fontes são uma allow-list explícita. Cada ciclo:

1. abre o SQLite vivo em modo somente leitura;
2. usa a API de backup online para um arquivo temporário próprio;
3. valida o snapshot;
4. fecha imediatamente a transação de origem;
5. lê o snapshot em lotes e faz UPSERT transacional no PostgreSQL;
6. publica contagens/checksum somente após o commit.

Nunca se copia apenas o `.sqlite3`: registros recentes podem estar no WAL. Não
se usa `immutable=1`, pois ele pode ignorar o WAL de um banco vivo. Uma falha
preserva o último catálogo válido.

## Modelo canônico

Os schemas PostgreSQL são:

- `catalog`: fontes, torrents, arquivos, Drive, metadados, swarm e legendas;
- `runtime`: playback, downloads, transcodes, artefatos e transferências;
- `ops`: migrações, snapshots, cursores, heartbeats e auditoria.

Identidades importantes:

- torrent: `(site, infohash)`;
- arquivo descrito: `(torrent_id, path)` com índice determinístico de catálogo;
  antes de qualquer I/O o motor relê o metainfo e recupera o índice bencode
  real por caminho + tamanho;
- blob do Drive: `drive_file_id` — nomes e caminhos não são identidade;
- transferência: UUID persistente;
- conteúdo confirmado: `(sha256, size)`.

Sem SHA-256, nome normalizado + tamanho gera apenas presença `possible`, nunca
deduplicação automática.

## Máquina de estados de transferência

```text
queued
  └─> validating
        ├─> downloading ─> downloaded
        └────────────────> downloaded   (já estava local)
                              └─> classifying
                                    ├─> uploading ─> verifying
                                    └──────────────> verifying   (destino local)
                                                        └─> completed
```

Qualquer estado ativo pode ir para `failed` ou `cancelled`. `failed` pode voltar
a `queued`; `completed` e `cancelled` são terminais. Um trigger no PostgreSQL
rejeita saltos inválidos. Reconciliações independentes retomam jobs torrent e
Drive que ficaram em estados intermediários; cada retomada revalida manifesto,
caminhos, tamanhos e checksums antes de avançar.

### Torrent → local

O motor relê o metainfo inventariado, recalcula o infohash, compara caminho e
tamanho com o catálogo e mapeia cada item ao índice libtorrent. Todos os índices
começam em prioridade zero; somente a seleção recebe prioridade. O download usa
storage sparse, peças verificadas e fast-resume.

### Local → Drive

O worker cria pastas de forma idempotente e inicia upload resumível com um
`fileId` pré-gerado. O job persiste URI, offset e metadados por arquivo. Chunks
são múltiplos de 256 KiB; depois de uma falha, o offset real é consultado no
servidor. O estado só termina após conferir tamanho e checksum remoto.

### Drive → local

O download escreve `.part`, retoma pelo tamanho existente, valida versão,
tamanho e checksum e só então faz rename atômico. O destino precisa permanecer
dentro de `storage/media` e links/reparse points são rejeitados.

## Catálogo do Drive

O Drive é inventariado por `fileId`, incluindo todos os blobs baixáveis. Pastas
são usadas somente para classificação/apresentação. Google Docs nativos sem
blob exportável não entram no streaming. Vídeo e legenda são marcados por uma
allow-list de extensões e MIME; um executável com sufixo de vídeo no meio do nome
continua sendo software.

Shared Drives usam `supportsAllDrives=true` e
`includeItemsFromAllDrives=true`. O catálogo armazena checksums, tamanho,
`modifiedTime`, MIME, permissões e metadados de vídeo quando fornecidos.

O primeiro ciclo captura `startPageToken` e faz uma reconciliação completa. Os
ciclos seguintes consomem `changes.list` com o `driveId` real quando a raiz está
em Shared Drive. Um poll vazio apenas avança o cursor; qualquer mudança dispara
reconciliação. O novo token só é confirmado na mesma transação que publica o
catálogo, e cursores expirados são descartados com full scan seguro.

## Streaming

O browser nunca recebe OAuth do Drive nem caminho local. O Control gera token de
capacidade aleatório e armazena apenas seu hash. Sessões expiram em 12 horas por
padrão. FFprobe/FFmpeg acessa a fonte por um proxy estritamente loopback no
transcoder; a capability não aparece em argv, fingerprint ou `ffmpeg.log`.

- Drive: proxy backend de `files.get?alt=media` com uma faixa Range;
- torrent: fonte Range só lê peças já verificadas e prioriza a janela pedida;
- H.264/AAC compatível: remux;
- áudio incompatível: cópia de vídeo e transcode apenas de áudio;
- vídeo incompatível: NVENC quando disponível, CPU como fallback controlado;
- Nginx entrega somente artefatos HLS associados à sessão autorizada.

## Legendas

O catálogo importa relações do SubtitleVault. O Control resolve apenas caminhos
dentro da montagem read-only aprovada, limita a 5 MiB, aceita SRT/VTT textual e
converte timestamps SRT para WebVTT. A faixa é servida após autenticar a mesma
sessão de playback e associar `site + infohash`.

## Persistência e reinício

- PostgreSQL: volumes Docker;
- mídia materializada: `storage/media`;
- fast-resume: `storage/resume`;
- HLS: `storage/hls`;
- snapshots: `storage/snapshots`.

Parar ou reconstruir containers não apaga esses diretórios. Limpeza automática
de mídia só pode ocorrer após job `completed` e política explícita de retenção;
esta versão preserva os arquivos por padrão.

O indicador de presença local representa a última materialização concluída no
catálogo. Se o operador remover manualmente um arquivo de `storage/media`, deve
solicitar nova materialização; a versão atual não vigia deleções feitas fora da
plataforma.
