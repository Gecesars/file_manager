#!/usr/bin/env python3
"""
Garante que toda legenda fique junto do seu video.

Regra: a legenda deve estar na MESMA pasta do video, ou numa subpasta `Subs`
dentro da pasta do video. Qualquer outra situacao e considerada legenda orfa
e o script traz ela de volta para a pasta do video.

    python legendas.py --verificar   so relata (nada e alterado)
    python legendas.py --aplicar     move as legendas desgarradas
    python legendas.py --aplicar --renomear
                                     move e renomeia para o nome do video,
                                     preservando o sufixo de idioma
                                     (Filme.mkv + legenda-por.srt -> Filme.por.srt)

O pareamento e feito pelo nome: primeiro casamento exato do radical, depois
prefixo, depois similaridade textual acima de 80%. O que nao parear com
confianca e listado como orfao e NAO e movido.
"""

import argparse
import os
import re
from difflib import SequenceMatcher

from googleapiclient.errors import HttpError

from organizar import ROOT_ID, FOLDER_MIME, autenticar, com_retry, listar_filhos

EXT_VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts"}
EXT_LEGENDA = {".srt", ".sub", ".ass", ".ssa", ".idx", ".vtt", ".smi"}

# Sufixos de idioma que os grupos de release grudam no nome da legenda.
SUFIXO_IDIOMA = re.compile(
    r"[.\-_ ]+(por|pob|pt[-_]?br|pt|ptbr|eng|en[-_]?us|en|english|spa|es|fre|fr|"
    r"ita|it|ger|de|jpn|ja|dut|swe|fin|forced|sdh|hi|cc|legendado|brazilian|"
    r"portuguese)$",
    re.IGNORECASE,
)

ANO = re.compile(r"\b(19|20)\d{2}\b")

# O titulo precisa ter corpo para uma comparacao difusa significar alguma coisa.
# Sem isso, "fin" (codigo de idioma) casa com "Finding Nemo" por prefixo.
MIN_TITULO = 8
LIMIAR_SIMILARIDADE = 0.85

PASTAS_LEGENDA = {"subs", "subtitles", "legendas", "subtitulos"}


def separar_extensao(nome):
    raiz, ext = os.path.splitext(nome)
    return raiz, ext.lower()


def normalizar(texto):
    """Reduz o nome a tokens comparaveis: minusculo, sem pontuacao de release."""
    texto = re.sub(r"[._\-\[\]()]+", " ", texto.lower())
    return re.sub(r"\s+", " ", texto).strip()


def radical_legenda(raiz):
    """Tira o sufixo de idioma para poder casar com o nome do video."""
    anterior = None
    while anterior != raiz:
        anterior = raiz
        raiz = SUFIXO_IDIOMA.sub("", raiz)
    return raiz


def idioma_de(raiz):
    """Devolve o sufixo de idioma encontrado, ou string vazia."""
    achado = SUFIXO_IDIOMA.search(raiz)
    return achado.group(1).lower() if achado else ""


def varrer(svc, pasta_id, caminho, videos, legendas, pastas):
    """Percorre a arvore inteira coletando videos, legendas e pastas."""
    for item in listar_filhos(svc, pasta_id):
        if item["mimeType"] == FOLDER_MIME:
            pastas[item["id"]] = {"nome": item["name"], "pai": pasta_id,
                                  "caminho": f"{caminho}/{item['name']}"}
            varrer(svc, item["id"], f"{caminho}/{item['name']}", videos, legendas, pastas)
            continue

        _, ext = separar_extensao(item["name"])
        registro = {**item, "pasta_id": pasta_id, "caminho": caminho}
        if ext in EXT_VIDEO:
            videos.append(registro)
        elif ext in EXT_LEGENDA:
            legendas.append(registro)


def titulo_e_ano(nome, eh_legenda):
    """Extrai (titulo, ano) de um nome de release.

    O titulo e o que vem antes do ano. Comparar o nome inteiro nao funciona:
    "Fury.2014.2160p.4K.BluRay.x265...YTS.MX" e "Logan.2017.2160p.4K.BluRay...
    YTS.MX" batem 89% porque a parte tecnica domina, e sao filmes diferentes.
    """
    raiz, _ = separar_extensao(nome)
    if eh_legenda:
        raiz = radical_legenda(raiz)
    texto = normalizar(raiz)
    achado = ANO.search(texto)
    if achado:
        return texto[:achado.start()].strip(), achado.group(0)
    return texto, None


def esta_no_lugar(legenda, pastas_com_video, pastas):
    """True se a legenda ja satisfaz a regra da colecao.

    Vale: estar numa pasta que contem video, ou numa subpasta Subs cuja pasta
    pai contem video. Isso e verificado ANTES de tentar parear por nome, senao
    arquivos de nome generico ("English.srt", "Forced.eng.srt") corretamente
    guardados em Release/Subs/ apareceriam como orfaos.
    """
    if legenda["pasta_id"] in pastas_com_video:
        return True
    pasta = pastas.get(legenda["pasta_id"])
    if pasta and pasta["nome"].lower() in PASTAS_LEGENDA:
        return pasta["pai"] in pastas_com_video
    return False


def parear(legenda, indice_titulo, videos_titulos):
    """Acha o video da legenda. Devolve (video, motivo) ou (None, motivo)."""
    titulo, ano = titulo_e_ano(legenda["name"], eh_legenda=True)
    if not titulo:
        return None, "sem titulo"

    # 1) Casamento exato de titulo. Vale para titulo de qualquer tamanho:
    #    "Fury" tem 4 letras mas "fury"+2014 == "fury"+2014 e confiavel.
    candidatos = indice_titulo.get(titulo, [])
    if candidatos:
        if ano:
            do_ano = [v for v, a in candidatos if a == ano or a is None]
            if do_ano:
                return do_ano[0], "exato" if len(do_ano) == 1 else "exato (ambiguo)"
        elif len(titulo) >= MIN_TITULO or len(candidatos) == 1:
            videos_cand = [v for v, _ in candidatos]
            return videos_cand[0], "exato" if len(videos_cand) == 1 else "exato (ambiguo)"

    # 2) Comparacao difusa: so com titulo encorpado, senao "fin" casa com
    #    "finding nemo" e "2" casa com qualquer coisa.
    if len(titulo) < MIN_TITULO:
        return None, "titulo curto demais para parear com seguranca"

    melhor, melhor_score = None, 0.0
    for video, titulo_video, ano_video in videos_titulos:
        if len(titulo_video) < MIN_TITULO:
            continue
        if ano and ano_video and ano != ano_video:
            continue  # anos conflitantes: filmes diferentes
        score = SequenceMatcher(None, titulo, titulo_video).ratio()
        if score > melhor_score:
            melhor, melhor_score = video, score

    if melhor and melhor_score >= LIMIAR_SIMILARIDADE:
        return melhor, f"titulo {melhor_score:.0%}"
    return None, f"sem par (melhor titulo {melhor_score:.0%})"


def novo_nome(legenda, video):
    raiz_leg, ext_leg = separar_extensao(legenda["name"])
    raiz_video, _ = separar_extensao(video["name"])
    idioma = idioma_de(raiz_leg)
    return f"{raiz_video}.{idioma}{ext_leg}" if idioma else f"{raiz_video}{ext_leg}"


def main():
    ap = argparse.ArgumentParser(description="Mantem legendas junto dos videos.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verificar", action="store_true", help="so relata")
    g.add_argument("--aplicar", action="store_true", help="move as desgarradas")
    ap.add_argument("--renomear", action="store_true",
                    help="renomeia a legenda para o nome do video")
    args = ap.parse_args()

    svc = autenticar()

    print("Varrendo a arvore inteira de #AVideos ...")
    videos, legendas, pastas = [], [], {}
    varrer(svc, ROOT_ID, "#AVideos", videos, legendas, pastas)
    print(f"{len(videos)} videos, {len(legendas)} legendas, {len(pastas)} pastas.\n")

    pastas_com_video = {v["pasta_id"] for v in videos}

    indice_titulo, videos_titulos = {}, []
    for video in videos:
        titulo, ano = titulo_e_ano(video["name"], eh_legenda=False)
        indice_titulo.setdefault(titulo, []).append((video, ano))
        videos_titulos.append((video, titulo, ano))

    ok = desgarradas = orfas = erros = 0
    acoes = []

    for legenda in sorted(legendas, key=lambda x: x["name"]):
        # Localizacao primeiro: se ja cumpre a regra, o nome nao importa.
        if esta_no_lugar(legenda, pastas_com_video, pastas):
            ok += 1
            continue
        video, motivo = parear(legenda, indice_titulo, videos_titulos)
        if not video:
            orfas += 1
            print(f"  ORFA      {legenda['name'][:62]:<62} {motivo}")
            continue
        desgarradas += 1
        acoes.append((legenda, video, motivo))
        destino = pastas.get(video["pasta_id"], {}).get("caminho", "#AVideos")
        print(f"  MOVER     {legenda['name'][:62]:<62} -> {destino}  ({motivo})")

    if args.aplicar:
        print()
        for legenda, video, _ in acoes:
            corpo = {}
            if args.renomear:
                corpo["name"] = novo_nome(legenda, video)
            try:
                com_retry(svc.files().update(
                    fileId=legenda["id"],
                    addParents=video["pasta_id"],
                    removeParents=legenda["pasta_id"],
                    body=corpo or None,
                    fields="id",
                ))
                if args.renomear:
                    print(f"  movida e renomeada  {corpo['name'][:60]}")
                else:
                    print(f"  movida  {legenda['name'][:60]}")
            except HttpError as err:
                erros += 1
                print(f"  ERRO    {legenda['name'][:60]} {err}")

    print(f"\n{ok} ja no lugar certo | {desgarradas} desgarradas | "
          f"{orfas} orfas (nao movidas) | {erros} erros")
    if not args.aplicar and desgarradas:
        print("VERIFICACAO - nada foi alterado. Rode com --aplicar para corrigir.")


if __name__ == "__main__":
    main()
