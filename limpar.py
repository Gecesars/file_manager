#!/usr/bin/env python3
"""
Remove as pastas vazias que sobraram da reorganizacao.

    python limpar.py --verificar   lista o que seria removido
    python limpar.py --aplicar     manda para a lixeira

"Vazia" aqui significa vazia de verdade: nenhum arquivo em nenhum nivel da
subarvore. Uma pasta que so contem outras pastas vazias tambem conta, e o
conjunto inteiro e removido de baixo para cima.

IMPORTANTE: nada e apagado em definitivo. As pastas vao para a LIXEIRA do
Drive, onde ficam 30 dias e podem ser restauradas. Exclusao permanente e
decisao sua, feita por voce na interface do Drive.
"""

import argparse

from googleapiclient.errors import HttpError

from organizar import ROOT_ID, FOLDER_MIME, autenticar, com_retry, listar_filhos


def mapear(svc, pasta_id, caminho, nos):
    """Monta a arvore. Devolve True se houver algum arquivo na subarvore."""
    tem_arquivo = False
    filhos_pasta = []

    for item in listar_filhos(svc, pasta_id):
        if item["mimeType"] == FOLDER_MIME:
            filhos_pasta.append(item)
        else:
            tem_arquivo = True

    for filho in filhos_pasta:
        if mapear(svc, filho["id"], f"{caminho}/{filho['name']}", nos):
            tem_arquivo = True

    nos.append({
        "id": pasta_id,
        "caminho": caminho,
        "vazia": not tem_arquivo,
        "profundidade": caminho.count("/"),
    })
    return tem_arquivo


def main():
    ap = argparse.ArgumentParser(description="Remove pastas vazias de #AVideos.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verificar", action="store_true", help="so lista")
    g.add_argument("--aplicar", action="store_true", help="manda para a lixeira")
    ap.add_argument("--gavetas", action="store_true",
                    help="olha so o primeiro nivel de #AVideos (rapido); "
                         "sem isso a arvore inteira e varrida")
    args = ap.parse_args()

    svc = autenticar()

    nos = []
    if args.gavetas:
        print("Checando o primeiro nivel de #AVideos ...")
        for item in listar_filhos(svc, ROOT_ID):
            if item["mimeType"] != FOLDER_MIME:
                continue
            mapear(svc, item["id"], f"#AVideos/{item['name']}", nos)
    else:
        print("Mapeando a arvore de #AVideos ...")
        mapear(svc, ROOT_ID, "#AVideos", nos)
    print(f"{len(nos)} pastas analisadas.\n")

    # Nunca mexer na raiz, mesmo que estivesse vazia.
    vazias = [n for n in nos if n["vazia"] and n["id"] != ROOT_ID]
    # Mais profundas primeiro: remove filhas antes das maes.
    vazias.sort(key=lambda n: -n["profundidade"])

    if not vazias:
        print("Nenhuma pasta vazia. Nada a fazer.")
        return

    print(f"{len(vazias)} pastas vazias:")
    for no in vazias:
        print(f"  {no['caminho']}")

    if not args.aplicar:
        print("\nVERIFICACAO - nada foi alterado. Rode com --aplicar para remover.")
        return

    print()
    ok = erros = 0
    for no in vazias:
        try:
            # trashed=True manda para a lixeira (reversivel por 30 dias).
            # files().delete() apagaria de vez - de proposito nao usamos.
            com_retry(svc.files().update(
                fileId=no["id"], body={"trashed": True}, fields="id"))
            ok += 1
            print(f"  lixeira  {no['caminho']}")
        except HttpError as err:
            erros += 1
            print(f"  ERRO     {no['caminho']}  {err}")

    print(f"\n{ok} pastas na lixeira, {erros} erros.")
    print("Para restaurar: drive.google.com/drive/trash")


if __name__ == "__main__":
    main()
