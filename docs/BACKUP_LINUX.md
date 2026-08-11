# Backup portátil do banco para Linux

Este documento registra a captura criada em **2026-08-11 20:34:09
America/Sao_Paulo** para migrar o File Manager a uma máquina Linux.

## Artefato publicado

| Campo | Valor |
|---|---|
| Pasta no Drive | `#Avideos/Backups` |
| Arquivo | `file-manager-linux-20260811-203409.tar.gz` |
| File ID | `1IQvcXIXka8Ydh3d5CJ4g47TNi41JHsg0` |
| Tamanho | `71.487.003` bytes |
| SHA-256 | `9d2e7f87bcb579ef2cb7dd32c9c6ce26738a44ae75ea85060f0a6283946eb094` |
| Código incluído | commit `226ae70` |
| Entradas no arquivo | 39 |

O checksum remoto retornado pela API do Google Drive é idêntico ao checksum
calculado localmente. Um sidecar
`file-manager-linux-20260811-203409.tar.gz.sha256` foi enviado para a mesma
pasta; seu File ID é `1WpdOrIf4NVYK5yP88L9TszqBJAB8ihkW`.

Links privados do Drive:

- [arquivo compactado](https://drive.google.com/file/d/1IQvcXIXka8Ydh3d5CJ4g47TNi41JHsg0/view);
- [checksum SHA-256](https://drive.google.com/file/d/1WpdOrIf4NVYK5yP88L9TszqBJAB8ihkW/view).

## Conteúdo do pacote

### PostgreSQL

- `postgres/ofc_media.dump`: `pg_dump` em formato customizado;
- `postgres/globals-without-passwords.sql`: roles sem material de senha;
- dump criado com `--no-owner --no-acl` e validado com
  `pg_restore --list`.

O formato customizado é portátil entre Windows e Linux quando restaurado por
uma versão compatível do PostgreSQL. O pacote usa PostgreSQL 18 como destino de
referência.

### SQLite canônicos

Os seis bancos foram copiados pela Online Backup API do SQLite, sem copiar
`.sqlite3`, `-wal` e `-shm` separadamente e sem interromper os escritores:

| Arquivo no pacote | Origem lógica |
|---|---|
| `sqlite/1337x.sqlite3` | inventário 1337x |
| `sqlite/filecr.sqlite3` | inventário FileCR |
| `sqlite/metadata.sqlite3` | metadados de mídia |
| `sqlite/subtitles.sqlite3` | catálogo e jobs de legendas |
| `sqlite/swarm.sqlite3` | estatísticas do swarm |
| `sqlite/stream_suite.sqlite3` | sessões e estado de streaming |

Cada cópia passou por `PRAGMA integrity_check` com resultado `ok`.

### Inventário e aplicação

- relatórios, resumos e CSVs do inventário `20260811-194302`;
- `source/file-manager-226ae70.zip`, gerado por `git archive`;
- Compose isolado do PostgreSQL e `restore/restore_linux.sh`;
- `MANIFEST.json`, `README.md` e `SHA256SUMS` internos.

## O que não está incluído

- `.env` real;
- `token.json` e `credentials.json`;
- senhas ou refresh tokens;
- payloads de mídia baixados;
- arquivos `.torrent` físicos;
- cache HLS ou arquivos temporários.

Uma auditoria dos 39 caminhos internos confirmou zero arquivos com nomes
`token.json`, `credentials.json` ou `.env`. O banco mantém os metadados e os
caminhos dos torrents, mas os diretórios físicos de torrents e mídia precisam
ser migrados separadamente caso sejam necessários na nova máquina.

## Verificação no Linux

```bash
tar -xzf file-manager-linux-20260811-203409.tar.gz
cd file-manager-linux-20260811-203409
sha256sum -c SHA256SUMS
pg_restore --list postgres/ofc_media.dump >/dev/null
```

O hash do arquivo externo também pode ser verificado antes da extração:

```bash
sha256sum -c file-manager-linux-20260811-203409.tar.gz.sha256
```

## Restauração automatizada

Use uma senha nova na máquina Linux. Não copie as credenciais OAuth do Windows.

```bash
export POSTGRES_PASSWORD='defina-uma-senha-nova-e-longa'
export CONFIRM_RESTORE=YES
bash restore/restore_linux.sh
```

O restaurador:

1. valida todos os hashes do pacote;
2. inicia `avideos-canonical-postgres` com PostgreSQL 18;
3. aguarda `pg_isready`;
4. remove e recria somente o banco `ofc_media` desse container;
5. executa `pg_restore --exit-on-error --no-owner --no-privileges`;
6. copia os SQLite para `restored-sources/`.

Defina `SQLITE_DESTINATION=/caminho/desejado` para escolher outro diretório.
Defina `POSTGRES_CONTAINER`, `POSTGRES_USER` ou `POSTGRES_DB` somente quando a
instalação de destino não usar os valores padrão.

> A restauração do PostgreSQL é destrutiva para o banco `ofc_media` de destino.
> Por isso o script recusa continuar sem `CONFIRM_RESTORE=YES`.

## Restauração manual do PostgreSQL

```bash
docker cp postgres/ofc_media.dump \
  avideos-canonical-postgres:/tmp/ofc_media.dump
docker exec avideos-canonical-postgres \
  dropdb -U ofc --if-exists --force ofc_media
docker exec avideos-canonical-postgres createdb -U ofc ofc_media
docker exec avideos-canonical-postgres pg_restore \
  -U ofc -d ofc_media --exit-on-error \
  --no-owner --no-privileges /tmp/ofc_media.dump
docker exec avideos-canonical-postgres \
  rm -f -- /tmp/ofc_media.dump
```

Depois da restauração, extraia o código incluído ou clone este repositório,
gere segredos novos, configure as montagens Linux para os seis SQLite e rode as
migrações antes de habilitar workers.

## Controles operacionais usados na captura

- nenhum terminal de coleta de `.torrent` foi interrompido;
- nenhum download torrent foi criado;
- `catalog-sync` e `transcoder` permaneceram desligados;
- o PostgreSQL foi capturado por `pg_dump` online;
- os SQLite foram lidos em modo somente leitura e copiados pela API de backup;
- a cópia local foi preservada em
  `storage/media/Backups/file-manager-linux-20260811-203409.tar.gz`;
- o diretório local `banco/` não é versionado por conter snapshots binários.
