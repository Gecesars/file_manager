# Checklist de entrega

## Isolamento

- [ ] projeto Compose é `file-manager`/`file-manager-wsl`;
- [ ] porta pública é loopback `5090` ou outra explicitamente configurada;
- [ ] serviços existentes em `5050`, `5070`, `5080` e coletores permanecem ativos;
- [ ] nenhum SQLite ou diretório `.torrent` possui montagem com escrita.

## Configuração

- [ ] `.env`, `token.json` e `credentials.json` estão ignorados;
- [ ] autorização OAuth anterior foi revogada e o token local foi regenerado;
- [ ] `OFC_GDRIVE_ROOT_ID` contém a pasta correta;
- [ ] segredos internos possuem ao menos 32 caracteres;
- [ ] diretórios de mídia têm espaço suficiente;
- [ ] quota diária de upload do Drive foi conferida.

## Catálogo

- [ ] snapshots passam na validação SQLite;
- [ ] comparação SQLite × PostgreSQL não apresenta perda;
- [ ] FileCR, 1337x, metadados e legendas exibem sync recente;
- [ ] Drive registra blobs de todos os tipos e restringe streaming a vídeo.

## Fluxos

- [ ] busca, filtros e paginação do explorador funcionam;
- [ ] torrent seleciona somente os índices solicitados;
- [ ] download local termina após verificação de tamanho;
- [ ] upload resumível retoma após interrupção simulada;
- [ ] jobs torrent e Drive retomam após reinício em cada estado intermediário;
- [ ] Drive confirma tamanho/checksum antes de `completed`;
- [ ] possível duplicata nunca é tratada como exata automaticamente;
- [ ] SRT é servido como WebVTT somente para sessão autenticada;
- [ ] HLS e Range rejeitam token inválido.
- [ ] API pública não retorna `upload_state`, URI resumível ou caminho local;
- [ ] execução CPU funciona sem GPU; override NVIDIA foi validado separadamente
      quando usado.

## Validação

```powershell
.\testar.bat
git status --short
git check-ignore -v credentials.json token.json .env
```

Não executar smoke tests com torrents reais ou escrita no Drive durante a
validação automatizada. A primeira transferência real deve ser pequena,
explicitamente selecionada e acompanhada pelo painel.
