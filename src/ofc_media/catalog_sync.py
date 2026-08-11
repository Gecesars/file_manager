from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from psycopg.types.json import Jsonb

from .config import Settings
from .db import connection
from .file_kinds import classify_file, normalize_sha256
from .heartbeat import beat
from .safety import safe_owned_path, safe_relative_path


LOG = logging.getLogger("ofc.catalog_sync")
STALE_SNAPSHOT_RE = re.compile(
    r"^\.(?:filecr|1337x|metadata|subtitles)\.[0-9a-f]{32}\.tmp(?:-(?:journal|shm|wal))?$"
)


def _batches(values: Iterable[tuple[Any, ...]], size: int = 1000) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _execute_batches(database: Any, sql: str, batches: Iterable[list[tuple[Any, ...]]]) -> None:
    with database.cursor() as cursor:
        for batch in batches:
            cursor.executemany(sql, batch)


def _cleanup_stale_snapshots(target_root: Path, minimum_age_seconds: int = 600) -> int:
    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds nao pode ser negativo")
    target_root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - minimum_age_seconds
    removed = 0
    for item in target_root.iterdir():
        if not STALE_SNAPSHOT_RE.fullmatch(item.name):
            continue
        status = item.lstat()
        if (
            item.is_symlink()
            or item.is_dir()
            or (minimum_age_seconds > 0 and status.st_mtime > cutoff)
        ):
            continue
        safe_owned_path(target_root, item.name).unlink()
        removed += 1
    return removed


def _stable_file_snapshot(source: Path, temporary: Path) -> None:
    """Copia SQLite sem locks apenas quando a origem permanece comprovadamente estável.

    Alguns binds Windows/WSL permitem leitura de bytes, mas não locks POSIX. O
    backup SQLite continua sendo a via principal; este fallback recusa bancos
    com sidecars, alterações durante a cópia ou qualquer falha de integridade.
    """
    sidecars = [Path(f"{source}{suffix}") for suffix in ("-journal", "-wal", "-shm")]
    before = source.stat()
    if any(item.exists() for item in sidecars):
        raise sqlite3.OperationalError("sidecar SQLite ativo; copia estavel recusada")
    with source.open("rb") as reader, temporary.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    signature_before = (before.st_size, before.st_mtime_ns)
    signature_after = (after.st_size, after.st_mtime_ns)
    if signature_before != signature_after or any(item.exists() for item in sidecars):
        raise sqlite3.OperationalError("SQLite mudou durante a copia estavel")
    with sqlite3.connect(
        f"file:{temporary.as_posix()}?mode=ro", uri=True, timeout=30
    ) as check_database:
        check_database.execute("PRAGMA query_only=ON")
        check = check_database.execute("PRAGMA quick_check").fetchone()
    if not check or check[0] != "ok":
        raise sqlite3.DatabaseError(f"copia estavel invalida: {check!r}")


def _snapshot(source: Path, target_root: Path, name: str) -> tuple[Path, os.stat_result]:
    if not source.is_file():
        raise FileNotFoundError(source)
    target_root.mkdir(parents=True, exist_ok=True)
    target = safe_owned_path(target_root, f"{name}.sqlite3")
    source_stat = source.stat()
    last_error: sqlite3.Error | None = None
    for attempt in range(1, 6):
        temporary = safe_owned_path(target_root, f".{name}.{uuid.uuid4().hex}.tmp")
        source_db: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source_db = sqlite3.connect(
                f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30
            )
            destination = sqlite3.connect(temporary, timeout=30)
            source_db.execute("PRAGMA query_only=ON")
            # A cópia inteira preserva um snapshot consistente mesmo quando o
            # coletor continua gravando. Cópias paginadas podem nunca alcançar
            # o fim de um banco que recebe escrita contínua.
            source_db.backup(destination)
            destination.commit()
            check = destination.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError(f"snapshot invalido: {check!r}")
            destination.close()
            destination = None
            source_db.close()
            source_db = None
            os.replace(temporary, target)
            return target, source_stat
        except sqlite3.OperationalError as exc:
            if destination is not None:
                destination.close()
                destination = None
            if source_db is not None:
                source_db.close()
                source_db = None
            for suffix in ("", "-journal", "-shm", "-wal"):
                safe_owned_path(target_root, temporary.name + suffix).unlink(missing_ok=True)
            try:
                _stable_file_snapshot(source, temporary)
                os.replace(temporary, target)
                LOG.warning(
                    "backup SQLite indisponivel para %s; usada copia estavel validada",
                    name,
                )
                return target, source_stat
            except (OSError, sqlite3.Error) as fallback_error:
                last_error = sqlite3.OperationalError(
                    f"backup falhou ({exc}); fallback recusado ({fallback_error})"
                )
                LOG.warning(
                    "snapshot %s falhou na tentativa %s/5: %s",
                    name,
                    attempt,
                    last_error,
                )
                if attempt < 5:
                    time.sleep(attempt * 2)
        finally:
            if destination is not None:
                destination.close()
            if source_db is not None:
                source_db.close()
            for suffix in ("", "-journal", "-shm", "-wal"):
                safe_owned_path(target_root, temporary.name + suffix).unlink(missing_ok=True)
    assert last_error is not None
    raise last_error


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _relative_metainfo(local_path: str, host_root: str) -> str:
    value = local_path.replace("\\", "/")
    base = host_root.replace("\\", "/").rstrip("/")
    if value.casefold().startswith(base.casefold() + "/"):
        value = value[len(base) + 1 :]
    else:
        value = PurePosixPath(value).name
    return safe_relative_path(value)


def _prepared_file_row(item: dict[str, Any]) -> dict[str, Any]:
    infohash = str(item.get("infohash") or "").strip().casefold()
    path = safe_relative_path(str(item.get("path") or ""))
    size = int(item.get("size") or 0)
    if size < 0:
        raise ValueError(f"tamanho de arquivo negativo: {infohash}:{path}")
    classification = classify_file(path, item.get("mime_type"))
    raw_sha256 = item.get("sha256")
    return {
        "infohash": infohash,
        "path": path,
        "size": size,
        "extension": classification.extension,
        "file_kind": classification.file_kind,
        "mime_type": classification.mime_type,
        "is_video": classification.file_kind == "video",
        "is_subtitle": classification.is_subtitle,
        "sha256": normalize_sha256(str(raw_sha256)) if raw_sha256 else None,
    }


def _index_prepared_file_rows(
    prepared: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    prepared.sort(
        key=lambda item: (item["infohash"], item["path"].casefold(), item["path"])
    )
    current_infohash: str | None = None
    file_index = 0
    previous_key: tuple[str, str] | None = None
    for item in prepared:
        key = (item["infohash"], item["path"])
        if key == previous_key:
            raise ValueError(f"arquivo de torrent duplicado: {key[0]}:{key[1]}")
        previous_key = key
        if item["infohash"] != current_infohash:
            current_infohash = item["infohash"]
            file_index = 0
        item["file_index"] = file_index
        file_index += 1
        yield item


def _canonical_file_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize arbitrary file rows and return the historical sorted list API."""

    return list(_index_prepared_file_rows([_prepared_file_row(item) for item in rows]))


def _canonical_file_stream(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Normalize a cursor grouped by infohash while buffering one torrent only.

    File paths are sorted in Python per torrent, preserving the exact historical
    ``casefold`` order even when SQLite and Python disagree for Unicode text.
    """

    current_infohash: str | None = None
    prepared: list[dict[str, Any]] = []
    for raw in rows:
        item = _prepared_file_row(raw)
        infohash = str(item["infohash"])
        if current_infohash is None:
            current_infohash = infohash
        elif infohash != current_infohash:
            if infohash < current_infohash:
                raise ValueError("arquivos de torrent fora da ordem por infohash")
            yield from _index_prepared_file_rows(prepared)
            prepared = []
            current_infohash = infohash
        prepared.append(item)
    if prepared:
        yield from _index_prepared_file_rows(prepared)


class CatalogSynchronizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run_once(self) -> dict[str, Any]:
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE ops.ingestion_runs
                SET status='failed',error='recuperado apos interrupcao do ciclo anterior',
                    finished_at=now()
                WHERE status='running' AND started_at < now() - interval '5 minutes'
                """
            )
            database.commit()
        removed = _cleanup_stale_snapshots(self.settings.snapshot_root)
        if removed:
            LOG.info("snapshots temporarios obsoletos removidos: %s", removed)
        results: dict[str, Any] = {}
        sources = (
            ("filecr", self.settings.filecr_db),
            ("1337x", self.settings.x1337_db),
            ("metadata", self.settings.metadata_db),
            ("subtitles", self.settings.subtitle_db),
        )
        snapshots: dict[str, tuple[Path, os.stat_result]] = {}
        beat("sync", "healthy", {"phase": "snapshotting"})
        for name, source in sources:
            LOG.info("criando snapshot somente-leitura: %s", name)
            try:
                snapshots[name] = _snapshot(source, self.settings.snapshot_root, name)
            except sqlite3.OperationalError:
                previous = safe_owned_path(self.settings.snapshot_root, f"{name}.sqlite3")
                if not previous.is_file():
                    raise
                LOG.exception(
                    "snapshot atual de %s indisponivel; usando a ultima copia validada", name
                )
                snapshots[name] = (previous, previous.stat())
        beat("sync", "healthy", {"phase": "importing"})
        LOG.info("importando inventario FileCR")
        results["filecr"] = self._sync_filecr(*snapshots["filecr"])
        LOG.info("importando inventario 1337x")
        results["1337x"] = self._sync_1337x(*snapshots["1337x"])
        LOG.info("importando metadados")
        results["metadata"] = self._sync_metadata(*snapshots["metadata"])
        LOG.info("importando legendas")
        results["subtitles"] = self._sync_subtitles(*snapshots["subtitles"])
        beat("sync", "healthy", results)
        return results

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        return database

    def _start_run(self, source: str, stat: os.stat_result) -> uuid.UUID:
        run_id = uuid.uuid4()
        with connection(self.settings) as database:
            database.execute(
                "INSERT INTO ops.ingestion_runs(id,source,status,source_bytes,source_mtime_ns) VALUES(%s,%s,'running',%s,%s)",
                (run_id, source, stat.st_size, stat.st_mtime_ns),
            )
            database.commit()
        return run_id

    def _finish_run(
        self,
        run_id: uuid.UUID,
        source: str,
        stat: os.stat_result,
        counts: dict[str, int],
    ) -> dict[str, int]:
        checksum = hashlib.sha256(
            json.dumps(counts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        total = sum(counts.values())
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE ops.ingestion_runs SET status='done', rows_read=%s,
                    rows_written=%s, counts=%s, checksum=%s, finished_at=now()
                WHERE id=%s
                """,
                (total, total, Jsonb(counts), checksum, run_id),
            )
            database.execute(
                """
                INSERT INTO ops.ingest_checkpoints(source,source_bytes,source_mtime_ns,checksum)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(source) DO UPDATE SET source_bytes=excluded.source_bytes,
                  source_mtime_ns=excluded.source_mtime_ns,checksum=excluded.checksum,
                  updated_at=now()
                """,
                (source, stat.st_size, stat.st_mtime_ns, checksum),
            )
            database.execute(
                """
                INSERT INTO catalog.sources(site,kind,source_path,last_snapshot_at,last_synced_at,
                  source_bytes,source_mtime_ns,row_counts,last_error)
                VALUES(%s,'sqlite',%s,now(),now(),%s,%s,%s,NULL)
                ON CONFLICT(site) DO UPDATE SET last_snapshot_at=now(),last_synced_at=now(),
                  source_bytes=excluded.source_bytes,source_mtime_ns=excluded.source_mtime_ns,
                  row_counts=excluded.row_counts,last_error=NULL
                """,
                (source, source, stat.st_size, stat.st_mtime_ns, Jsonb(counts)),
            )
            database.commit()
        return counts

    def _sync_filecr(self, path: Path, stat: os.stat_result) -> dict[str, int]:
        run_id = self._start_run("filecr", stat)
        source = self._open(path)
        try:
            torrent_rows = source.execute(
                """
                SELECT t.*,p.name,p.primary_category,p.application_category
                FROM filecr_torrents t LEFT JOIN products p ON p.source_url=t.source_url
                """
            )
            torrent_count = self._upsert_torrents(
                "filecr", (_row(row) for row in torrent_rows)
            )
            file_rows = source.execute(
                """
                SELECT infohash,path,size,NULL AS sha256
                FROM filecr_torrent_files
                ORDER BY lower(trim(infohash)) COLLATE BINARY,path COLLATE BINARY
                """
            )
            file_count = self._upsert_files(
                "filecr",
                (_row(row) for row in file_rows),
                ordered_by_infohash=True,
            )
        finally:
            source.close()
        return self._finish_run(
            run_id,
            "filecr",
            stat,
            {"torrents": torrent_count, "files": file_count},
        )

    def _sync_1337x(self, path: Path, stat: os.stat_result) -> dict[str, int]:
        run_id = self._start_run("1337x", stat)
        source = self._open(path)
        try:
            torrent_rows = source.execute(
                """
                SELECT t.*,c.title,c.category,c.uploader,c.download_url,
                       c.seeders,c.leechers,c.peer_count
                FROM torrents t LEFT JOIN candidates c ON c.detail_url=t.detail_url
                """
            )
            torrent_count = self._upsert_torrents(
                "1337x", (_row(row) for row in torrent_rows)
            )
            file_rows = source.execute(
                """
                SELECT infohash,path,size,NULL AS sha256
                FROM torrent_files
                ORDER BY lower(trim(infohash)) COLLATE BINARY,path COLLATE BINARY
                """
            )
            file_count = self._upsert_files(
                "1337x",
                (_row(row) for row in file_rows),
                ordered_by_infohash=True,
            )
        finally:
            source.close()
        self._sample_swarm()
        return self._finish_run(
            run_id,
            "1337x",
            stat,
            {"torrents": torrent_count, "files": file_count},
        )

    def _upsert_torrents(self, site: str, rows: Iterable[dict[str, Any]]) -> int:
        host_root = (
            self.settings.filecr_host_torrent_root
            if site == "filecr"
            else self.settings.x1337_host_torrent_root
        )

        count = 0

        def values() -> Iterator[tuple[Any, ...]]:
            nonlocal count
            for item in rows:
                count += 1
                source_url = str(item.get("source_url") or item.get("detail_url") or "")
                title = str(item.get("title") or item.get("name") or item.get("display_name") or "")
                category = str(
                    item.get("category")
                    or item.get("primary_category")
                    or item.get("application_category")
                    or ""
                )
                yield (
                    site,
                    str(item["infohash"]).casefold(),
                    item.get("sha256"),
                    source_url,
                    item.get("download_url"),
                    _relative_metainfo(str(item["local_path"]), host_root),
                    str(item.get("display_name") or title),
                    title,
                    category,
                    item.get("uploader"),
                    int(item.get("total_size") or 0),
                    int(item.get("file_count") or 0),
                    item.get("metainfo_size"),
                    item.get("piece_length"),
                    item.get("torrent_version"),
                    item.get("seeders"),
                    item.get("leechers"),
                    item.get("peer_count"),
                    item.get("downloaded_at"),
                    Jsonb(item),
                )

        sql = """
            INSERT INTO catalog.torrents(
              site,infohash,sha256,source_url,download_url,metainfo_relpath,
              display_name,title,category,uploader,total_size,file_count,
              metainfo_size,piece_length,torrent_version,seeders,leechers,
              peer_count,downloaded_at,source_record)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(site,infohash) DO UPDATE SET
              sha256=excluded.sha256,source_url=excluded.source_url,
              download_url=excluded.download_url,metainfo_relpath=excluded.metainfo_relpath,
              display_name=excluded.display_name,title=excluded.title,category=excluded.category,
              uploader=excluded.uploader,total_size=excluded.total_size,
              file_count=excluded.file_count,metainfo_size=excluded.metainfo_size,
              piece_length=excluded.piece_length,torrent_version=excluded.torrent_version,
              seeders=excluded.seeders,leechers=excluded.leechers,
              peer_count=excluded.peer_count,downloaded_at=excluded.downloaded_at,
              source_record=excluded.source_record,active=TRUE,updated_at=now()
        """
        with connection(self.settings) as database:
            _execute_batches(database, sql, _batches(values()))
            database.commit()
        return count

    def _upsert_files(
        self,
        site: str,
        rows: Iterable[dict[str, Any]],
        *,
        ordered_by_infohash: bool = False,
    ) -> int:
        canonical_rows: Iterable[dict[str, Any]] = (
            _canonical_file_stream(rows)
            if ordered_by_infohash
            else _canonical_file_rows(rows)
        )
        with connection(self.settings) as database:
            mapping = {
                str(row["infohash"]): int(row["id"])
                for row in database.execute(
                    "SELECT id,trim(infohash) AS infohash FROM catalog.torrents WHERE site=%s",
                    (site,),
                )
            }

            count = 0

            def values() -> Iterator[tuple[Any, ...]]:
                nonlocal count
                for item in canonical_rows:
                    count += 1
                    torrent_id = mapping.get(item["infohash"])
                    if torrent_id is None:
                        continue
                    yield (
                        torrent_id,
                        item["file_index"],
                        item["path"],
                        item["extension"],
                        item["file_kind"],
                        item["mime_type"],
                        item["size"],
                        item["is_video"],
                        item["is_subtitle"],
                        item["sha256"],
                    )

            sql = """
                INSERT INTO catalog.torrent_files(
                  torrent_id,file_index,path,extension,file_kind,mime_type,size,
                  is_video,is_subtitle,sha256)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(torrent_id,path) DO UPDATE SET
                  file_index=excluded.file_index,extension=excluded.extension,
                  file_kind=excluded.file_kind,mime_type=excluded.mime_type,
                  size=excluded.size,is_video=excluded.is_video,
                  is_subtitle=excluded.is_subtitle,
                  sha256=COALESCE(excluded.sha256,catalog.torrent_files.sha256),
                  updated_at=now()
            """
            _execute_batches(database, sql, _batches(values()))
            database.commit()
        return count

    def _sample_swarm(self) -> None:
        with connection(self.settings) as database:
            database.execute(
                """
                INSERT INTO catalog.swarm_samples(torrent_id,seeders,leechers,peers,source)
                SELECT id,seeders,leechers,peer_count,'1337x-inventory'
                FROM catalog.torrents WHERE site='1337x' AND seeders IS NOT NULL
                """
            )
            database.commit()

    def _sync_metadata(self, path: Path, stat: os.stat_result) -> dict[str, int]:
        run_id = self._start_run("metadata", stat)
        columns = (
            "site", "infohash", "source_title", "category", "media_kind", "query_title",
            "query_year", "query_type", "query_imdb_id", "source", "status", "imdb_id",
            "canonical_title", "release_year", "media_type", "description", "imdb_rating",
            "imdb_votes", "fetched_at", "retry_after", "error",
        )
        sql = """
            INSERT INTO catalog.metadata(site,infohash,source_title,category,media_kind,
              query_title,query_year,query_type,query_imdb_id,source,status,imdb_id,
              canonical_title,release_year,media_type,description,imdb_rating,imdb_votes,
              fetched_at,retry_after,error,source_record)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(site,infohash) DO UPDATE SET
              source_title=excluded.source_title,category=excluded.category,
              media_kind=excluded.media_kind,query_title=excluded.query_title,
              query_year=excluded.query_year,query_type=excluded.query_type,
              query_imdb_id=excluded.query_imdb_id,source=excluded.source,status=excluded.status,
              imdb_id=excluded.imdb_id,canonical_title=excluded.canonical_title,
              release_year=excluded.release_year,media_type=excluded.media_type,
              description=excluded.description,imdb_rating=excluded.imdb_rating,
              imdb_votes=excluded.imdb_votes,fetched_at=excluded.fetched_at,
              retry_after=excluded.retry_after,error=excluded.error,
              source_record=excluded.source_record,updated_at=now()
        """
        count = 0
        source = self._open(path)
        try:
            rows = source.execute("SELECT * FROM catalog_metadata")

            def metadata_values() -> Iterator[tuple[Any, ...]]:
                nonlocal count
                for row in rows:
                    item = _row(row)
                    count += 1
                    yield tuple(item.get(name) for name in columns) + (Jsonb(item),)

            with connection(self.settings) as database:
                _execute_batches(database, sql, _batches(metadata_values()))
                database.commit()
        finally:
            source.close()
        return self._finish_run(run_id, "metadata", stat, {"metadata": count})

    def _sync_subtitles(self, path: Path, stat: os.stat_result) -> dict[str, int]:
        run_id = self._start_run("subtitles", stat)
        columns = (
            "site", "infohash", "torrent_path", "language", "file_name", "normalized_name",
            "extension", "size", "season", "episode", "media_path", "match_method",
            "match_confidence", "status", "provider", "subtitle_path", "synced_path",
            "attempts", "active", "updated_at",
        )
        sql = """
            INSERT INTO catalog.subtitles(site,infohash,torrent_path,language,file_name,
              normalized_name,extension,size,season,episode,media_path,match_method,
              match_confidence,status,provider,subtitle_path,synced_path,attempts,active,
              source_updated_at,source_record)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(site,infohash,torrent_path,language) DO UPDATE SET
              file_name=excluded.file_name,normalized_name=excluded.normalized_name,
              extension=excluded.extension,size=excluded.size,season=excluded.season,
              episode=excluded.episode,media_path=excluded.media_path,
              match_method=excluded.match_method,match_confidence=excluded.match_confidence,
              status=excluded.status,provider=excluded.provider,
              subtitle_path=excluded.subtitle_path,synced_path=excluded.synced_path,
              attempts=excluded.attempts,active=excluded.active,
              source_updated_at=excluded.source_updated_at,
              source_record=excluded.source_record,updated_at=now()
        """
        count = 0
        source = self._open(path)
        try:
            rows = source.execute("SELECT * FROM subtitle_jobs")

            def subtitle_values() -> Iterator[tuple[Any, ...]]:
                nonlocal count
                for row in rows:
                    item = _row(row)
                    count += 1
                    values = tuple(
                        bool(item.get(name)) if name == "active" else item.get(name)
                        for name in columns
                    )
                    yield values + (Jsonb(item),)

            with connection(self.settings) as database:
                _execute_batches(database, sql, _batches(subtitle_values()))
                database.commit()
        finally:
            source.close()
        return self._finish_run(run_id, "subtitles", stat, {"subtitles": count})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    settings.validate_secrets()
    synchronizer = CatalogSynchronizer(settings)
    while True:
        try:
            result = synchronizer.run_once()
            LOG.info("sincronizacao concluida: %s", result)
        except Exception as exc:
            LOG.exception("sincronizacao falhou")
            try:
                beat("sync", "degraded", {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass
            if not args.watch:
                raise
        if not args.watch:
            return
        time.sleep(settings.sync_interval)


if __name__ == "__main__":
    main()
