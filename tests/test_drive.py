import hashlib
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import ofc_media.drive_service as drive_service

from ofc_media.drive_service import (
    DriveChangeFeed,
    DriveCatalog,
    DriveClient,
    DriveScope,
    DriveTransferWorker,
    DriveRuntime,
    InvalidDriveCursor,
    PostgresTransferStore,
    classify_file,
    drive_group,
    parse_range,
    relative_to_destination_group,
    safe_drive_component,
    unique_drive_components,
    unique_relative_paths,
)


def test_drive_names_are_catalogued_as_safe_relative_components():
    assert safe_drive_component("Filme: corte/final.mkv") == "Filme - corte_final.mkv"
    assert safe_drive_component("..") == "sem-nome"


def test_portable_components_use_stable_identity_on_collision_and_truncation():
    values = unique_drive_components(
        [
            (1, "movie?.mkv", "drive-file-one"),
            (2, "movie*.mkv", "drive-file-two"),
        ]
    )
    assert values[1] != values[2]
    assert "--" in values[1] and "--" in values[2]
    assert values == unique_drive_components(
        [
            (2, "movie*.mkv", "drive-file-two"),
            (1, "movie?.mkv", "drive-file-one"),
        ]
    )
    long_name = safe_drive_component(f"{'episodio' * 60}.mkv")
    assert len(long_name.encode("utf-8")) <= 180
    assert "--" in long_name
    paths = unique_relative_paths(
        [
            (1, "Season?/one.mkv", "file-one"),
            (2, "Season*/two.mkv", "file-two"),
            (3, "Season?/three.mkv", "file-three"),
        ]
    )
    assert paths[1].split("/")[0] == paths[3].split("/")[0]
    assert paths[1].split("/")[0] != paths[2].split("/")[0]


def test_remote_group_prefix_is_not_duplicated_under_destination(tmp_path: Path):
    destination = tmp_path / "Series" / "Dexter"
    assert relative_to_destination_group(
        [
            "Series/Dexter/Season 1/episode.mkv",
            "Series/Dexter/Season 1/subtitle.srt",
        ],
        destination,
    ) == ["Season 1/episode.mkv", "Season 1/subtitle.srt"]
    category_destination = tmp_path / "Series"
    assert relative_to_destination_group(
        ["Series/Dexter/episode.mkv"], category_destination
    ) == ["Dexter/episode.mkv"]
    # Coincidencia no meio/fim nao autoriza apagar a arvore original.
    assert relative_to_destination_group(
        ["Show/Season 1/episode.mkv"], tmp_path / "video" / "Season 1"
    ) == ["Show/Season 1/episode.mkv"]


def test_grouping_preserves_category_and_collects_nested_series():
    identity, title, category, infohash = drive_group(
        ("Series", "Dexter", "Temporada 1"),
        ("series-id", "dexter-id", "season-id"),
        "episode-id-123",
        "Dexter.S01E01.mkv",
    )
    assert identity == "folder:dexter-id"
    assert title == "Dexter"
    assert category == "Series"
    assert len(infohash) == 40

    direct = drive_group(
        ("Filmes", "Acao"),
        ("movies-id", "action-id"),
        "movie-id-12345",
        "Filme.mp4",
    )
    assert direct[0] == "file:movie-id-12345"
    assert direct[1:3] == ("Filme", "Filmes/Acao")


def test_batched_scan_catalogues_every_downloadable_blob_but_marks_only_real_videos():
    class FakeClient:
        calls = []

        def list_children(self, parent_ids):
            self.calls.append(tuple(parent_ids))
            values = {
                "root-id-12345": [
                    {"id": "folder-filmes", "name": "Filmes", "mimeType": "application/vnd.google-apps.folder", "parents": ["root-id-12345"]},
                    {"id": "folder-series", "name": "Series", "mimeType": "application/vnd.google-apps.folder", "parents": ["root-id-12345"]},
                    {"id": "unsafe-file-1", "name": "instalador.exe", "mimeType": "application/octet-stream", "size": "100", "parents": ["root-id-12345"], "capabilities": {"canDownload": True}},
                ],
                "folder-filmes": [
                    {"id": "folder-action", "name": "Acao", "mimeType": "application/vnd.google-apps.folder", "parents": ["folder-filmes"]},
                ],
                "folder-series": [
                    {"id": "folder-dexter", "name": "Dexter", "mimeType": "application/vnd.google-apps.folder", "parents": ["folder-series"]},
                ],
                "folder-action": [
                    {"id": "movie-file-123", "name": "Filme.mp4", "mimeType": "video/mp4", "size": "2000", "parents": ["folder-action"], "modifiedTime": "2026-01-01T00:00:00Z", "capabilities": {"canDownload": True}},
                    {"id": "fake-video-123", "name": "Filme.mkv.exe", "mimeType": "application/octet-stream", "size": "2000", "parents": ["folder-action"], "capabilities": {"canDownload": True}},
                ],
                "folder-dexter": [
                    {"id": "episode-file-1", "name": "Dexter.S01E01.mkv", "mimeType": "video/x-matroska", "size": "3000", "parents": ["folder-dexter"], "modifiedTime": "2026-01-02T00:00:00Z", "capabilities": {"canDownload": True}},
                    {"id": "subtitle-file-1", "name": "Dexter.S01E01.pt-BR.srt", "mimeType": "application/x-subrip", "size": "500", "parents": ["folder-dexter"], "modifiedTime": "2026-01-02T00:00:00Z", "capabilities": {"canDownload": True}},
                    {"id": "blocked-file-1", "name": "Dexter.S01E02.mkv", "mimeType": "video/x-matroska", "size": "3000", "parents": ["folder-dexter"], "capabilities": {"canDownload": False}},
                ],
            }
            result = []
            for parent_id in parent_ids:
                result.extend(values.get(parent_id, []))
            return result

    client = FakeClient()
    catalog = DriveCatalog(SimpleNamespace(gdrive_root_id="root-id-12345"), client)
    groups, counts = catalog.scan()
    assert counts == {
        "folders": 4,
        "files": 6,
        "blobs": 5,
        "videos": 2,
        "subtitles": 1,
        "rejected": 1,
    }
    assert {item["category"] for item in groups.values()} == {
        "Google Drive",
        "Filmes/Acao",
        "Series",
    }
    assert sorted(file["path"] for group in groups.values() for file in group["files"]) == sorted([
        "Filmes/Acao/Filme.mp4",
        "Filmes/Acao/Filme.mkv.exe",
        "Series/Dexter/Dexter.S01E01.pt-BR.srt",
        "Series/Dexter/Dexter.S01E01.mkv",
        "instalador.exe",
    ])
    files = [file for group in groups.values() for file in group["files"]]
    assert sum(item["is_video"] for item in files) == 2
    assert [item["file_kind"] for item in files if item["is_subtitle"]] == ["subtitle"]
    assert not next(item for item in files if item["name"].endswith(".exe"))["is_video"]
    assert any(set(call) == {"folder-filmes", "folder-series"} for call in client.calls)


def test_drive_range_parser():
    assert parse_range(None, 100) == (0, 99, False)
    assert parse_range("bytes=10-19", 100) == (10, 19, True)
    assert parse_range("bytes=-10", 100) == (90, 99, True)
    with pytest.raises(ValueError):
        parse_range("bytes=0-200", 100)


class FakeCredentials:
    valid = True
    refresh_token = "fake-refresh"
    token = "fake-access"

    def refresh(self, request):
        self.valid = True
        self.token = "fake-refreshed-access"


class FakeResponse:
    def __init__(self, status=200, payload=None, *, headers=None, chunks=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        yield from self._chunks


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"requisicao inesperada: {method} {url}")
        return self.responses.pop(0)


def drive_client(responses) -> tuple[DriveClient, FakeSession]:
    session = FakeSession(responses)
    return (
        DriveClient(credentials=FakeCredentials(), session=session, sleeper=lambda _: None),
        session,
    )


def test_changes_helpers_paginate_and_include_shared_drive_removals():
    client, session = drive_client(
        [
            FakeResponse(payload={"startPageToken": "start-1"}),
            FakeResponse(
                payload={
                    "changes": [
                        {
                            "fileId": "changed-file-12345",
                            "changeType": "file",
                            "file": {"id": "changed-file-12345"},
                        }
                    ],
                    "nextPageToken": "page-2",
                }
            ),
            FakeResponse(
                payload={
                    "changes": [
                        {
                            "fileId": "removed-file-12345",
                            "changeType": "file",
                            "removed": True,
                        }
                    ],
                    "newStartPageToken": "start-3",
                }
            ),
        ]
    )
    assert client.get_start_page_token() == "start-1"
    feed = client.list_changes("start-1")
    assert len(feed.changes) == 2
    assert feed.new_start_page_token == "start-3"
    assert feed.has_removals is True
    assert session.calls[0]["params"]["supportsAllDrives"] == "true"
    change_calls = session.calls[1:]
    assert [call["params"]["pageToken"] for call in change_calls] == [
        "start-1",
        "page-2",
    ]
    for call in change_calls:
        assert call["params"]["supportsAllDrives"] == "true"
        assert call["params"]["includeItemsFromAllDrives"] == "true"
        assert call["params"]["includeRemoved"] == "true"
        assert call["params"]["includeCorpusRemovals"] == "true"


def test_changes_helper_reports_rejected_page_token_without_retrying():
    client, session = drive_client([FakeResponse(status=410)])
    with pytest.raises(InvalidDriveCursor):
        client.list_changes("stale-page-token")
    assert len(session.calls) == 1


def test_root_scope_discovers_shared_drive_and_scopes_change_requests():
    root_id = "shared-root-folder-12345"
    drive_id = "shared-drive-12345"
    client, session = drive_client(
        [
            FakeResponse(payload={"id": root_id, "driveId": drive_id}),
            FakeResponse(payload={"startPageToken": "shared-start"}),
            FakeResponse(payload={"changes": [], "newStartPageToken": "shared-next"}),
            FakeResponse(payload={"files": []}),
        ]
    )
    scope = client.discover_root_scope(root_id)
    assert scope == DriveScope(drive_id, "drive")
    assert client.get_start_page_token(drive_id=scope.drive_id) == "shared-start"
    assert client.list_changes("shared-start", drive_id=scope.drive_id).changes == ()
    assert client.list_children([root_id], drive_id=scope.drive_id) == []
    assert session.calls[0]["params"]["supportsAllDrives"] == "true"
    assert session.calls[1]["params"]["driveId"] == drive_id
    assert session.calls[2]["params"]["driveId"] == drive_id
    assert session.calls[3]["params"]["driveId"] == drive_id
    assert session.calls[3]["params"]["corpora"] == "drive"


def test_catalog_reuses_persisted_shared_drive_scope_without_rediscovery():
    drive_id = "shared-drive-12345"
    client = FakeSyncClient(
        feed=DriveChangeFeed((), "next-shared-token", has_removals=False)
    )
    catalog, _, scan_calls = sync_catalog(
        {
            "drive_id": drive_id,
            "page_token": "shared-token",
            "pending_page_token": None,
        },
        client,
    )
    counts = catalog.sync_once()
    assert counts["skipped"] == 1 and scan_calls == []
    assert client.discovery_calls == []
    assert client.list_calls == [("shared-token", drive_id)]


class FakeSqlResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeSyncDatabase:
    def __init__(self, cursor=None):
        self.cursor = cursor
        self.calls = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM ops.drive_cursors WHERE cursor_key" in sql:
            return FakeSqlResult(self.cursor)
        return FakeSqlResult()

    def commit(self):
        self.commits += 1


class FakeSyncClient:
    def __init__(
        self,
        *,
        feed=None,
        error=None,
        start_token="fresh-start-token",
        scope=None,
    ):
        self.feed = feed
        self.error = error
        self.start_token = start_token
        self.scope = scope or DriveScope(None, "user")
        self.list_calls = []
        self.start_calls = 0
        self.discovery_calls = []

    def discover_root_scope(self, root_id):
        self.discovery_calls.append(root_id)
        return self.scope

    def list_changes(self, page_token, *, drive_id=None):
        self.list_calls.append((page_token, drive_id))
        if self.error:
            raise self.error
        return self.feed

    def get_start_page_token(self, *, drive_id=None):
        self.start_calls += 1
        self.start_drive_id = drive_id
        return self.start_token


def sync_catalog(cursor, client):
    database = FakeSyncDatabase(cursor)

    @contextmanager
    def factory(settings):
        yield database

    catalog = DriveCatalog(
        SimpleNamespace(gdrive_root_id="root-folder-12345"),
        client,
        connection_factory=factory,
    )
    scan_calls = []

    def scan():
        scan_calls.append(True)
        return {}, {
            "folders": 0,
            "files": 0,
            "blobs": 0,
            "videos": 0,
            "subtitles": 0,
            "rejected": 0,
        }

    catalog.scan = scan
    return catalog, database, scan_calls


def cursor_writes(database):
    return [call for call in database.calls if "INSERT INTO ops.drive_cursors" in call[0]]


def test_first_sync_scans_and_confirms_start_token_only_after_reconciliation():
    client = FakeSyncClient(start_token="initial-start-token")
    catalog, database, scan_calls = sync_catalog(None, client)
    counts = catalog.sync_once()
    assert scan_calls == [True]
    assert client.start_calls == 1 and client.list_calls == []
    assert counts["reconciled"] == 1
    writes = cursor_writes(database)
    assert len(writes) == 2
    assert "pending_page_token" in writes[0][0]
    assert writes[0][1][-1] == "initial-start-token"
    assert "last_success_at" in writes[1][0]
    assert writes[1][1][-1] == "initial-start-token"
    stage_index = database.calls.index(writes[0])
    reconciliation_index = next(
        index
        for index, (sql, _) in enumerate(database.calls)
        if "UPDATE catalog.drive_files SET active=FALSE" in sql
    )
    final_index = database.calls.index(writes[1])
    assert stage_index < reconciliation_index < final_index


def test_unchanged_poll_advances_cursor_without_walking_drive_tree():
    client = FakeSyncClient(
        feed=DriveChangeFeed((), "next-start-token", has_removals=False)
    )
    catalog, database, scan_calls = sync_catalog(
        {"page_token": "committed-token", "pending_page_token": None}, client
    )
    counts = catalog.sync_once()
    assert scan_calls == []
    assert client.list_calls == [("committed-token", None)] and client.start_calls == 0
    assert counts["skipped"] == 1 and counts["reconciled"] == 0
    assert len(cursor_writes(database)) == 1
    assert not any(
        "UPDATE catalog.drive_files SET active=FALSE" in sql for sql, _ in database.calls
    )


def test_any_change_including_access_loss_forces_full_reconciliation():
    feed = DriveChangeFeed(
        ({"fileId": "gone-file-12345", "removed": True},),
        "after-change-token",
        has_removals=True,
    )
    client = FakeSyncClient(feed=feed)
    catalog, database, scan_calls = sync_catalog(
        {"page_token": "before-change-token", "pending_page_token": None}, client
    )
    counts = catalog.sync_once()
    assert scan_calls == [True]
    assert counts["changes"] == 1 and counts["removals"] == 1
    writes = cursor_writes(database)
    assert len(writes) == 2
    assert writes[-1][1][-1] == "after-change-token"


def test_rejected_cursor_is_cleared_and_rebuilt_by_safe_full_scan():
    client = FakeSyncClient(
        error=InvalidDriveCursor("cursor rejeitado"), start_token="replacement-token"
    )
    catalog, database, scan_calls = sync_catalog(
        {"page_token": "bad-token", "pending_page_token": "stale-pending"}, client
    )
    counts = catalog.sync_once()
    assert scan_calls == [True]
    assert counts["cursor_reset"] == 1
    assert client.list_calls == [("bad-token", None)] and client.start_calls == 1
    writes = cursor_writes(database)
    assert len(writes) == 3
    assert "page_token=NULL" in writes[0][0]
    assert writes[-1][1][-1] == "replacement-token"


def test_changed_cursor_is_never_confirmed_when_reconciliation_fails():
    client = FakeSyncClient(
        feed=DriveChangeFeed(
            ({"fileId": "changed-file-12345", "changeType": "file"},),
            "candidate-token",
            has_removals=False,
        )
    )
    catalog, database, _ = sync_catalog(
        {"page_token": "committed-token", "pending_page_token": None}, client
    )

    def failed_scan():
        raise RuntimeError("scan interrompido")

    catalog.scan = failed_scan
    with pytest.raises(RuntimeError, match="scan interrompido"):
        catalog.sync_once()
    writes = cursor_writes(database)
    assert any("pending_page_token" in sql for sql, _ in writes)
    assert not any("last_success_at" in sql for sql, _ in writes)


def test_drive_lookup_rejects_sessions_older_than_configured_ttl(monkeypatch):
    database = FakeSyncDatabase()

    @contextmanager
    def factory(settings):
        yield database

    monkeypatch.setattr(drive_service, "connection", factory)
    runtime = object.__new__(DriveRuntime)
    runtime.settings = SimpleNamespace(playback_ttl_seconds=321)
    with pytest.raises(KeyError):
        runtime.lookup("session-id")
    sql, params = database.calls[0]
    assert "created_at >= now()-make_interval(secs=>%s)" in sql
    assert params == ("session-id", 321)


def test_trigger_sync_returns_immediately_and_is_idempotent_while_running(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingCatalog:
        def sync_once(self):
            started.set()
            assert release.wait(2)
            return {"files": 1}

    runtime = object.__new__(DriveRuntime)
    runtime.catalog = BlockingCatalog()
    runtime.sync_lock = threading.Lock()
    runtime.last_counts = {}
    runtime.last_error = None
    runtime.last_success = None
    runtime.details = lambda: {}
    monkeypatch.setattr(drive_service, "beat", lambda *args, **kwargs: None)
    before = time.monotonic()
    assert runtime.trigger_sync() is True
    assert time.monotonic() - before < 0.5
    assert started.wait(1)
    assert runtime.trigger_sync() is False
    release.set()
    deadline = time.monotonic() + 2
    while runtime.sync_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.sync_lock.locked() is False
    assert runtime.last_counts == {"files": 1}


def test_internal_sync_endpoint_returns_202_then_409_without_waiting(monkeypatch):
    settings = SimpleNamespace(internal_token="t" * 32)

    class FakeSettings:
        @staticmethod
        def from_env():
            return settings

    class FakeRuntime:
        last_error = None

        def __init__(self):
            self.accept = True

        def start_sync_loop(self):
            return None

        def start_transfer_loop(self):
            return None

        def details(self):
            return {}

        def trigger_sync(self):
            accepted, self.accept = self.accept, False
            return accepted

    runtime = FakeRuntime()
    monkeypatch.setattr(drive_service, "Settings", FakeSettings)
    monkeypatch.setattr(drive_service, "DriveRuntime", lambda selected: runtime)
    monkeypatch.setattr(drive_service, "start_heartbeat", lambda *args, **kwargs: None)
    app = drive_service.create_app()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {settings.internal_token}"}
    accepted = client.post("/internal/sync", headers=headers)
    duplicate = client.post("/internal/sync", headers=headers)
    assert accepted.status_code == 202
    assert accepted.get_json() == {"accepted": True, "status": "syncing"}
    assert duplicate.status_code == 409


def test_file_classification_never_turns_disguised_executable_into_video():
    assert classify_file("movie.mp4", "video/mp4").is_video
    assert classify_file("movie.mkv.exe", "application/octet-stream").kind == "software"
    assert classify_file("backup.tar.zst", "application/octet-stream").extension == ".tar.zst"
    subtitle = classify_file("movie.pt-BR.srt", "application/x-subrip")
    assert subtitle.is_subtitle and not subtitle.is_video


def test_rest_helpers_create_folder_idempotently_with_pregenerated_id():
    location = "folder-created-12345"
    client, session = drive_client(
        [
            FakeResponse(payload={"files": []}),
            FakeResponse(payload={"files": []}),
            FakeResponse(payload={"ids": [location]}),
            FakeResponse(payload={"id": location}),
            FakeResponse(
                payload={
                    "files": [
                        {"id": location, "name": "Series", "mimeType": "application/vnd.google-apps.folder"}
                    ]
                }
            ),
        ]
    )
    assert client.ensure_folder("parent-folder-12345", "Series") == location
    assert client.ensure_folder("parent-folder-12345", "Series") == location
    post = next(call for call in session.calls if call["method"] == "POST")
    assert post["json"]["id"] == location
    assert post["json"]["appProperties"]["ofc_managed"] == "1"
    assert not session.responses


@pytest.mark.parametrize("path", ["/video/Filmes", "C:/video/Filmes", "../Filmes"])
def test_ensure_folder_path_rejects_non_relative_roots_before_drive_request(path: str):
    client, session = drive_client([])
    with pytest.raises(drive_service.UnsafeMediaError):
        client.ensure_folder_path("parent-folder-12345", path)
    assert session.calls == []


def test_existing_folder_is_patched_with_management_identity():
    folder_id = "existing-folder-12345"
    client, session = drive_client(
        [
            FakeResponse(payload={"files": []}),
            FakeResponse(payload={"files": [{"id": folder_id, "name": "Filmes"}]}),
            FakeResponse(payload={"id": folder_id, "name": "Filmes"}),
        ]
    )
    assert client.ensure_folder("parent-folder-12345", "Filmes") == folder_id
    patch = next(call for call in session.calls if call["method"] == "PATCH")
    assert patch["json"]["appProperties"]["ofc_folder_key"]


def test_folder_creation_disambiguates_sanitized_name_owned_by_other_identity():
    location = "folder-collision-12345"
    client, session = drive_client(
        [
            FakeResponse(payload={"files": []}),
            FakeResponse(
                payload={
                    "files": [
                        {
                            "id": "other-folder-12345",
                            "name": "Series_",
                            "appProperties": {"ofc_original_hash": "0" * 64},
                        }
                    ]
                }
            ),
            FakeResponse(payload={"ids": [location]}),
            FakeResponse(payload={"id": location}),
        ]
    )
    assert client.ensure_folder("parent-folder-12345", "Series?") == location
    post = next(call for call in session.calls if call["method"] == "POST")
    assert post["json"]["name"].startswith("Series_--")
    assert post["json"]["appProperties"]["ofc_original_hash"] != "0" * 64


def test_resumable_upload_uses_pregenerated_id_chunks_and_checksum(tmp_path: Path):
    content = b"a" * (256 * 1024) + b"final"
    source = tmp_path / "episode.mp4"
    source.write_bytes(content)
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(content).hexdigest()
    file_id = "uploaded-file-12345"
    session_url = (
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=fake"
    )
    client, session = drive_client(
        [
            FakeResponse(payload={"ids": [file_id]}),
            FakeResponse(headers={"Location": session_url}),
            FakeResponse(status=308, headers={"Range": f"bytes=0-{256 * 1024 - 1}"}),
            FakeResponse(payload={"id": file_id}),
            FakeResponse(
                payload={
                    "id": file_id,
                    "name": source.name,
                    "size": str(len(content)),
                    "mimeType": "video/mp4",
                    "md5Checksum": md5,
                    "sha256Checksum": sha256,
                }
            ),
        ]
    )
    progress = []
    result = client.upload_resumable(
        source,
        "target-folder-12345",
        chunk_size=256 * 1024,
        app_properties={"ofc_job_id": "job-1"},
        on_progress=lambda offset, state: progress.append((offset, state)),
    )
    assert result["id"] == file_id
    assert result["upload_state"]["completed"] is True
    assert progress[-1][0] == len(content)
    post = next(call for call in session.calls if call["method"] == "POST")
    assert post["json"]["id"] == file_id
    assert post["json"]["appProperties"]["ofc_job_id"] == "job-1"
    uploads = [call for call in session.calls if call["method"] == "PUT"]
    assert [call["headers"]["Content-Range"] for call in uploads] == [
        f"bytes 0-{256 * 1024 - 1}/{len(content)}",
        f"bytes {256 * 1024}-{len(content) - 1}/{len(content)}",
    ]


def test_resumable_upload_queries_server_offset_before_continuing(tmp_path: Path):
    content = b"x" * (256 * 1024) + b"tail"
    source = tmp_path / "resume.bin"
    source.write_bytes(content)
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(content).hexdigest()
    file_id = "resumed-file-12345"
    session_url = (
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=resume"
    )
    client, session = drive_client(
        [
            FakeResponse(status=308, headers={"Range": f"bytes=0-{256 * 1024 - 1}"}),
            FakeResponse(payload={"id": file_id}),
            FakeResponse(
                payload={
                    "id": file_id,
                    "size": str(len(content)),
                    "md5Checksum": md5,
                    "sha256Checksum": sha256,
                }
            ),
        ]
    )
    result = client.upload_resumable(
        source,
        "target-folder-12345",
        chunk_size=256 * 1024,
        upload_state={
            "file_id": file_id,
            "session_url": session_url,
            "offset": 1,
            "size": len(content),
            "sha256": sha256,
        },
    )
    puts = [call for call in session.calls if call["method"] == "PUT"]
    assert puts[0]["headers"]["Content-Range"] == f"bytes */{len(content)}"
    assert puts[1]["headers"]["Content-Range"] == (
        f"bytes {256 * 1024}-{len(content) - 1}/{len(content)}"
    )
    assert result["upload_state"]["offset"] == len(content)


def test_drive_download_resumes_part_and_renames_only_after_checksum(tmp_path: Path):
    content = b"already-" + b"downloaded"
    destination = tmp_path / "movie.bin"
    destination.with_name("movie.bin.part").write_bytes(b"already-")
    client, session = drive_client(
        [FakeResponse(status=206, chunks=[b"down", b"loaded"])]
    )
    result = client.download_to_local(
        "download-file-12345",
        destination,
        metadata={
            "id": "download-file-12345",
            "name": destination.name,
            "size": str(len(content)),
            "md5Checksum": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            "sha256Checksum": hashlib.sha256(content).hexdigest(),
        },
    )
    assert destination.read_bytes() == content
    assert not destination.with_name("movie.bin.part").exists()
    assert result["local_path"] == str(destination)
    assert session.calls[0]["headers"]["Range"] == "bytes=8-17"


class FakeStore:
    transitions = {
        "queued": {"validating", "failed", "cancelled"},
        "validating": {"downloading", "downloaded", "failed", "cancelled"},
        "downloading": {"downloaded", "failed", "cancelled"},
        "downloaded": {"classifying", "failed", "cancelled"},
        "classifying": {"uploading", "verifying", "failed", "cancelled"},
        "uploading": {"verifying", "failed", "cancelled"},
        "verifying": {"completed", "failed", "cancelled"},
    }

    def __init__(self, *, upload=None, download=None):
        self.upload = upload
        self.download = download
        self.updates = []
        self.sha256s = []
        self.states = {
            item["id"]: item["state"] for item in (upload, download) if item is not None
        }

    def _transition(self, job_id, state):
        previous = self.states[job_id]
        assert state == previous or state in self.transitions.get(previous, set()), (
            f"transicao de teste invalida: {previous} -> {state}"
        )
        self.states[job_id] = state
        self.updates.append((job_id, {"state": state}))

    def claim_upload(self):
        selected, self.upload = self.upload, None
        if selected:
            assert selected["target"] == "gdrive"
            assert selected["state"] in {
                "downloaded",
                "classifying",
                "uploading",
                "verifying",
            }
            claimed = "classifying" if selected["state"] == "downloaded" else selected["state"]
            self._transition(selected["id"], claimed)
            selected = {**selected, "state": claimed}
        return selected

    def claim_download(self):
        selected, self.download = self.download, None
        if selected:
            assert selected["target"] == "local"
            assert selected["state"] in {
                "queued",
                "validating",
                "downloading",
                "downloaded",
                "classifying",
                "verifying",
            }
            claimed = "validating" if selected["state"] == "queued" else selected["state"]
            self._transition(selected["id"], claimed)
            selected = {**selected, "state": claimed}
        return selected

    def update(self, job_id, **fields):
        if fields.get("state"):
            previous = self.states[job_id]
            selected = fields["state"]
            assert selected == previous or selected in self.transitions.get(previous, set()), (
                f"transicao de teste invalida: {previous} -> {selected}"
            )
            self.states[job_id] = selected
        self.updates.append((job_id, fields))

    def persist_file_sha256(self, file_id, sha256):
        self.sha256s.append((int(file_id), sha256))
        return True


class FakeTransferClient:
    def __init__(self):
        self.uploads = []
        self.downloads = []
        self.folders = []
        self.metadata = {}
        self.metadata_calls = []

    def ensure_folder_path(self, root_id, path):
        self.folders.append((root_id, path))
        return "managed-folder-12345"

    def upload_resumable(self, source, parent_id, **kwargs):
        data = source.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        drive_file_id = (
            "worker-upload-12345" if not self.uploads else f"worker-upload-{len(self.uploads) + 12345}"
        )
        state = kwargs["upload_state"]
        state.update(
            {
                "file_id": drive_file_id,
                "size": len(data),
                "offset": len(data),
                "completed": True,
                "sha256": sha256,
            }
        )
        kwargs["on_progress"](len(data), dict(state))
        result = {
            "id": drive_file_id,
            "name": kwargs["name"],
            "size": str(len(data)),
            "mimeType": kwargs["mime_type"],
            "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            "sha256Checksum": sha256,
            "upload_state": dict(state),
        }
        self.metadata[drive_file_id] = {
            **result,
            "parents": [parent_id],
        }
        self.uploads.append((source, parent_id, kwargs))
        return result

    def get_file_metadata(self, file_id):
        self.metadata_calls.append(file_id)
        return self.metadata[file_id]

    def download_to_local(self, file_id, destination, *, metadata, on_progress):
        payload = b"remote-data"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        on_progress(len(payload))
        self.downloads.append((file_id, destination))
        return {**metadata, "local_path": str(destination)}


def worker_settings(tmp_path: Path):
    return SimpleNamespace(
        gdrive_root_id="root-folder-12345",
        media_root=tmp_path,
        resume_root=tmp_path / "resume",
    )


def test_worker_claims_downloaded_upload_job_and_publishes_manifest(tmp_path: Path):
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"episode")
    unlinked = tmp_path / "readme.txt"
    unlinked.write_bytes(b"notes")
    job = {
        "id": "job-upload-1",
        "source_site": "1337x",
        "infohash": "a" * 40,
        "target": "gdrive",
        "state": "downloaded",
        "destination_path": "Series/Teste",
        "local_files": [
            {
                "file_id": 41,
                "local_path": str(source),
                "relative_path": "Season 1/episode.mp4",
            },
            {"local_path": str(unlinked), "relative_path": "Season 1/readme.txt"},
        ],
        "drive_files": [],
        "upload_state": {},
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("gdrive")["state"] == "completed"
    assert client.uploads
    assert [update.get("state") for _, update in store.updates if update.get("state")] == [
        "classifying",
        "uploading",
        "verifying",
        "completed",
    ]
    final = next(
        update for _, update in reversed(store.updates) if update.get("state") == "completed"
    )
    assert final["bytes_done"] == len(b"episode") + len(b"notes")
    assert final["drive_files"][0]["drive_file_id"] == "worker-upload-12345"
    assert store.sha256s == [(41, hashlib.sha256(b"episode").hexdigest())]


def test_torrent_upload_keeps_classified_root_once_and_original_subtree(
    tmp_path: Path,
):
    episode = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    episode.write_bytes(b"episode")
    subtitle.write_bytes(b"subtitle")
    job = {
        "id": "job-upload-tree",
        "source_site": "1337x",
        "infohash": "e" * 40,
        "target": "gdrive",
        "state": "downloaded",
        "destination_path": "video/Series/Dexter",
        "local_files": [
            {
                "file_id": 61,
                "local_path": str(episode),
                "relative_path": (
                    "video/Series/Dexter/Season 01/episode.mkv"
                ),
            },
            {
                "file_id": 62,
                "local_path": str(subtitle),
                "relative_path": "Series/Dexter/Subtitles/episode.srt",
            },
        ],
        "drive_files": [],
        "upload_state": {},
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )

    assert worker.run_once("gdrive")["state"] == "completed"
    final = next(
        update
        for _, update in reversed(store.updates)
        if update.get("state") == "completed"
    )
    by_id = {
        entry["drive_file_id"]: entry["relative_path"]
        for entry in final["drive_files"]
    }
    assert set(by_id.values()) == {
        "Season 01/episode.mkv",
        "Subtitles/episode.srt",
    }
    assert client.folders == [
        ("root-folder-12345", "video/Series/Dexter"),
        ("managed-folder-12345", "Season 01"),
        ("managed-folder-12345", "Subtitles"),
    ]
    assert all("video/Series/Dexter" not in path for path in by_id.values())


def test_upload_worker_resumes_stale_uploading_job_idempotently(tmp_path: Path):
    source = tmp_path / "resume.mp4"
    source.write_bytes(b"resume-upload")
    job = {
        "id": "job-upload-resume",
        "source_site": "1337x",
        "infohash": "b" * 40,
        "target": "gdrive",
        "state": "uploading",
        "destination_path": "Series/Resume",
        "local_files": [
            {"file_id": 52, "local_path": str(source), "relative_path": "resume.mp4"}
        ],
        "drive_files": [],
        "upload_state": {"files": {}},
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("gdrive")["state"] == "completed"
    assert len(client.uploads) == 1
    assert [update.get("state") for _, update in store.updates if update.get("state")] == [
        "uploading",
        "uploading",
        "verifying",
        "completed",
    ]


def test_upload_worker_resumes_verifying_by_revalidating_remote_only(tmp_path: Path):
    payload = b"already-uploaded"
    digest = hashlib.sha256(payload).hexdigest()
    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    file_id = "verified-upload-12345"
    # Chave legacy anterior ao endurecimento; a retomada nao pode criar copia
    # sanitizada adicional.
    relative = "episode?.mp4"
    job = {
        "id": "job-upload-verifying",
        "source_site": "1337x",
        "infohash": "c" * 40,
        "target": "gdrive",
        "state": "verifying",
        "destination_path": "Series/Verified",
        "local_files": [],
        "drive_files": [
            {
                "drive_file_id": file_id,
                "relative_path": relative,
                "size": len(payload),
                "md5_checksum": md5,
                "sha256_checksum": digest,
            }
        ],
        "upload_state": {
            "files": {
                relative: {
                    "completed": True,
                    "size": len(payload),
                    "md5": md5,
                    "sha256": digest,
                }
            }
        },
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    client.metadata[file_id] = {
        "id": file_id,
        "name": relative,
        "size": str(len(payload)),
        "mimeType": "video/mp4",
        "md5Checksum": md5,
        "sha256Checksum": digest,
    }
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("gdrive")["state"] == "completed"
    assert client.uploads == [] and client.metadata_calls == [file_id]
    assert [update.get("state") for _, update in store.updates if update.get("state")] == [
        "verifying",
        "completed",
    ]


def test_postgres_store_persists_verified_sha256_without_rehashing_file():
    digest = hashlib.sha256(b"verified-upload").hexdigest()
    database = FakeSyncDatabase()

    def execute(sql, params=None):
        database.calls.append((sql, params))
        if "UPDATE catalog.torrent_files SET sha256" in sql:
            return FakeSqlResult({"id": 41})
        return FakeSqlResult()

    database.execute = execute

    @contextmanager
    def factory(settings):
        yield database

    store = PostgresTransferStore(SimpleNamespace(), connection_factory=factory)
    assert store.persist_file_sha256("41", digest) is True
    update = next(call for call in database.calls if "SET sha256" in call[0])
    assert update[1] == (digest, 41, digest)
    call_count = len(database.calls)
    assert store.persist_file_sha256(None, digest) is False
    assert len(database.calls) == call_count


def test_postgres_store_claims_only_stale_intermediate_jobs_with_lease_refresh():
    database = FakeSyncDatabase()

    def execute(sql, params=None):
        database.calls.append((sql, params))
        return FakeSqlResult(
            {
                "id": "job-stale-upload",
                "target": "gdrive",
                "state": "uploading",
            }
        )

    database.execute = execute

    @contextmanager
    def factory(settings):
        yield database

    store = PostgresTransferStore(
        SimpleNamespace(), connection_factory=factory, resume_after_seconds=45
    )
    assert store.claim_upload()["state"] == "uploading"
    sql, params = database.calls[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "updated_at <= now()-make_interval" in sql
    assert params[2] == ["classifying", "uploading", "verifying"]
    assert params[3] == 45


def test_worker_claims_local_job_and_publishes_downloaded_files(tmp_path: Path):
    payload = b"remote-data"
    job = {
        "id": "job-download-1",
        "source_site": "gdrive",
        "target": "local",
        "state": "queued",
        "destination_path": str(tmp_path / "downloads"),
        "drive_files": [
            {
                "drive_file_id": "remote-file-12345",
                "relative_path": "Movies/movie.bin",
                "name": "movie.bin",
                "size": len(payload),
                "md5_checksum": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
                "sha256_checksum": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "local_files": [],
    }
    store = FakeStore(download=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("local")["state"] == "completed"
    assert [update.get("state") for _, update in store.updates if update.get("state")] == [
        "validating",
        "downloading",
        "downloaded",
        "classifying",
        "verifying",
        "completed",
    ]
    final = next(
        update for _, update in reversed(store.updates) if update.get("state") == "completed"
    )
    assert final["bytes_done"] == len(payload)
    assert Path(final["local_files"][0]["local_path"]).read_bytes() == payload


def test_drive_download_keeps_classified_root_once_and_original_subtree(
    tmp_path: Path,
):
    payload = b"remote-data"
    destination = tmp_path / "video" / "Series" / "Dexter"
    job = {
        "id": "job-download-tree",
        "source_site": "gdrive",
        "target": "local",
        "state": "queued",
        "destination_path": str(destination),
        "drive_files": [
            {
                "drive_file_id": "remote-episode-12345",
                "relative_path": (
                    "video/Series/Dexter/Season 01/episode.bin"
                ),
                "size": len(payload),
                "md5_checksum": hashlib.md5(
                    payload, usedforsecurity=False
                ).hexdigest(),
                "sha256_checksum": hashlib.sha256(payload).hexdigest(),
            },
            {
                "drive_file_id": "remote-subtitle-12345",
                "relative_path": "Series/Dexter/Subtitles/episode.srt",
                "size": len(payload),
                "md5_checksum": hashlib.md5(
                    payload, usedforsecurity=False
                ).hexdigest(),
                "sha256_checksum": hashlib.sha256(payload).hexdigest(),
            },
        ],
        "local_files": [],
    }
    store = FakeStore(download=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )

    assert worker.run_once("local")["state"] == "completed"
    final = next(
        update
        for _, update in reversed(store.updates)
        if update.get("state") == "completed"
    )
    assert {Path(item["local_path"]) for item in final["local_files"]} == {
        destination / "Season 01" / "episode.bin",
        destination / "Subtitles" / "episode.srt",
    }
    assert not (destination / "video" / "Series" / "Dexter").exists()
    assert not (destination / "Series" / "Dexter").exists()


def test_drive_download_uses_stable_suffix_for_unrelated_local_collision(
    tmp_path: Path,
):
    payload = b"remote-data"
    destination = tmp_path / "video" / "Series" / "Dexter"
    occupied = destination / "Season 01" / "episode.bin"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"unrelated")

    def run(job_id: str) -> Path:
        job = {
            "id": job_id,
            "source_site": "gdrive",
            "target": "local",
            "state": "queued",
            "destination_path": str(destination),
            "drive_files": [
                {
                    "drive_file_id": "remote-collision-12345",
                    "relative_path": "Series/Dexter/Season 01/episode.bin",
                    "size": len(payload),
                    "md5_checksum": hashlib.md5(
                        payload, usedforsecurity=False
                    ).hexdigest(),
                    "sha256_checksum": hashlib.sha256(payload).hexdigest(),
                }
            ],
            "local_files": [],
        }
        store = FakeStore(download=job)
        worker = DriveTransferWorker(
            worker_settings(tmp_path),
            FakeTransferClient(),
            store=store,
            allowed_local_roots=(tmp_path,),
        )
        assert worker.run_once("local")["state"] == "completed"
        final = next(
            update
            for _, update in reversed(store.updates)
            if update.get("state") == "completed"
        )
        return Path(final["local_files"][0]["local_path"])

    first = run("job-local-collision-1")
    second = run("job-local-collision-2")
    digest = hashlib.sha256(b"gdrive:remote-collision-12345").hexdigest()[:10]
    assert first == second == occupied.with_name(f"episode--{digest}.bin")
    assert occupied.read_bytes() == b"unrelated"
    assert first.read_bytes() == payload


@pytest.mark.parametrize(
    ("state", "expected_states", "needs_download"),
    [
        (
            "validating",
            ["validating", "downloading", "downloaded", "classifying", "verifying", "completed"],
            True,
        ),
        (
            "downloading",
            ["downloading", "downloading", "downloaded", "classifying", "verifying", "completed"],
            True,
        ),
        ("downloaded", ["downloaded", "classifying", "verifying", "completed"], False),
        ("classifying", ["classifying", "verifying", "completed"], False),
        ("verifying", ["verifying", "completed"], False),
    ],
)
def test_local_worker_resumes_every_stale_phase_without_duplicate_group_tree(
    tmp_path: Path, state: str, expected_states: list[str], needs_download: bool
):
    payload = b"remote-data"
    destination = tmp_path / "downloads" / "Movies"
    if not needs_download:
        destination.mkdir(parents=True)
        (destination / "movie.bin").write_bytes(payload)
    job = {
        "id": f"job-local-{state}",
        "source_site": "gdrive",
        "target": "local",
        "state": state,
        "destination_path": str(destination),
        "drive_files": [
            {
                "drive_file_id": "remote-file-12345",
                "relative_path": "Movies/movie.bin",
                "name": "movie.bin",
                "size": len(payload),
                "md5_checksum": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
                "sha256_checksum": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "local_files": [],
    }
    store = FakeStore(download=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("local")["state"] == "completed"
    states = [update.get("state") for _, update in store.updates if update.get("state")]
    assert states == expected_states
    final = next(update for _, update in reversed(store.updates) if update.get("state") == "completed")
    final_path = Path(final["local_files"][0]["local_path"])
    assert final_path == destination / "movie.bin"
    assert not (destination / "Movies" / "movie.bin").exists()
    assert bool(client.downloads) is needs_download


def test_upload_source_reparse_point_is_rejected_without_touching_drive(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "real.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        drive_service,
        "_is_reparse_point",
        lambda path: Path(path).absolute() == source.absolute(),
    )
    job = {
        "id": "job-upload-link",
        "source_site": "1337x",
        "infohash": "d" * 40,
        "target": "gdrive",
        "state": "classifying",
        "destination_path": "Series/Safe",
        "local_files": [{"local_path": str(source), "relative_path": "linked.mp4"}],
        "drive_files": [],
        "upload_state": {},
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("gdrive")["state"] == "failed"
    assert client.uploads == []


def test_upload_manifest_absolute_relative_path_is_rejected_before_drive_api(
    tmp_path: Path,
):
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video")
    job = {
        "id": "job-upload-absolute-relative",
        "source_site": "1337x",
        "infohash": "f" * 40,
        "target": "gdrive",
        "state": "classifying",
        "destination_path": "video/Series/Safe",
        "local_files": [
            {
                "local_path": str(source),
                "relative_path": "/video/Series/Safe/episode.mp4",
            }
        ],
        "drive_files": [],
        "upload_state": {},
    }
    store = FakeStore(upload=job)
    client = FakeTransferClient()
    worker = DriveTransferWorker(
        worker_settings(tmp_path), client, store=store, allowed_local_roots=(tmp_path,)
    )
    assert worker.run_once("gdrive")["state"] == "failed"
    assert client.folders == [] and client.uploads == []
