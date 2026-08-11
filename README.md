# File Manager

Painel local para unificar os inventários de torrents, os arquivos já existentes
no Google Drive e a biblioteca local. A aplicação pesquisa qualquer tipo de
arquivo, transmite vídeos do torrent ou do Drive, oferece legendas e executa o
fluxo persistente **torrent → pasta local → classificação → Google Drive**.

> Use somente com conteúdo que você tem autorização para baixar, armazenar e
> reproduzir. O File Manager não executa instaladores, não publica links anônimos
> e não transforma o Google Drive em CDN.

## O que está incluído

- ingestão consistente dos SQLite vivos do FileCR, 1337x, OMDb e SubtitleVault;
- PostgreSQL como catálogo canônico e Redis apenas para sinais transitórios;
- dashboard com cards separados e selecionáveis para Drive, FileCR, 1337x e
  Local, com status, localização, tipos e volume por fonte;
- curadoria de TV/filmes/séries com prioridades fixas, ranking por
  popularidade/perfil do Drive, exigência de legenda e publicação explícita em
  `#Avideos/TV/...` ou `#Avideos/Movies/...`;
- explorador por texto, fonte, tipo, status e presença local/Drive, com
  agrupamento e ações em lote por tipo;
- classificação em vídeo, áudio, legenda, imagem, documento, compactado,
  software, dataset e outros;
- deduplicação exata por SHA-256 + tamanho e indicação conservadora de possível
  duplicata por nome normalizado + tamanho;
- streaming privado por HTTP Range, FFprobe/FFmpeg e HLS adaptativo;
- legendas SRT do SubtitleVault convertidas para WebVTT no momento da leitura;
- downloads seletivos com `python-libtorrent`, fast-resume e prioridades apenas
  para os arquivos escolhidos;
- upload resumível e idempotente para o Drive, com pastas classificadas,
  retomada, checksums e auditoria;
- sincronização incremental pelo feed de mudanças do Drive, inclusive Shared
  Drives, com reconciliação completa somente quando necessária;
- retomada automática de downloads/uploads interrompidos após reinício;
- interface de operações com estados, bytes transferidos e erros recuperáveis.

## Regra de ouro: coletores continuam rodando

O File Manager nunca controla os terminais de coleta. Os diretórios em
`D:\dev\Torrents` entram nos containers como somente leitura. A sincronização usa
o backup online do SQLite, inclui o WAL e mantém transações curtas; ela não chama
`checkpoint`, `VACUUM`, `integrity_check` ou qualquer escrita nos bancos vivos.

Os serviços existentes em `5050`, `5070` e `5080` podem continuar ativos. Este
projeto usa o nome Docker `file-manager` e publica somente
`http://127.0.0.1:5090` por padrão.

## Arquitetura

```text
SQLite vivos (ro) ──snapshot──> catalog-sync ──UPSERT──> PostgreSQL
                                                        │
Navegador ──> Nginx ──> Control API ────────────────────┤
                │                                       ├── transfer_jobs
                ├── HLS <── FFmpeg/NVENC <── Range ─────┤
                │                                       ├── torrent-engine
                └── autorização                         └── gdrive-source

transfer_job: queued → validating → downloading → downloaded
              → classifying → uploading → verifying → completed
```

Consulte [ARCHITECTURE.md](ARCHITECTURE.md) para o modelo de dados, fronteiras de
serviço e decisões de retomada.

O procedimento de migração, o conteúdo do backup portátil e os hashes do
artefato atualmente salvo em `#Avideos/Backups` estão documentados em
[docs/BACKUP_LINUX.md](docs/BACKUP_LINUX.md).

## Início rápido no Windows

Requisitos:

- Python 3.12 para testes locais;
- Docker Desktop com Compose;
- FFmpeg e `python-libtorrent` são instalados dentro da imagem;
- GPU NVIDIA é opcional; a configuração padrão funciona somente com CPU.

1. Gere a configuração local:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\configurar.ps1
   ```

2. Edite `.env` e substitua `CONFIGURE_O_ID_DA_PASTA` pelo ID da pasta raiz do
   Drive. O arquivo é ignorado pelo Git.

3. Antes do primeiro uso deste checkout, revogue a autorização OAuth anterior,
   regenere o cliente quando aplicável e crie um `token.json` novo. Mantenha
   `credentials.json` e `token.json` somente na raiz local; ambos são ignorados
   pelo Git e o token é montado apenas no serviço do Drive. Para reautorizar:

   ```powershell
   if (-not (Test-Path .\.venv\Scripts\python.exe)) { py -3.12 -m venv .venv }
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   $env:OFC_GDRIVE_ROOT_ID = "ID_DA_PASTA"
   .\.venv\Scripts\python.exe organizar.py --teste
   ```

4. Valide sem acessar trackers nem iniciar downloads:

   ```powershell
   .\testar.bat
   ```

5. Inicie a stack isolada:

   ```powershell
   .\iniciar.bat
   ```

O inicializador padrão usa o **modo leve**: sobe os serviços em fases, reutiliza
a imagem existente e mantém `catalog-sync` e `transcoder` desligados. O painel
abrirá em <http://127.0.0.1:5090/#curation>. Isso evita que um simples start
reconstrua a imagem e ligue sincronização pesada/FFmpeg ao mesmo tempo.

Construção e modo completo são decisões explícitas:

```powershell
.\iniciar.bat -Build       # reconstrói a imagem e inicia em modo leve
.\iniciar.bat -Full        # adiciona catalog-sync e transcoder
.\iniciar.bat -Build -Full # primeira implantação completa
```

Para habilitar NVIDIA/NVENC explicitamente, use o override opcional depois de
validar o runtime da GPU:

```powershell
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

O perfil base fixa `libx264` e não solicita dispositivos GPU. O override de GPU
é a única configuração que seleciona `h264_nvenc` e solicita as GPUs NVIDIA.

### Orçamento de memória

Os limites são tetos, não memória pré-alocada. A stack contínua tem teto de
6,75 GiB; o container de migração pode acrescentar 512 MiB enquanto estiver ativo.

| Serviço | Limite | Reserva |
|---|---:|---:|
| PostgreSQL | 1280 MiB | 384 MiB |
| Redis | 384 MiB | 96 MiB |
| migração, temporário | 512 MiB | 96 MiB |
| sincronização de catálogo | 512 MiB | 128 MiB |
| torrent engine | 1536 MiB | 384 MiB |
| Google Drive | 512 MiB | 128 MiB |
| transcoder | 2048 MiB | 256 MiB |
| control | 512 MiB | 128 MiB |
| gateway | 128 MiB | 32 MiB |

O Redis limita os dados transitórios a 256 MiB e reserva o restante do teto para
overhead do processo e picos de comandos. O PostgreSQL conserva suas regulagens
de memória padrão dentro do teto de 1280 MiB; aumentos de `work_mem` devem levar
em conta que o valor pode ser usado várias vezes por consulta e conexão.
O catálogo verifica os SQLite a cada 600 segundos por padrão, reduzindo pressão
de memória e I/O durante a convivência; configure `OFC_SYNC_INTERVAL=600` também
em ambientes que já possuem `.env`.

O sincronizador percorre torrents, arquivos, metadados e legendas em lotes de
1.000 registros. Em runtime, `OFC_MAX_ACTIVE_TORRENTS=2` limita handles ativos;
`OFC_MAX_TRANSCODES=1` com `OFC_MAX_TRANSCODE_QUEUE=1` admite uma codificação e
uma espera; solicitações excedentes retornam `429` e podem ser repetidas. Apenas
os últimos `OFC_FFMPEG_LOG_TAIL_BYTES=65536` bytes do log são lidos ao concluir
um FFmpeg.

Para convivência temporária com `ofc-media-v1-wsl`, mantenha projetos, portas e
volumes distintos e não aplique novamente o Compose da stack antiga durante um
job. Duas stacks no teto exigem 13,5 GiB, além do engine e de builds; não altere o
limite global do Docker Desktop durante um job, pois essa operação reinicia o
engine. Sem essa folga disponível ao Docker, suba primeiro apenas
PostgreSQL, Redis e migração da stack nova e adie workers, playback e novos jobs
até a transferência antiga chegar a um estado terminal verificado.

## Usando o painel

### Curadoria #Avideos

A tela inicial trabalha somente com torrents de mídia do 1337x nas categorias
TV e Movies; o FileCR é excluído porque o inventário atual é de software/jogos.
Breaking Bad, Game of Thrones, Dexter e The Walking Dead aparecem sempre na
faixa de prioridades, inclusive quando não existe release válida no inventário.

O filtro padrão mostra somente candidatos com legenda separada pronta. A ação
remove samples/trailers, não reenvia vídeos com correspondência SHA-256 exata no
Drive e apresenta uma prévia antes de criar qualquer job. Legendas `.srt/.vtt`
baixadas e validadas no SubtitleVault são anexadas em `Subtitles/`. A estrutura
de destino é:

- `#Avideos/TV/<Título>/Season XX/...`;
- `#Avideos/Movies/<Título> (Ano)/...`.

O ranking combina IMDb/swarm com os gêneros mais frequentes nas pastas atuais
do Drive. É uma heurística baseada nos metadados disponíveis, não uma afirmação
de equivalência editorial. Nenhum download é disparado ao abrir ou filtrar a
tela; somente o botão **Revisar e publicar**, seguido da confirmação explícita,
cria transferências.

### Biblioteca

Pesquisa títulos das três origens (`Google Drive`, `1337x`, `FileCR`). Um vídeo
pode ser reproduzido com HLS adaptativo; o original permanece privado e os
tokens OAuth ficam apenas no backend. O navegador recebe somente uma capability
curta, com TTL de 12 horas, enquanto FFprobe/FFmpeg usa um proxy loopback sem
colocar essa capability na linha de comando ou nos logs.

### Arquivos

Mostra todos os itens descritos pelos inventários, inclusive documentos,
arquivos compactados e software. Software pode ser armazenado, mas nunca é
executado. A seleção em lote é separada por origem, torrent e tipo, com no
máximo 200 arquivos em cada job. As ações disponíveis são:

- **Manter no Local**: materializa somente os arquivos selecionados e os publica
  em `storage/media/tipo/categoria/título/caminho-relativo-original`;
- **Disponibilizar no Drive**: materializa, verifica, classifica, cria as pastas
  necessárias e envia por upload resumível, preservando a mesma subárvore;
- **Abrir**: mostra todos os arquivos do título e as legendas relacionadas.

A publicação local tenta primeiro um hardlink verificado para não duplicar os
bytes usados pelo seeding; quando o filesystem não permite, usa cópia temporária
atômica e valida SHA-256. O arquivo de seed nunca é movido nem removido. Um
download feito apenas como etapa de um envio ao Drive não entra no card Local:
para manter a cópia classificada nos dois destinos, execute também **Manter no
Local**.

### Transferências

Cada operação fica persistida em `runtime.transfer_jobs`. Rebuild ou reinício de
container não perde o histórico, a seleção, o fast-resume nem o estado de upload;
jobs intermediários são retomados automaticamente. Arquivos locais só são
considerados concluídos depois da verificação do cliente torrent; uploads só
terminam depois de confirmar tamanho e checksum no Drive. A presença local é o
último estado verificado pelo worker; remoções manuais fora do painel exigem nova
materialização.

## Operação

```powershell
.\status.bat                  # containers e saúde dos serviços
.\comparar_inventario.bat     # compara o snapshot ingerido com PostgreSQL
.\testar.bat                  # testes, sintaxe JS e compose config
.\parar.bat                   # para somente o projeto file-manager
```

`parar.bat` preserva PostgreSQL, downloads, fast-resume, cache HLS e snapshots.
Ele não envia comandos aos coletores em `D:\dev\Torrents`.

## Desenvolvimento

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
node --check src\ofc_media\static\app.js
docker compose config --quiet
docker compose -f compose.yaml -f compose.gpu.yaml config --quiet
```

Os testes usam fixtures, clientes HTTP falsos e SQLite temporário. Eles não
acessam trackers, não baixam conteúdo real, não gravam no Drive e não alteram os
bancos vivos.

## Fontes locais esperadas

| Fonte | Caminho |
|---|---|
| FileCR | `D:\dev\Torrents\FileCR\data\inventory.sqlite3` |
| 1337x | `D:\dev\Torrents\1337xVault\data\inventory.sqlite3` |
| metadados | `D:\dev\Torrents\FileCRWeb\data\catalog_metadata.sqlite3` |
| legendas | `D:\dev\Torrents\SubtitleVault\data\subtitles.sqlite3` |
| arquivos SRT | `D:\dev\Torrents\SubtitleVault\subtitles` |

Espelhos, snapshots antigos, perfis Chromium e bancos `integration_*` não são
fontes canônicas e não entram novamente no catálogo.

## Ferramentas legadas do Drive

Os scripts anteriores foram preservados:

- `organizar.py`: plano/aplicação/reversão de movimentos no Drive;
- `legendas.py`: pareamento e organização de legendas;
- `limpar.py`: diagnóstico/limpeza explícita;
- `classificador.py`: regras específicas da coleção `#AVideos`.

Eles compartilham apenas configuração e credenciais locais; não participam da
fila de transferências da plataforma.

## Segurança e limites

- gateway publicado exclusivamente em loopback;
- tokens de capacidade armazenados somente como hash;
- mutações HTTP exigem mesma origem e URLs com capabilities não entram no access
  log do gateway;
- manifestos internos e URIs de upload resumível não saem pela API pública;
- caminhos, infohash, bencode, tamanhos e índices são revalidados antes do I/O;
- nenhum segredo, banco, `.torrent`, mídia ou fast-resume é versionado;
- o uploader usa IDs pré-gerados, `appProperties`, chunks alinhados e retomada;
- o Drive não é uma CDN e o painel não deve ser exposto diretamente à internet.

Veja [SECURITY.md](SECURITY.md). A implementação segue as APIs oficiais de
[mudanças](https://developers.google.com/workspace/drive/api/guides/manage-changes),
[downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
e [uploads resumíveis](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
do Google Drive.
