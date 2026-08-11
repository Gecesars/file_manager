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
- explorador por texto, fonte, tipo e presença local/Drive;
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

O painel abrirá em <http://127.0.0.1:5090>.

Para habilitar NVIDIA/NVENC explicitamente, use o override opcional depois de
validar o runtime da GPU:

```powershell
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

## Usando o painel

### Biblioteca

Pesquisa títulos das três origens (`Google Drive`, `1337x`, `FileCR`). Um vídeo
pode ser reproduzido com HLS adaptativo; o original permanece privado e os
tokens OAuth ficam apenas no backend. O navegador recebe somente uma capability
curta, com TTL de 12 horas, enquanto FFprobe/FFmpeg usa um proxy loopback sem
colocar essa capability na linha de comando ou nos logs.

### Arquivos

Mostra todos os itens descritos pelos inventários, inclusive documentos,
arquivos compactados e software. Software pode ser armazenado, mas nunca é
executado. As ações disponíveis são:

- **Baixar local**: materializa somente os arquivos selecionados em
  `storage/media`;
- **Disponibilizar no Drive**: materializa, verifica, classifica, cria as pastas
  necessárias e envia por upload resumível;
- **Abrir**: mostra todos os arquivos do título e as legendas relacionadas.

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
