# Execução no WSL2

O código deve ficar no filesystem Linux, por exemplo `~/src/file_manager`.
Executar diretamente em `/mnt/d` reduz desempenho e torna as operações de HLS e
libtorrent mais suscetíveis a latência. Os inventários continuam no Windows e
são montados somente leitura.

## Preparar

```bash
cd ~/src/file_manager
cp .env.wsl.example .env
chmod 600 .env
# edite senhas, tokens, OFC_GDRIVE_ROOT_ID e caminhos se necessário
./scripts/wsl/prepare.sh
```

O script valida Ubuntu 26.04, Docker Desktop, fontes SQLite e a configuração
Compose. Ele não copia nem altera bancos.

## Validar e iniciar

```bash
./scripts/wsl/validate.sh   # testes e build; não inicia containers
./scripts/wsl/start.sh
./scripts/wsl/status.sh
```

O override usa o projeto `file-manager-wsl`, porta padrão `5091` e volumes Linux
para PostgreSQL, mídia, resume, HLS e snapshots.

## Parar

```bash
./scripts/wsl/stop.sh
```

Somente os containers deste projeto são parados. Volumes e coletores do Windows
permanecem intactos.
