# Segurança

## Modelo de uso

O File Manager é uma aplicação privada de loopback para conteúdo autorizado.
Não exponha o gateway diretamente à internet e não use Google Drive como CDN ou
para distribuição pública. O operador é responsável pelos direitos e pelos
termos dos trackers, provedores de legendas e Google Drive.

## Segredos

Nunca versione:

- `.env`;
- `credentials.json`, `token.json` ou `client_secret*.json`;
- chaves OMDb/provedores;
- bancos SQLite, `.torrent`, fast-resume, mídia ou logs com dados pessoais.

O `.gitignore` cobre esses padrões. Antes de qualquer push, execute:

```powershell
git status --short
git check-ignore -v credentials.json token.json .env
```

Antes do primeiro uso deste checkout, remova o acesso OAuth anterior em
[Conexões da Conta do Google](https://support.google.com/accounts/answer/13533235?hl=PT),
regenere o cliente OAuth quando aplicável e reautorize para produzir um token
novo. Nunca cole tokens em issue, commit, terminal compartilhado ou log.

O OAuth permanece somente no `gdrive-source`. Tokens de playback são aleatórios,
curtos, expiram em 12 horas por padrão e são armazenados como SHA-256 com pepper
local. O proxy loopback do transcoder evita inserir a capability em argv,
fingerprint ou `ffmpeg.log`.

## Isolamento

- Nginx é o único serviço com porta publicada e escuta `127.0.0.1`;
- PostgreSQL, Redis e serviços internos ficam em rede privada;
- containers removem capabilities, usam `no-new-privileges` e filesystem
  read-only quando possível;
- fontes SQLite e metainfo são montadas somente leitura;
- diretórios de mídia, HLS, resume e snapshot são separados;
- requisições entre serviços exigem Bearer interno de pelo menos 32 caracteres.
- mutações do navegador recusam `Origin`/`Sec-Fetch-Site` de terceiros;
- o gateway não grava access log, pois URLs HLS carregam capabilities de sessão.

## Validação de arquivos

- infohash hexadecimal e bencode são recalculados;
- caminhos absolutos, `..`, nomes de dispositivo, symlinks e junctions são
  rejeitados antes de I/O;
- cada arquivo é comparado por índice, caminho e tamanho com o metainfo;
- extensões de vídeo são uma allow-list; `filme.mkv.exe` não é vídeo;
- nenhum conteúdo baixado é executado ou passado ao shell;
- FFmpeg recebe uma URL interna autorizada, não um caminho fornecido pelo usuário;
- checksums remotos são conferidos antes de concluir uploads/downloads.

## SQLite vivo

Não execute checkpoint, `VACUUM`, `integrity_check`, migração ou transação longa
nos bancos dos coletores. O WAL pode conter a maior parte dos dados recentes. A
única integração aceita é snapshot online curto e somente leitura.

## Google Drive

- IDs e URLs de sessão são validados antes de requisições;
- upload usa ID pré-gerado, sessão resumível e `appProperties` mínimas;
- `Range` múltiplo e respostas que ignoram a faixa são rejeitados;
- somente blobs com `capabilities.canDownload` entram em streaming;
- o access token nunca é enviado ao navegador;
- URIs de sessão de upload e manifestos com caminhos internos ficam no
  PostgreSQL e não são projetados pela API pública;
- arquivos locais não são apagados automaticamente após upload.

## Relato de vulnerabilidade

Não abra uma issue pública com token, nome de arquivo pessoal, infohash privado ou
caminho local. Revogue imediatamente qualquer credencial exposta e envie ao
mantenedor apenas passos mínimos de reprodução, sem dados reais.
