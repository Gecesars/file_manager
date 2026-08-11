from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import Settings
from .db import connection


def _count(path: Path, table: str) -> int:
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        database.close()


def compare(settings: Settings | None = None) -> dict[str, object]:
    selected = settings or Settings.from_env()
    # Compara com os snapshots exatos que alimentaram o último ciclo. Ler os
    # bancos vivos aqui geraria diferenças legítimas enquanto os coletores
    # continuam acrescentando registros.
    filecr = selected.snapshot_root / "filecr.sqlite3"
    x1337 = selected.snapshot_root / "1337x.sqlite3"
    metadata = selected.snapshot_root / "metadata.sqlite3"
    subtitles = selected.snapshot_root / "subtitles.sqlite3"
    source = {
        "filecr_torrents": _count(filecr, "filecr_torrents"),
        "filecr_files": _count(filecr, "filecr_torrent_files"),
        "1337x_torrents": _count(x1337, "torrents"),
        "1337x_files": _count(x1337, "torrent_files"),
        "metadata": _count(metadata, "catalog_metadata"),
        "subtitles": _count(subtitles, "subtitle_jobs"),
    }
    with connection(selected) as database:
        rows = database.execute(
            """
            SELECT
              count(*) FILTER(WHERE site='filecr') AS filecr_torrents,
              count(*) FILTER(WHERE site='1337x') AS x1337_torrents
            FROM catalog.torrents
            """
        ).fetchone()
        file_rows = database.execute(
            """
            SELECT
              count(*) FILTER(WHERE t.site='filecr') AS filecr_files,
              count(*) FILTER(WHERE t.site='1337x') AS x1337_files
            FROM catalog.torrent_files f JOIN catalog.torrents t ON t.id=f.torrent_id
            """
        ).fetchone()
        target = {
            "filecr_torrents": int(rows["filecr_torrents"]),
            "filecr_files": int(file_rows["filecr_files"]),
            "1337x_torrents": int(rows["x1337_torrents"]),
            "1337x_files": int(file_rows["x1337_files"]),
            "metadata": int(database.execute("SELECT count(*) AS n FROM catalog.metadata").fetchone()["n"]),
            "subtitles": int(database.execute("SELECT count(*) AS n FROM catalog.subtitles").fetchone()["n"]),
        }
    differences = {key: target[key] - source[key] for key in source}
    return {"source": source, "postgres": target, "differences": differences, "match": not any(differences.values())}


def main() -> None:
    result = compare()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["match"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
