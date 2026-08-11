from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

import ofc_media.catalog_sync as catalog_sync
from ofc_media.auth import token_digest, token_matches
from ofc_media.catalog_sync import (
    CatalogSynchronizer,
    _canonical_file_rows,
    _cleanup_stale_snapshots,
    _relative_metainfo,
    _snapshot,
    _stable_file_snapshot,
)
from ofc_media.control import STREAM_RE
from ofc_media.torrent_service import _parse_range


def test_windows_inventory_path_becomes_safe_container_relative_path():
    result = _relative_metainfo(
        r"D:\dev\Torrents\1337xVault\downloads\Movies\release.torrent",
        "D:/dev/Torrents/1337xVault/downloads",
    )
    assert result == "Movies/release.torrent"


def test_torrent_files_receive_stable_indices_and_canonical_classification():
    rows = [
        {"infohash": "a" * 40, "path": "Season/Zeta.MKV", "size": 300},
        {"infohash": "b" * 40, "path": "Album/song.FLAC", "size": 400},
        {"infohash": "a" * 40, "path": "Season/a.pt-BR.SRT", "size": 10},
        {
            "infohash": "a" * 40,
            "path": "Season/data.csv",
            "size": 20,
            "sha256": "C" * 64,
        },
    ]

    canonical = _canonical_file_rows(rows)
    first_torrent = [item for item in canonical if item["infohash"] == "a" * 40]
    assert [(item["path"], item["file_index"]) for item in first_torrent] == [
        ("Season/a.pt-BR.SRT", 0),
        ("Season/data.csv", 1),
        ("Season/Zeta.MKV", 2),
    ]
    assert first_torrent[0]["file_kind"] == "subtitle"
    assert first_torrent[0]["is_subtitle"] is True
    assert first_torrent[0]["mime_type"] == "application/x-subrip"
    assert first_torrent[1]["file_kind"] == "dataset"
    assert first_torrent[1]["sha256"] == "c" * 64
    assert first_torrent[2]["file_kind"] == "video"
    assert first_torrent[2]["is_video"] is True
    assert canonical[-1]["file_index"] == 0
    assert canonical[-1]["sha256"] is None


def test_duplicate_torrent_path_is_rejected_before_database_write():
    row = {"infohash": "a" * 40, "path": "same/file.mkv", "size": 10}
    with pytest.raises(ValueError, match="duplicado"):
        _canonical_file_rows([row, dict(row)])


def test_file_upsert_uses_canonical_columns_and_preserves_known_hash(monkeypatch):
    class FakeDatabase:
        committed = False

        def execute(self, sql: str, params: tuple[str, ...]):
            assert "trim(infohash)" in sql
            assert params == ("1337x",)
            return [{"id": 7, "infohash": "a" * 40}]

        def commit(self) -> None:
            self.committed = True

    database = FakeDatabase()

    @contextmanager
    def fake_connection(_settings: Any) -> Iterator[FakeDatabase]:
        yield database

    captured: dict[str, Any] = {}

    def fake_execute_batches(_database: Any, sql: str, batches: Any) -> None:
        captured["sql"] = sql
        captured["values"] = [value for batch in batches for value in batch]

    monkeypatch.setattr(catalog_sync, "connection", fake_connection)
    monkeypatch.setattr(catalog_sync, "_execute_batches", fake_execute_batches)
    synchronizer = CatalogSynchronizer(SimpleNamespace())
    synchronizer._upsert_files(
        "1337x",
        [{"infohash": "a" * 40, "path": "movie.MP4", "size": 99}],
    )

    assert database.committed is True
    assert captured["values"] == [
        (7, 0, "movie.MP4", ".mp4", "video", "video/mp4", 99, True, False, None)
    ]
    assert "file_index=excluded.file_index" in captured["sql"]
    assert "file_kind=excluded.file_kind" in captured["sql"]
    assert "is_subtitle=excluded.is_subtitle" in captured["sql"]
    assert "sha256=COALESCE(excluded.sha256,catalog.torrent_files.sha256)" in captured["sql"]


def test_online_snapshot_is_consistent(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
        database.execute("INSERT INTO items(name) VALUES('video')")
    target, source_stat = _snapshot(source, tmp_path / "snapshots", "test")
    with sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT name FROM items").fetchone()[0] == "video"
    assert source_stat.st_size > 0


def test_stable_file_snapshot_validates_copy_and_refuses_sidecar(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE items(value TEXT)")
        database.execute("INSERT INTO items VALUES('ok')")
    copied = tmp_path / "copied.sqlite3"
    _stable_file_snapshot(source, copied)
    with sqlite3.connect(copied) as database:
        assert database.execute("SELECT value FROM items").fetchone() == ("ok",)

    Path(f"{source}-wal").write_bytes(b"active")
    with pytest.raises(sqlite3.OperationalError, match="sidecar"):
        _stable_file_snapshot(source, tmp_path / "refused.sqlite3")


def test_only_stale_owned_snapshot_temporary_files_are_cleaned(tmp_path: Path):
    stale = tmp_path / f".filecr.{'a' * 32}.tmp"
    unrelated = tmp_path / "keep.tmp"
    stale.write_bytes(b"stale")
    unrelated.write_bytes(b"keep")
    assert _cleanup_stale_snapshots(tmp_path, minimum_age_seconds=0) == 1
    assert not stale.exists()
    assert unrelated.read_bytes() == b"keep"


def test_capability_token_hash_and_stream_route():
    token = "A" * 43
    pepper = "B" * 64
    digest = token_digest(token, pepper)
    assert token_matches(token, pepper, digest)
    assert not token_matches(token + "x", pepper, digest)
    uri = f"/stream/{'a' * 32}/{token}/{'b' * 64}/master.m3u8"
    assert STREAM_RE.match(uri)


def test_http_range_parser():
    assert _parse_range("bytes=10-19", 100) == (10, 19, True)
    assert _parse_range("bytes=-10", 100) == (90, 99, True)
    assert _parse_range(None, 100) == (0, 99, False)
    with pytest.raises(ValueError):
        _parse_range("bytes=100-120", 100)
