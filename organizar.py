#!/usr/bin/env python3
"""
Organizador da pasta #AVideos no Google Drive.

    python organizar.py --teste      autentica e faz uma leitura de sanidade
    python organizar.py --plano      simula: mostra e grava o plano em CSV
    python organizar.py --aplicar    executa os movimentos de verdade
    python organizar.py --desfazer   reverte usando o log da ultima execucao

Os movimentos usam addParents/removeParents da Drive API: o arquivo troca de
pasta sem ser copiado, entao nao consome espaco nem banda.
"""

import argparse
import csv
import os
import random
import socket
import sys
import time
from datetime import datetime

# A Drive API as vezes demora a responder em lotes grandes; o padrao do socket
# derrubava a execucao no meio.
socket.setdefaulttimeout(180)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from classificador import AGGREGATORS, classificar

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_ID = os.environ.get("OFC_GDRIVE_ROOT_ID", "")  # pasta raiz, nunca versionar
FOLDER_MIME = "application/vnd.google-apps.folder"

BASE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(BASE, "credentials.json")
TOKEN = os.path.join(BASE, "token.json")
LOG_MOVIMENTOS = os.path.join(BASE, "movimentos.csv")
PLANO_CSV = os.path.join(BASE, "plano.csv")


# ---------------------------------------------------------------- autenticacao

def autenticar():
    if not ROOT_ID:
        sys.exit("Defina OFC_GDRIVE_ROOT_ID com o ID da pasta raiz do Drive.")
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not os.path.exists(CREDENTIALS):
                sys.exit(f"Falta {CREDENTIALS}")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            print("\n>>> Abrindo o navegador para voce autorizar o acesso ao Drive.")
            print(">>> O app aparece como nao verificado: Avancado -> Ir para ...\n")
            creds = flow.run_local_server(port=0)
        with open(TOKEN, "w") as fh:
            fh.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


# --------------------------------------------------------------------- helpers

def com_retry(requisicao, tentativas=5):
    """Executa uma requisicao da Drive API tolerando falhas transitorias.

    Timeout de socket e 5xx sao comuns em lotes de centenas de chamadas e nao
    significam que a operacao esta errada - so que precisa ser repetida.
    Erros 4xx (permissao, nao encontrado) sao definitivos e sobem na hora.
    """
    for tentativa in range(tentativas):
        try:
            return requisicao.execute()
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as err:
            if tentativa == tentativas - 1:
                raise
            espera = 2 ** tentativa + random.uniform(0, 1)
            print(f"    rede instavel ({type(err).__name__}), "
                  f"repetindo em {espera:.1f}s ...")
            time.sleep(espera)
        except HttpError as err:
            if err.resp.status < 500 and err.resp.status != 429:
                raise
            if tentativa == tentativas - 1:
                raise
            espera = 2 ** tentativa + random.uniform(0, 1)
            print(f"    HTTP {err.resp.status}, repetindo em {espera:.1f}s ...")
            time.sleep(espera)
    raise RuntimeError("com_retry: tentativas esgotadas")  # inalcancavel


def listar_filhos(svc, parent_id):
    itens, token = [], None
    while True:
        resp = com_retry(svc.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, parents)",
            pageSize=1000, pageToken=token,
        ))
        itens.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return itens


def garantir_pasta(svc, nome, parent_id, cache, aplicar):
    chave = (parent_id, nome)
    if chave in cache:
        return cache[chave]

    # Em simulacao o pai pode ser um placeholder que nao existe no Drive ainda;
    # consultar a API com esse id daria 404.
    if parent_id.startswith("<nova:"):
        cache[chave] = f"<nova:{parent_id[6:-1]}/{nome}>"
        return cache[chave]

    nome_q = nome.replace("'", "\\'")
    q = (f"'{parent_id}' in parents and name = '{nome_q}' "
         f"and mimeType = '{FOLDER_MIME}' and trashed = false")
    achadas = com_retry(
        svc.files().list(q=q, fields="files(id)", pageSize=1)
    ).get("files", [])

    if achadas:
        cache[chave] = achadas[0]["id"]
    elif aplicar:
        nova = com_retry(svc.files().create(
            body={"name": nome, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
        ))
        cache[chave] = nova["id"]
        print(f"  + pasta criada: {nome}")
    else:
        cache[chave] = f"<nova:{nome}>"
    return cache[chave]


def coletar(svc, parent_id, caminho, saida):
    """Percorre a raiz e as gavetas, juntando tudo que precisa ser reclassificado."""
    for item in listar_filhos(svc, parent_id):
        eh_pasta = item["mimeType"] == FOLDER_MIME
        if eh_pasta and item["name"] in AGGREGATORS:
            coletar(svc, item["id"], f"{caminho}/{item['name']}", saida)
        else:
            item["origem"] = caminho or "#AVideos"
            saida.append(item)


def montar_plano(svc, aplicar):
    print("Lendo #AVideos ...")
    itens = []
    coletar(svc, ROOT_ID, "", itens)
    print(f"{len(itens)} itens encontrados.\n")

    cache, plano, sem_regra = {}, [], []
    for item in itens:
        cat, sub = classificar(item["name"])
        if not cat:
            sem_regra.append(item)
            continue
        destino_id = garantir_pasta(svc, cat, ROOT_ID, cache, aplicar)
        if sub:
            destino_id = garantir_pasta(svc, sub, destino_id, cache, aplicar)
        plano.append((item, cat, sub, destino_id))
    return plano, sem_regra


def gravar_csv(caminho, plano, sem_regra):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["nome", "id", "tipo", "tamanho_bytes", "origem", "destino"])
        for item, cat, sub, _ in plano:
            w.writerow([item["name"], item["id"],
                        "pasta" if item["mimeType"] == FOLDER_MIME else "arquivo",
                        item.get("size", ""), item["origem"],
                        f"{cat}/{sub}" if sub else cat])
        for item in sem_regra:
            w.writerow([item["name"], item["id"],
                        "pasta" if item["mimeType"] == FOLDER_MIME else "arquivo",
                        item.get("size", ""), item["origem"], "SEM REGRA"])


def resumo(plano, sem_regra):
    contagem = {}
    for _, cat, sub, _ in plano:
        chave = f"{cat}/{sub}" if sub else cat
        contagem[chave] = contagem.get(chave, 0) + 1
    print("\nResumo do plano:")
    for chave in sorted(contagem):
        print(f"  {chave:<35} {contagem[chave]:>4}")
    print(f"  {'TOTAL A MOVER':<35} {len(plano):>4}")
    print(f"  {'sem regra (ficam onde estao)':<35} {len(sem_regra):>4}")


# ----------------------------------------------------------------------- acoes

def cmd_teste(svc):
    sobre = svc.about().get(fields="user(emailAddress),storageQuota").execute()
    print(f"Conta autenticada: {sobre['user']['emailAddress']}")

    raiz = svc.files().get(fileId=ROOT_ID, fields="id,name,mimeType").execute()
    print(f"Pasta raiz encontrada: {raiz['name']} ({raiz['id']})")

    filhos = listar_filhos(svc, ROOT_ID)
    pastas = sum(1 for f in filhos if f["mimeType"] == FOLDER_MIME)
    print(f"Primeiro nivel: {len(filhos)} itens ({pastas} pastas, "
          f"{len(filhos) - pastas} arquivos)")

    print("\nAmostra de classificacao:")
    for item in filhos[:12]:
        cat, sub = classificar(item["name"])
        destino = f"{cat}/{sub}" if sub else (cat or "SEM REGRA")
        print(f"  {item['name'][:58]:<58} -> {destino}")

    print("\nTeste OK: autenticacao, leitura e classificacao funcionando.")


def cmd_plano(svc):
    plano, sem_regra = montar_plano(svc, aplicar=False)
    for item, cat, sub, _ in plano:
        destino = f"{cat}/{sub}" if sub else cat
        print(f"  {item['name'][:60]:<60} -> {destino}")
    if sem_regra:
        print(f"\n{len(sem_regra)} itens sem regra (ficam onde estao):")
        for item in sem_regra:
            print(f"  ? {item['name']}")
    resumo(plano, sem_regra)
    gravar_csv(PLANO_CSV, plano, sem_regra)
    print(f"\nPlano gravado em {PLANO_CSV}")
    print("SIMULACAO - nada foi alterado. Rode com --aplicar para executar.")


def cmd_aplicar(svc):
    plano, sem_regra = montar_plano(svc, aplicar=True)
    resumo(plano, sem_regra)

    novo_log = not os.path.exists(LOG_MOVIMENTOS)
    with open(LOG_MOVIMENTOS, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        if novo_log:
            w.writerow(["quando", "file_id", "nome", "pais_antigos", "pai_novo"])

        ok = erros = 0
        for item, cat, sub, destino_id in plano:
            destino = f"{cat}/{sub}" if sub else cat
            antigos = ",".join(item.get("parents", []))
            try:
                com_retry(svc.files().update(
                    fileId=item["id"],
                    addParents=destino_id,
                    removeParents=antigos,
                    fields="id",
                ))
                w.writerow([datetime.now().isoformat(timespec="seconds"),
                            item["id"], item["name"], antigos, destino_id])
                fh.flush()  # log utilizavel mesmo se a execucao morrer no meio
                ok += 1
                print(f"  movido  {item['name'][:58]:<58} -> {destino}")
            except HttpError as err:
                erros += 1
                print(f"  ERRO    {item['name'][:58]:<58} {err}")

    print(f"\n{ok} itens movidos, {erros} erros.")
    print(f"Log para reverter: {LOG_MOVIMENTOS}")


def cmd_desfazer(svc):
    if not os.path.exists(LOG_MOVIMENTOS):
        sys.exit(f"Nao existe {LOG_MOVIMENTOS} - nada para desfazer.")

    with open(LOG_MOVIMENTOS, encoding="utf-8") as fh:
        linhas = list(csv.DictReader(fh, delimiter=";"))

    print(f"{len(linhas)} movimentos no log. Revertendo do mais recente para o mais antigo.")
    ok = erros = 0
    for linha in reversed(linhas):
        try:
            com_retry(svc.files().update(
                fileId=linha["file_id"],
                addParents=linha["pais_antigos"],
                removeParents=linha["pai_novo"],
                fields="id",
            ))
            ok += 1
            print(f"  revertido  {linha['nome'][:60]}")
        except HttpError as err:
            erros += 1
            print(f"  ERRO       {linha['nome'][:60]} {err}")

    print(f"\n{ok} revertidos, {erros} erros.")
    if erros == 0:
        os.rename(LOG_MOVIMENTOS, LOG_MOVIMENTOS + ".revertido")


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Organiza a pasta #AVideos no Google Drive.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--teste", action="store_true", help="checagem de sanidade")
    g.add_argument("--plano", action="store_true", help="simula e grava plano.csv")
    g.add_argument("--aplicar", action="store_true", help="executa os movimentos")
    g.add_argument("--desfazer", action="store_true", help="reverte a ultima execucao")
    args = ap.parse_args()

    svc = autenticar()

    if args.teste:
        cmd_teste(svc)
    elif args.plano:
        cmd_plano(svc)
    elif args.aplicar:
        cmd_aplicar(svc)
    elif args.desfazer:
        cmd_desfazer(svc)


if __name__ == "__main__":
    main()
