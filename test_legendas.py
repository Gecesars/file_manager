"""
Testes da logica de pareamento legenda <-> video.

Cobre os casos que o primeiro --verificar pegou errado:
Fury casando com Logan, "fin.srt" casando com Finding Nemo, e nomes
genericos de idioma que nao podem ser pareados por nome nenhum.

    python test_legendas.py
"""

from legendas import MIN_TITULO, parear, titulo_e_ano


def video(nome, pasta="p1"):
    return {"name": nome, "id": nome, "pasta_id": pasta}


VIDEOS = [
    video("Fury.2014.2160p.4K.BluRay.x265.10bit.AAC5.1-[YTS.MX].mkv", "fury"),
    video("Logan.2017.2160p.4K.BluRay.x265.10bit.AAC5.1-[YTS.MX].mkv", "logan"),
    video("Finding.Nemo.2003.1080p.BluRay.x265-RARBG.mkv", "nemo"),
    video("Meet Joe Black (1998) [1080p].mp4", "joe"),
    video("The Dawn Wall (2017) [BluRay] [1080p] [YTS.AM].mp4", "dawn"),
    video("Superman 2025 1080p WEB-DL HEVC x265 5.1 BONE.mkv", "super"),
    video("The Shining (1980) [BluRay] [1080p] [YTS.AM].mp4", "shining"),
]

indice, titulos = {}, []
for v in VIDEOS:
    t, a = titulo_e_ano(v["name"], eh_legenda=False)
    indice.setdefault(t, []).append((v, a))
    titulos.append((v, t, a))


CASOS = [
    # (nome da legenda, pasta_id esperada ou None, descricao)
    ("Fury.2014.2160p.4K.BluRay.x265.10bit.AAC5.1-[YTS.MX]-eng.srt",
     "fury", "Fury deve casar com Fury, nao com Logan"),
    ("fin.srt", None, "codigo de idioma nao pode casar com Finding Nemo"),
    ("dut.srt", None, "codigo de idioma sem par"),
    ("English.srt", None, "nome generico sem par"),
    ("2_English.srt", None, "nome generico sem par"),
    ("SDH.eng.HI.srt", None, "nome generico sem par"),
    ("Meet Joe Black-Brazilian-portuguese.srt",
     "joe", "sufixos de idioma encadeados devem ser removidos"),
    ("The.Dawn.Wall.2017.1080p.BluRay.x264-[YTS.AM]-por.srt",
     "dawn", "titulo e ano batem"),
    ("Superman 2025 1080p WEB-DL HEVC x265 5.1 BONE-por.srt",
     "super", "casamento exato"),
    ("The.Shining.1980.1080p.BluRay.x264-[YTS.AM]-por.srt",
     "shining", "grafias diferentes do mesmo titulo"),
]


def main():
    falhas = 0
    for nome, esperado, descricao in CASOS:
        achado, motivo = parear({"name": nome, "pasta_id": "solto"}, indice, titulos)
        obtido = achado["pasta_id"] if achado else None
        if obtido == esperado:
            print(f"  ok    {nome[:52]:<52} -> {obtido}  ({motivo})")
        else:
            falhas += 1
            print(f"  FALHA {nome[:52]:<52} -> {obtido}, esperado {esperado}")
            print(f"        {descricao}")

    # Sanidade da extracao de titulo/ano
    t, a = titulo_e_ano("Fury.2014.2160p.4K.BluRay.mkv", eh_legenda=False)
    assert (t, a) == ("fury", "2014"), (t, a)
    t, a = titulo_e_ano("Meet Joe Black (1998) [1080p].mp4", eh_legenda=False)
    assert (t, a) == ("meet joe black", "1998"), (t, a)
    t, _ = titulo_e_ano("Superman 2025 BONE-por.srt", eh_legenda=True)
    assert t == "superman", t
    assert MIN_TITULO >= 8
    print("\n  ok    extracao de titulo e ano")

    print(f"\n{len(CASOS)} casos, {falhas} falhas")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
