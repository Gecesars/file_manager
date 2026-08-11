from __future__ import annotations

import hashlib
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ofc_media import torrent_service
from ofc_media.safety import UnsafeMediaError, encode_bencode
from ofc_media.torrent_service import (
    Materialization,
    MaterializedFile,
    StreamSession,
    TorrentCapacityError,
    TorrentEngine,
)


class FakeFiles:
    def __init__(self, entries: list[tuple[str, int]]) -> None:
        self.entries = entries
        self.offsets: list[int] = []
        offset = 0
        for _path, size in entries:
            self.offsets.append(offset)
            offset += size

    def num_files(self) -> int:
        return len(self.entries)

    def file_path(self, index: int) -> str:
        return self.entries[index][0]

    def file_size(self, index: int) -> int:
        return self.entries[index][1]

    def file_offset(self, index: int) -> int:
        return self.offsets[index]


class FakeTorrentInfo:
    def __init__(self, entries: list[tuple[str, int]]) -> None:
        self._files = FakeFiles(entries)

    def files(self) -> FakeFiles:
        return self._files

    def piece_length(self) -> int:
        return 16 * 1024

    def total_size(self) -> int:
        return sum(size for _path, size in self._files.entries)


class FakeHandle:
    def __init__(self, file_count: int) -> None:
        self.priorities = [0] * file_count
        self.progress = [0] * file_count

    def prioritize_files(self, priorities: list[int]) -> None:
        self.priorities = list(priorities)

    def file_progress(self) -> list[int]:
        return list(self.progress)

    def save_resume_data(self, _flags: object) -> None:
        return None


class FakeLibtorrentSession:
    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle
        self.added: list[object] = []
        self.removed: list[FakeHandle] = []

    def add_torrent(self, parameters: object) -> FakeHandle:
        self.added.append(parameters)
        return self.handle

    def remove_torrent(self, handle: FakeHandle) -> None:
        self.removed.append(handle)


class FakeLibtorrent:
    storage_mode_t = SimpleNamespace(storage_mode_sparse="sparse")
    save_resume_flags_t = SimpleNamespace(flush_disk_cache=1)

    def __init__(self, torrent_info: FakeTorrentInfo) -> None:
        self.info = torrent_info
        self.resume_payloads: list[bytes] = []

    def torrent_info(self, _path: str) -> FakeTorrentInfo:
        return self.info

    def read_resume_data(self, payload: bytes) -> SimpleNamespace:
        self.resume_payloads.append(payload)
        return SimpleNamespace()


def make_metainfo(
    root: Path, files: list[tuple[str, int]]
) -> tuple[Path, str, FakeTorrentInfo]:
    info = {
        b"files": [
            {
                b"length": size,
                b"path": [part.encode("utf-8") for part in path.split("/")],
            }
            for path, size in files
        ],
        b"name": b"bundle",
        b"piece length": 16 * 1024,
        b"pieces": b"\x00" * 20,
    }
    payload = encode_bencode({b"announce": b"https://tracker.invalid", b"info": info})
    infohash = hashlib.sha1(encode_bencode(info)).hexdigest()
    target = root / "bundle.torrent"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    torrent_info = FakeTorrentInfo(
        [(f"bundle/{path}", size) for path, size in files]
    )
    return target, infohash, torrent_info


def make_engine(tmp_path: Path, torrent_info: FakeTorrentInfo) -> TorrentEngine:
    engine = object.__new__(TorrentEngine)
    engine.settings = SimpleNamespace(
        filecr_torrent_root=tmp_path / "torrents",
        x1337_torrent_root=tmp_path / "1337x",
        media_root=tmp_path / "media",
        resume_root=tmp_path / "resume",
    )
    engine.settings.media_root.mkdir()
    engine.settings.resume_root.mkdir()
    engine.lt = FakeLibtorrent(torrent_info)
    engine.handle = FakeHandle(torrent_info.files().num_files())
    engine.engine = FakeLibtorrentSession(engine.handle)
    engine.downloads = {}
    engine.sessions = {}
    engine.materializations = {}
    engine.lock = threading.RLock()
    engine.stop = threading.Event()
    engine._save_resume = lambda _shared: None
    return engine


def install_job(
    engine: TorrentEngine,
    *,
    job_id: str,
    infohash: str,
    rows: list[dict[str, object]],
    updates: list[dict[str, object]],
    target: str = "gdrive",
    state: str = "queued",
    destination_path: str = "video/Filmes/Teste",
) -> None:
    job = {
        "id": job_id,
        "source_site": "filecr",
        "infohash": infohash,
        "target": target,
        "state": state,
        "destination_path": destination_path,
        "selected_file_ids": [row["id"] for row in rows],
        "metainfo_relpath": "bundle.torrent",
    }
    engine._lookup_transfer_job = lambda _job_id: (job, rows)

    def write(_job_id: str, **values: object) -> None:
        updates.append(dict(values))

    engine._write_transfer_progress = write
    engine._write_transfer_error = lambda _job_id, error: updates.append(
        {"state": "failed", "error": str(error)}
    )
    engine._read_transfer_status = lambda _job_id: {
        "id": job_id,
        "source_site": "filecr",
        "infohash": infohash,
        "target": target,
        "selected_file_ids": job["selected_file_ids"],
        "error": None,
        **updates[-1],
    }


def test_materialization_accepts_any_file_type_and_selects_only_requested_indices(
    tmp_path: Path,
):
    torrent_root = tmp_path / "torrents"
    files = [("setup.exe", 3), ("notes.txt", 5), ("archive.bin", 4)]
    _path, infohash, torrent_info = make_metainfo(torrent_root, files)
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[
            {"id": 101, "path": "setup.exe", "size": 3},
            {"id": 103, "path": "archive.bin", "size": 4},
        ],
        updates=updates,
    )

    item = engine.materialize(job_id)

    assert item is not None
    assert {value.catalog_file_id for value in item.files} == {101, 103}
    assert engine.handle.priorities == [7, 0, 7]
    assert updates[0]["state"] == "validating"
    assert updates[-1]["state"] == "downloading"
    assert updates[-1]["bytes_total"] == 7
    assert all("local_path" in value for value in updates[-1]["local_files"])


def test_materialization_rejects_inventory_size_mismatch(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("payload.dat", 10)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 5, "path": "payload.dat", "size": 11}],
        updates=updates,
    )

    with pytest.raises(UnsafeMediaError, match="diverge"):
        engine.materialize(job_id)

    assert not engine.downloads
    assert not engine.engine.added


def test_completed_materialization_keeps_a_shared_stream_alive(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    files = [("payload.bin", 3), ("movie.mkv", 5)]
    _path, infohash, torrent_info = make_metainfo(torrent_root, files)
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 7, "path": "payload.bin", "size": 3}],
        updates=updates,
    )
    item = engine.materialize(job_id)
    assert item is not None
    shared = engine.downloads[item.download_key]
    session_id = "a" * 32
    stream_path = shared.save_root / "bundle" / "movie.mkv"
    engine.sessions[session_id] = StreamSession(
        id=session_id,
        download_key=item.download_key,
        file_index=1,
        file_size=5,
        file_offset=3,
        relative_path="bundle/movie.mkv",
        file_path=stream_path,
    )
    shared.sessions.add(session_id)
    shared.selected_indices.add(1)
    engine._apply_file_priorities(shared)
    materialized = item.files[0].file_path
    materialized.parent.mkdir(parents=True, exist_ok=True)
    materialized.write_bytes(b"abc")
    engine.handle.progress[0] = 3

    status = engine._refresh_materialization(job_id)

    assert status["state"] == "downloaded"
    assert status["bytes_done"] == 3
    assert job_id not in engine.materializations
    assert item.download_key in engine.downloads
    assert engine.handle.priorities == [0, 7]
    assert engine.engine.removed == []


def test_completed_local_materialization_reaches_terminal_state(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(torrent_root, [("payload.bin", 3)])
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 7, "path": "payload.bin", "size": 3}],
        updates=updates,
        target="local",
    )
    item = engine.materialize(job_id)
    assert item is not None
    materialized = item.files[0].file_path
    materialized.parent.mkdir(parents=True, exist_ok=True)
    materialized.write_bytes(b"abc")
    engine.handle.progress[0] = 3

    status = engine._refresh_materialization(job_id)

    assert status["state"] == "completed"
    states = [entry["state"] for entry in updates]
    transitions = [
        value
        for index, value in enumerate(states)
        if index == 0 or value != states[index - 1]
    ]
    assert transitions[-4:] == [
        "downloaded",
        "classifying",
        "verifying",
        "completed",
    ]
    manifest = updates[-1]["local_files"][0]
    classified = (
        engine.settings.media_root
        / "video"
        / "Filmes"
        / "Teste"
        / "payload.bin"
    )
    assert Path(manifest["local_path"]) == classified
    assert manifest["relative_path"] == "payload.bin"
    assert classified.read_bytes() == b"abc"
    assert materialized.read_bytes() == b"abc"


def test_local_publication_preserves_nested_tree_and_avoids_name_collision(
    tmp_path: Path,
):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("Season 01/Episode.mkv", 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 77, "path": "Season 01/Episode.mkv", "size": 3}],
        updates=updates,
        target="local",
        destination_path="video/Series/Minha Serie",
    )
    collision = (
        engine.settings.media_root
        / "video"
        / "Series"
        / "Minha Serie"
        / "Season 01"
        / "Episode.mkv"
    )
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"old")
    item = engine.materialize(job_id)
    assert item is not None
    item.files[0].file_path.parent.mkdir(parents=True, exist_ok=True)
    item.files[0].file_path.write_bytes(b"new")
    engine.handle.progress[0] = 3

    status = engine._refresh_materialization(job_id)

    published = (
        collision.parent / "Episode~77.mkv"
    )
    assert status["state"] == "completed"
    assert collision.read_bytes() == b"old"
    assert published.read_bytes() == b"new"
    assert updates[-1]["local_files"][0]["relative_path"] == (
        "Season 01/Episode~77.mkv"
    )


def test_local_publication_strips_only_classified_root_overlap(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    relative = "video/Series/Dexter/Season 01/Episode.mkv"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [(relative, 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 81, "path": relative, "size": 3}],
        updates=updates,
        target="local",
        destination_path="video/Series/Dexter",
    )
    item = engine.materialize(job_id)
    assert item is not None
    item.files[0].file_path.parent.mkdir(parents=True, exist_ok=True)
    item.files[0].file_path.write_bytes(b"new")
    engine.handle.progress[0] = 3

    assert engine._refresh_materialization(job_id)["state"] == "completed"
    published = (
        engine.settings.media_root
        / "video"
        / "Series"
        / "Dexter"
        / "Season 01"
        / "Episode.mkv"
    )
    assert published.read_bytes() == b"new"
    assert not (
        published.parent.parent / "video" / "Series" / "Dexter"
    ).exists()


def test_local_publication_rejects_symlinked_torrent_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("payload.bin", 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 82, "path": "payload.bin", "size": 3}],
        updates=updates,
        target="local",
    )
    item = engine.materialize(job_id)
    assert item is not None
    source = item.files[0].file_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"bad")
    original_check = engine._is_link_or_junction
    monkeypatch.setattr(
        engine,
        "_is_link_or_junction",
        lambda path: path.absolute() == source.absolute() or original_check(path),
    )
    engine.handle.progress[0] = 3

    with pytest.raises(torrent_service.UnsafeMediaError):
        engine._refresh_materialization(job_id)
    assert not (
        engine.settings.media_root / "video" / "Filmes" / "Teste" / "payload.bin"
    ).exists()


def test_cancel_winning_classification_claim_does_not_publish(tmp_path: Path):
    engine = make_engine(tmp_path, FakeTorrentInfo([("payload.bin", 3)]))
    infohash = "b" * 40
    source = engine.settings.media_root / f"filecr-{infohash}" / "payload.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"abc")
    item = Materialization(
        id=str(uuid.uuid4()),
        download_key=f"filecr-{infohash}",
        target="local",
        files=(MaterializedFile(83, 0, "payload.bin", 3, source),),
        bytes_total=3,
        destination_path="video/Filmes/Cancelado",
    )
    writes = 0

    def conditional_write(_job_id: str, **_values: object) -> bool:
        nonlocal writes
        writes += 1
        return writes == 1

    engine._write_transfer_progress = conditional_write
    engine._read_transfer_status = lambda _job_id: {"state": "cancelled"}

    state = engine._persist_materialization_snapshot(
        item,
        state="downloaded",
        bytes_done=3,
        local_files=[{**item.files[0].as_dict(), "complete": True}],
    )

    assert state == "cancelled"
    assert not (
        engine.settings.media_root
        / "video"
        / "Filmes"
        / "Cancelado"
        / "payload.bin"
    ).exists()


@pytest.mark.parametrize("interrupted_state", ["queued", "validating", "downloading"])
def test_recovery_rematerializes_interrupted_job_with_fastresume_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupted_state: str
):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("payload.bin", 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 7, "path": "payload.bin", "size": 3}],
        updates=updates,
        state=interrupted_state,
    )
    resume_payload = b"fake-fastresume"
    (engine.settings.resume_root / f"filecr-{infohash}.fastresume").write_bytes(
        resume_payload
    )
    recovery_sql: list[tuple[str, object]] = []

    class RecoveryResult:
        def fetchall(self) -> list[dict[str, str]]:
            return [{"id": job_id}]

    class RecoveryDatabase:
        def execute(self, sql: str, params: object = None) -> RecoveryResult:
            recovery_sql.append((sql, params))
            return RecoveryResult()

    @contextmanager
    def recovery_connection(_settings: object):
        yield RecoveryDatabase()

    monkeypatch.setattr(torrent_service, "connection", recovery_connection)

    assert engine._recover_materializations_once() == 1
    assert engine._recover_materializations_once() == 0

    assert engine.lt.resume_payloads == [resume_payload]
    assert len(engine.engine.added) == 1
    assert job_id in engine.materializations
    assert updates[-1]["state"] == "downloading"
    assert "('queued','validating','downloading')" in recovery_sql[0][0]
    assert "LIMIT %s" in recovery_sql[0][0]
    assert recovery_sql[0][1] == ([], 2)
    assert "LIMIT %s" in recovery_sql[1][0]
    assert recovery_sql[1][1] == ([job_id], 32)


def test_recovery_loop_survives_transient_scan_failure(tmp_path: Path):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/file.bin", 1)]))
    attempts = 0

    def recover() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("postgres temporariamente indisponivel")
        engine.stop.set()
        return 0

    engine._recover_materializations_once = recover
    engine._recovery_loop(0)

    assert attempts == 2


def test_capacity_rejection_leaves_queued_materialization_untouched(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("payload.bin", 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    engine.settings.max_active_torrents = 1
    engine.materializations["existing"] = Materialization(
        id="existing",
        download_key="filecr-" + "f" * 40,
        target="local",
        files=(),
        bytes_total=0,
    )
    job_id = str(uuid.uuid4())
    updates: list[dict[str, object]] = []
    install_job(
        engine,
        job_id=job_id,
        infohash=infohash,
        rows=[{"id": 7, "path": "payload.bin", "size": 3}],
        updates=updates,
    )

    with pytest.raises(TorrentCapacityError, match="capacidade"):
        engine.materialize(job_id)

    assert updates == []
    assert job_id not in engine.materializations
    assert getattr(engine, "materialization_admissions", {}) == {}


def test_recovery_requests_only_free_slots_and_excludes_active_jobs(tmp_path: Path):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/file.bin", 1)]))
    engine.settings.max_active_torrents = 3
    active_id = str(uuid.uuid4())
    engine.materializations[active_id] = Materialization(
        id=active_id,
        download_key="filecr-" + "f" * 40,
        target="local",
        files=(),
        bytes_total=0,
    )
    scans: list[tuple[str, int, tuple[str, ...]]] = []

    def materialization_scan(limit: int, excluded: tuple[str, ...]) -> list[str]:
        scans.append(("materialize", limit, excluded))
        return []

    def completion_scan(limit: int, excluded: tuple[str, ...]) -> list[str]:
        scans.append(("complete", limit, excluded))
        return []

    engine._recoverable_materialization_ids = materialization_scan
    engine._recoverable_local_completion_ids = completion_scan

    assert engine._recover_materializations_once() == 0
    assert scans == [
        ("materialize", 2, (active_id,)),
        ("complete", 32, (active_id,)),
    ]


def test_conditional_progress_update_uses_row_state_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/file.bin", 1)]))
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def fetchone(self) -> None:
            return None

    class Database:
        def execute(self, sql: str, params: tuple[object, ...]) -> Result:
            calls.append((sql, params))
            return Result()

        def commit(self) -> None:
            return None

    @contextmanager
    def fake_connection(_settings: object):
        yield Database()

    monkeypatch.setattr(torrent_service, "connection", fake_connection)
    updated = engine._write_transfer_progress(
        str(uuid.uuid4()),
        state="downloading",
        bytes_total=10,
        bytes_done=3,
        local_files=[],
        expected_states=("validating", "downloading"),
    )
    cancelled = engine._cancel_transfer_job(str(uuid.uuid4()))

    assert updated is False
    assert cancelled is False
    assert "state=ANY(%s::text[])" in calls[0][0]
    assert "RETURNING state" in calls[0][0]
    assert calls[0][1][-1] == ["validating", "downloading"]
    assert "bytes_done" not in calls[1][0]
    assert "state=ANY(%s::text[])" in calls[1][0]


def test_cancel_during_validation_cannot_resurrect_materialization(tmp_path: Path):
    torrent_root = tmp_path / "torrents"
    _path, infohash, torrent_info = make_metainfo(
        torrent_root, [("payload.bin", 3)]
    )
    engine = make_engine(tmp_path, torrent_info)
    job_id = str(uuid.uuid4())
    rows = [{"id": 7, "path": "payload.bin", "size": 3}]
    job = {
        "id": job_id,
        "source_site": "filecr",
        "infohash": infohash,
        "target": "gdrive",
        "state": "queued",
        "selected_file_ids": [7],
        "metainfo_relpath": "bundle.torrent",
    }
    persisted: dict[str, object] = {
        **job,
        "bytes_total": 0,
        "bytes_done": 0,
        "local_files": [],
        "error": None,
    }
    state_lock = threading.Lock()
    validated = threading.Event()
    resume_validation = threading.Event()
    engine._lookup_transfer_job = lambda _job_id: (job, rows)

    def read_status(_job_id: str) -> dict[str, object]:
        with state_lock:
            return dict(persisted)

    def conditional_write(_job_id: str, **values: object) -> bool:
        expected = tuple(values.pop("expected_states", ()))
        with state_lock:
            if expected and persisted["state"] not in expected:
                return False
            persisted.update(values)
            return True

    def conditional_cancel(_job_id: str) -> bool:
        with state_lock:
            if persisted["state"] not in torrent_service.CANCELLABLE_TRANSFER_STATES:
                return False
            persisted["state"] = "cancelled"
            persisted["error"] = None
            return True

    engine._read_transfer_status = read_status
    engine._write_transfer_progress = conditional_write
    engine._cancel_transfer_job = conditional_cancel
    engine._write_transfer_error = lambda *_args, **_kwargs: True
    original_validation = engine._validated_materialization

    def blocking_validation(*args: object):
        result = original_validation(*args)
        validated.set()
        assert resume_validation.wait(2)
        return result

    engine._validated_materialization = blocking_validation
    results: list[Materialization | None] = []
    failures: list[BaseException] = []

    def run_materialize() -> None:
        try:
            results.append(engine.materialize(job_id))
        except BaseException as exc:  # pragma: no cover - torna a falha legivel
            failures.append(exc)

    worker = threading.Thread(target=run_materialize)
    worker.start()
    assert validated.wait(2)

    cancelled = engine.cancel_materialization(job_id)
    resume_validation.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert results and results[0] is not None
    assert cancelled["state"] == "cancelled"
    assert persisted["state"] == "cancelled"
    assert job_id not in engine.materializations
    assert engine.downloads == {}
    assert engine.engine.removed == [engine.handle]
    assert getattr(engine, "materialization_admissions", {}) == {}


def test_recovery_stops_at_capacity_and_keeps_remaining_jobs_queued(tmp_path: Path):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/file.bin", 1)]))
    calls: list[str] = []
    engine._recoverable_materialization_ids = lambda *_args: ["first", "second"]
    engine._recoverable_local_completion_ids = lambda *_args: []

    def at_capacity(job_id: str) -> None:
        calls.append(job_id)
        raise TorrentCapacityError("cheio")

    engine.materialize = at_capacity

    assert engine._recover_materializations_once() == 0
    assert calls == ["first"]


@pytest.mark.parametrize(
    ("persisted_state", "expected_transitions"),
    [
        ("downloaded", ["classifying", "classifying", "verifying", "completed"]),
        ("classifying", ["classifying", "verifying", "completed"]),
        ("verifying", ["verifying", "completed"]),
    ],
)
def test_recovery_finishes_validated_local_manifest_from_each_intermediate_state(
    tmp_path: Path,
    persisted_state: str,
    expected_transitions: list[str],
):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/payload.bin", 3)]))
    job_id = str(uuid.uuid4())
    infohash = "a" * 40
    local_path = (
        engine.settings.media_root
        / f"filecr-{infohash}"
        / "bundle"
        / "payload.bin"
    )
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"abc")
    manifest = [
        {
            "file_id": 7,
            "file_index": 0,
            "path": "payload.bin",
            "local_path": str(local_path),
            "size": 3,
            "bytes_done": 3,
            "complete": True,
        }
    ]
    status = {
        "id": job_id,
        "source_site": "filecr",
        "infohash": infohash,
        "target": "local",
        "state": persisted_state,
        "destination_path": "video/Series/Teste",
        "selected_file_ids": [7],
        "bytes_total": 3,
        "bytes_done": 3,
        "local_files": manifest,
    }
    updates: list[dict[str, object]] = []
    errors: list[str] = []
    engine._recoverable_materialization_ids = lambda *_args: []
    engine._recoverable_local_completion_ids = lambda *_args: [job_id]
    engine._read_transfer_status = lambda _job_id: status
    engine._write_transfer_progress = (
        lambda _job_id, **values: updates.append(dict(values))
    )
    engine._write_transfer_error = (
        lambda _job_id, error: errors.append(str(error))
    )

    assert engine._recover_materializations_once() == 1

    assert [entry["state"] for entry in updates] == expected_transitions
    assert all(entry["local_files"] for entry in updates)
    assert all(entry["bytes_done"] == entry["bytes_total"] == 3 for entry in updates)
    assert errors == []
    assert (
        engine.settings.media_root / "video" / "Series" / "Teste" / "payload.bin"
    ).read_bytes() == b"abc"


def test_recovery_reuses_published_collision_when_staging_is_gone(tmp_path: Path):
    engine = make_engine(tmp_path, FakeTorrentInfo([("Episode.mkv", 3)]))
    job_id = str(uuid.uuid4())
    infohash = "c" * 40
    destination = engine.settings.media_root / "video" / "Series" / "Dexter"
    published = destination / "Season 01" / "Episode~77.mkv"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"abc")
    missing_staging = (
        engine.settings.media_root / f"filecr-{infohash}" / "Episode.mkv"
    )
    manifest = [
        {
            "file_id": 77,
            "file_index": 0,
            "path": "Season 01/Episode.mkv",
            "relative_path": "Season 01/Episode~77.mkv",
            "source_path": str(missing_staging),
            "local_path": str(published),
            "size": 3,
            "bytes_done": 3,
            "complete": True,
        }
    ]
    status = {
        "id": job_id,
        "source_site": "filecr",
        "infohash": infohash,
        "target": "local",
        "state": "verifying",
        "destination_path": "video/Series/Dexter",
        "selected_file_ids": [77],
        "bytes_total": 3,
        "bytes_done": 3,
        "local_files": manifest,
    }
    updates: list[dict[str, object]] = []
    engine._read_transfer_status = lambda _job_id: status
    engine._write_transfer_progress = (
        lambda _job_id, **values: updates.append(dict(values)) or True
    )

    assert engine._recover_local_completion(job_id) is True
    assert [entry["state"] for entry in updates] == ["verifying", "completed"]
    assert Path(updates[-1]["local_files"][0]["local_path"]) == published
    assert published.read_bytes() == b"abc"


def test_recovery_marks_local_job_failed_when_manifest_escapes_media_root(
    tmp_path: Path,
):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/payload.bin", 3)]))
    job_id = str(uuid.uuid4())
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"abc")
    status = {
        "id": job_id,
        "source_site": "1337x",
        "target": "local",
        "state": "verifying",
        "selected_file_ids": [7],
        "bytes_total": 3,
        "bytes_done": 3,
        "local_files": [
            {
                "file_id": 7,
                "local_path": str(outside),
                "size": 3,
                "complete": True,
            }
        ],
    }
    errors: list[str] = []
    engine._recoverable_materialization_ids = lambda *_args: []
    engine._recoverable_local_completion_ids = lambda *_args: [job_id]
    engine._read_transfer_status = lambda _job_id: status
    engine._write_transfer_progress = lambda *_args, **_kwargs: pytest.fail(
        "manifesto inseguro nao deve avancar"
    )
    engine._write_transfer_error = (
        lambda _job_id, error: errors.append(str(error))
    )

    assert engine._recover_materializations_once() == 0
    assert errors and "fora da raiz" in errors[0]


def test_torrent_playback_lookup_enforces_default_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    engine = make_engine(tmp_path, FakeTorrentInfo([("bundle/file.bin", 1)]))
    calls: list[tuple[str, object]] = []

    class Result:
        def fetchone(self) -> dict[str, object]:
            return {"id": "a" * 32, "site": "filecr"}

    class Database:
        def execute(self, sql: str, params: object = None) -> Result:
            calls.append((sql, params))
            return Result()

    @contextmanager
    def fake_connection(_settings: object):
        yield Database()

    monkeypatch.setattr(torrent_service, "connection", fake_connection)
    assert engine._lookup("a" * 32)["site"] == "filecr"

    sql, params = calls[0]
    assert "created_at >= now()-make_interval(secs => %s)" in sql
    assert params == ("a" * 32, 43_200)


def test_internal_materialization_endpoints_are_authenticated_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
):
    token = "I" * 32
    job_id = str(uuid.uuid4())
    full_job_id = str(uuid.uuid4())

    class FakeEngine:
        sessions: dict[str, object] = {}
        materializations: dict[str, object] = {}

        def __init__(self, _settings: object) -> None:
            self.calls: list[tuple[str, str]] = []

        def materialize(self, value: str) -> None:
            self.calls.append(("post", value))
            if value == full_job_id:
                raise TorrentCapacityError("cheio")

        def materialization_status(self, value: str) -> dict[str, object]:
            self.calls.append(("get", value))
            return {"id": value, "state": "downloading", "bytes_done": 2}

        def cancel_materialization(self, value: str) -> dict[str, object]:
            self.calls.append(("delete", value))
            return {"id": value, "state": "cancelled", "bytes_done": 2}

        def close_all(self) -> None:
            return None

    fake = FakeEngine(None)
    monkeypatch.setattr(
        torrent_service.Settings,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(internal_token=token)),
    )
    monkeypatch.setattr(torrent_service, "TorrentEngine", lambda _settings: fake)
    monkeypatch.setattr(torrent_service, "start_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(torrent_service.atexit, "register", lambda *_args, **_kwargs: None)
    app = torrent_service.create_app()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/internal/materializations", json={"job_id": job_id}).status_code == 403
    created = client.post(
        "/internal/materializations", json={"job_id": job_id}, headers=headers
    )
    fetched = client.get(f"/internal/materializations/{job_id}", headers=headers)
    cancelled = client.delete(f"/internal/materializations/{job_id}", headers=headers)
    rejected = client.post(
        "/internal/materializations", json={"job_id": full_job_id}, headers=headers
    )

    assert created.status_code == 202
    assert fetched.get_json()["state"] == "downloading"
    assert cancelled.get_json()["state"] == "cancelled"
    assert rejected.status_code == 429
    assert rejected.get_json()["retryable"] is True
    assert fake.calls == [
        ("post", job_id),
        ("get", job_id),
        ("get", job_id),
        ("delete", job_id),
        ("post", full_job_id),
    ]
