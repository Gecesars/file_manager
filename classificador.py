"""
Regras de classificacao dos itens da pasta #AVideos.

A primeira regra que casar vence, entao a ordem importa: categorias mais
especificas (Adulto, Treinamentos, Series) vem antes de Filmes, senao um
episodio de serie cairia em algum genero de filme.

Para reclassificar um titulo, ache o padrao certo e mova/adicione o termo.
Os padroes sao regex case-insensitive aplicados ao nome do arquivo ou pasta.
"""

import os
import re

# Extensoes de legenda. O organizador ignora esses arquivos de proposito:
# ver comentario em classificar().
EXT_LEGENDA = {".srt", ".sub", ".ass", ".ssa", ".idx", ".vtt", ".smi"}

# Pastas-gaveta: o conteudo delas deve ser redistribuido e elas ficam vazias.
AGGREGATORS = {
    "Filmes", "Filmes_1", "Filmes_2", "FIlmes3", "Filme_4",
    "Novos", "novos 2", "atuais", "re3cente", "hoje", "terca", "fonomeno",
    # Gavetas tematicas: o conteudo precisa ser separado nas subpastas certas,
    # senao a pasta inteira viraria um bloco unico dentro de Treinamentos.
    "#Treinamentos", "Documents",
}

# Itens que NAO devem ser movidos, mesmo casando com alguma regra.
# "Subs" e "Screens" guardam legendas/prints de um release especifico: mover
# separaria do video a que pertencem.
SKIP = {
    "Subs", "Screens",
}

RULES = [
    ("Adulto", None,
     r"\bXXX\b|PornHub|Sweetie Fox|Teen Anal|Private\.\d|Vixen\.\d|Real Anal|"
     r"Caligula|Sal[oò].*120|Nymphomaniac|^Romance[ .]|Wetlands|wetland|"
     r"Pleasure\.2021|Immoral Tales|Erotic Sex Games|Proposta indecente|"
     r"Death Of A Porn Crew|^love\.|Tarzan.*XXX"),

    ("Treinamentos", "Gravacoes de tela",
     r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.mp4$|treinamento|EFTX_APP|"
     r"Linha Rigida|field fox|detector_cenas.*\.mp4$"),
    ("Treinamentos", "Projetos ANSYS",
     r"\.aedt$|\.sat$|HFSS_CLONE|Udemy|divisor\d|\bFMC\b|\bFMV\b|Banda Larga|"
     r"Tabela diagrama"),
    ("Treinamentos", "Documentos tecnicos",
     r"anatel|time_domain|CST_AM|painel 5\.8|^5992-|PyAEDT|Sistema Irradiante|"
     r"guia_detector|roteiro_narracao|Realize as tarefas"),

    ("Series", None,
     r"\bS\d{2}E\d{2}\b|\bS\d{2}\b.*COMPLETE|Complete\.?(Series|Season)|"
     r"Full\.Season|S01\.S02|dexter|Big Bang Theory|South Park|Doctor Who"),

    ("Documentarios", None,
     r"Cocaine Cowboys|Dawn Wall|Internets Own Boy|Seaspiracy"),

    ("Filmes", "Terror",
     r"Exorcist|Shining|Psycho|The Thing|The Others|Sixth Sense|Doctor Sleep|"
     r"Insidious|Paranormal Activity|Saw\.?X|Scream|Final Destination|The\.Ring|"
     r"Nosferatu|Longlegs|Late Night With The Devil|Speak No Evil|Barbarian|"
     r"^Pearl |^X \(2022|Midsommar|Green Room|Bone Tomahawk|Sinners|28 Years Later|"
     r"Weapons|Know What You Did|Bring Her Back|Together\.2025|Alien Romulus|"
     r"Nightingale|Strange Darling|Sleep Tight|Haute Tension|Martyrs|^REC |"
     r"Shutter|Dawn of the Dead|From Dusk Till Dawn|American Psyco|Human Centipede|"
     r"Last House On The Left|Excision|Case39|Valentine|Babysitter|Happy Death Day|"
     r"Escape Room|Ready Or Not|M3GAN|Companion|Arachnophobia|Thinner|Zombie Strippers|"
     r"Piranha|Cocaine Bear|What We Do In The Shadows|Herege|Under the Skin|"
     r"Antichrist|Irreversible|Obsession|Pathology|Poltergeist"),

    ("Filmes", "Super-herois",
     r"Iron Man|Avengers|Black Widow|Thor|Doctor\.?Strange|Spider-?Man|Thunderbolts|"
     r"Superman|X-?Men|X Men|Logan|Deadpool|Batman|Dark Knight|Joker|Wonder Woman"),

    ("Filmes", "Aventura",
     r"Indiana Jones|Indiana\.Jones|Lord of the Rings|Pirates of the Caribbean|"
     r"Jurassic|Jaws|Goonies|Cast Away|Lost City|Avatar|Man In The Iron Mask|"
     r"Titanic|How To Train Your Dragon"),

    ("Filmes", "Ficcao Cientifica",
     r"Interstellar|The Martian|Gravity|Contact|Gattaca|Children of Men|Cloud Atlas|"
     r"Minority Report|I Robot|The Island|Total Recall|Planet of the Apes|X Files|"
     r"A\.I\.|V for Vendetta|Man From the Earth|K-PAX|Hunger Games|^Her |Vanilla Sky|"
     r"Apollo 13"),

    ("Filmes", "Thriller e Crime",
     r"Pulp Fiction|Reservoir Dogs|Se7en|Silence Of The Lambs|Godfather|Primal Fear|"
     r"12 Angry Men|Rear Window|Secret Window|Little Things|Donnie Darko|Prestige|"
     r"Poker Face|8MM|Sympathy For The Devil|Late Shift"),

    ("Filmes", "Animacao",
     r"WALL-E|Finding\.?Nemo|Ratatouille|Ice Age"),

    ("Filmes", "Comedia",
     r"The Dictator|Naked Gun"),

    ("Filmes", "Drama",
     r"Schindler|Green Mile|American Beauty|Oppenheimer|Theory of Everything|"
     r"Current War|Men of Honor|Rain Man|Intouchables|Bucket List|Meet Joe Black|"
     r"City of Angels|Truman Show|Phenomenon|Eighth Grade|Brutalist|"
     r"A Complete Unknown|Race For Glory|Barbie|Crash \(1996"),

    ("Filmes", "Acao",
     r"Matrix|Terminator|Predator|RoboCop|First Blood|Rocky|Top Gun|The Rock|"
     r"Bad Boys|Ballerina|Nobody|Bullet Train|Kill Bill|Mad Max|Elite Squad|"
     r"Carandiru|Scarface|Braveheart|Gladiator|^Fury |Dunkirk|Civil War|"
     r"Hunt for Red October|Big Trouble In Little China|Blade|She Rides Shotgun|"
     r"Michael\.2026"),
]

_COMPILED = [(cat, sub, re.compile(pat, re.IGNORECASE)) for cat, sub, pat in RULES]

# Todas as pastas de destino, usado para nao tentar mover uma pasta de destino
# para dentro dela mesma numa segunda execucao.
DESTINOS = {cat for cat, _, _ in RULES} | {
    sub for _, sub, _ in RULES if sub
}


def classificar(nome):
    """Devolve (categoria, subpasta|None). (None, None) se nada casar.

    Nomes de release usam pontos no lugar de espacos ("The.Naked.Gun.2025"),
    entao cada regra e testada contra o nome original e contra uma versao
    normalizada. Testar os dois preserva as regras que dependem de pontuacao
    (`\\.aedt$`, `A\\.I\\.`, `WALL-E`) sem precisar duplicar cada padrao.
    """
    if nome in SKIP or nome in DESTINOS:
        return None, None
    # Legendas nunca sao classificadas por conta propria: quem decide o destino
    # delas e o legendas.py, que as coloca na pasta do video correspondente.
    # Classificar pelo nome separaria a legenda do video quando os padroes
    # casassem em categorias diferentes.
    if os.path.splitext(nome)[1].lower() in EXT_LEGENDA:
        return None, None
    normalizado = re.sub(r"[._]+", " ", nome)
    for cat, sub, rx in _COMPILED:
        if rx.search(nome) or rx.search(normalizado):
            return cat, sub
    return None, None
