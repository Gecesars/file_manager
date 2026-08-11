from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import requests

from ofc_media import control
from ofc_media.control import (
    LARGE_TRANSFER_BYTES,
    ControlPlane,
    LargeTransferConfirmationRequired,
    MAX_TRANSFER_FILES,
    PlaybackCapacityError,
    _selected_file_ids,
)
from ofc_media.safety import UnsafeMediaError
from ofc_media.subtitle_tracks import track_id


INFOHASH = "a" * 40


class FakeResult:
    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self.row

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class QueueDatabase:
    def __init__(self, *results: FakeResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0

    def execute(self, sql: str, params: Any = None) -> FakeResult:
        self.calls.append((sql, params))
        if self.results:
            return self.results.pop(0)
        return FakeResult()

    def commit(self) -> None:
        self.commits += 1


def install_connection(
    monkeypatch: pytest.MonkeyPatch, database: Any
) -> None:
    @contextmanager
    def fake_connection(_settings: Any) -> Iterator[Any]:
        yield database

    monkeypatch.setattr(control, "connection", fake_connection)


def bare_plane(**settings: Any) -> ControlPlane:
    plane = object.__new__(ControlPlane)
    plane.settings = SimpleNamespace(**settings)
    plane.internal_headers = {"Authorization": "Bearer internal"}
    return plane


def test_inventory_endpoints_expose_ui_aliases(monkeypatch: pytest.MonkeyPatch):
    database = QueueDatabase(
        FakeResult(
            row={
                "torrent_count": 7,
                "file_count": 19,
                "gdrive_file_count": 5,
                "active_transfer_count": 2,
                "files_by_kind": {"video": {"count": 3}},
            }
        ),
        FakeResult(
            row={
                "total_count": 1,
                "items": [{"file_id": 81, "path": "Filme.mkv"}],
            }
        ),
        FakeResult(
            row={
                "total_count": 1,
                "items": [{"id": "job-1", "state": "queued"}],
            }
        ),
    )
    install_connection(monkeypatch, database)
    plane = bare_plane()

    dashboard = plane.dashboard()
    files = plane.files(
        q="filme",
        site="filecr",
        kind="video",
        presence="missing",
        page="1",
        per_page="25",
    )
    transfers = plane.transfers(
        state="queued",
        target="gdrive",
        site="filecr",
        infohash=INFOHASH,
        page="1",
        per_page="10",
    )

    assert dashboard["titles"] == dashboard["torrent_count"] == 7
    assert dashboard["files"] == dashboard["file_count"] == 19
    assert dashboard["drive_files"] == dashboard["gdrive_file_count"] == 5
    assert dashboard["active_transfers"] == 2
    assert files["items"][0]["id"] == files["items"][0]["file_id"] == 81
    assert files["per_page"] == files["page_size"] == 25
    assert transfers["per_page"] == transfers["page_size"] == 10


def test_detail_lists_every_file_and_assigns_opaque_subtitle_track(
    monkeypatch: pytest.MonkeyPatch,
):
    database = QueueDatabase(
        FakeResult(row={"site": "filecr", "infohash": INFOHASH, "title": "Filme"}),
        FakeResult(
            rows=[
                {
                    "id": 1,
                    "path": "Filme.mkv",
                    "size": 100,
                    "extension": ".mkv",
                    "file_kind": "video",
                    "mime_type": "video/x-matroska",
                    "is_video": True,
                    "is_subtitle": False,
                },
                {
                    "id": 2,
                    "path": "capa.jpg",
                    "size": 10,
                    "extension": ".jpg",
                    "file_kind": "image",
                    "mime_type": "image/jpeg",
                    "is_video": False,
                    "is_subtitle": False,
                },
            ]
        ),
        FakeResult(
            rows=[
                {
                    "torrent_path": "Filme.mkv",
                    "language": "pt-BR",
                    "file_name": "Filme.srt",
                    "status": "synced",
                    "provider": "local",
                    "match_confidence": 1.0,
                    "extension": ".srt",
                    "subtitle_path": "D:/vault/Filme.srt",
                    "synced_path": None,
                }
            ]
        ),
    )
    install_connection(monkeypatch, database)

    result = bare_plane().detail("filecr", INFOHASH)

    assert [item["file_kind"] for item in result["files"]] == ["video", "image"]
    assert [item["id"] for item in result["videos"]] == [1]
    subtitle = result["subtitles"][0]
    assert subtitle["track_id"] == track_id("filecr", INFOHASH, "Filme.mkv", "pt-BR")
    assert "subtitle_path" not in subtitle and "synced_path" not in subtitle
    detail_sql = database.calls[1][0]
    assert "t.active" in detail_sql
    assert "own_drive.active AND own_drive.can_download" in detail_sql


class TransferDatabase:
    def __init__(
        self,
        files: list[dict[str, Any]],
        *,
        existing: dict[str, Any] | None = None,
    ) -> None:
        self.files = files
        self.existing = existing
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0

    def execute(self, sql: str, params: Any = None) -> FakeResult:
        self.calls.append((sql, params))
        if "FROM catalog.torrents" in sql:
            return FakeResult(
                row={
                    "id": 11,
                    "title": "Meu: Filme",
                    "display_name": "Meu Filme",
                    "category": "Filmes/Acao",
                }
            )
        if "FROM catalog.torrent_files" in sql:
            return FakeResult(rows=self.files)
        if "FROM runtime.transfer_jobs" in sql:
            return FakeResult(row=self.existing)
        return FakeResult()

    def commit(self) -> None:
        self.commits += 1


class OkResponse:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self.payload = {} if payload is None else payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


def test_torrent_transfer_is_audited_and_dispatched_after_commit(
    monkeypatch: pytest.MonkeyPatch,
):
    database = TransferDatabase(
        [
            {
                "id": 4,
                "path": "release/filme.mkv",
                "file_kind": "video",
                "mime_type": "video/x-matroska",
                "size": 123,
            }
        ]
    )
    install_connection(monkeypatch, database)
    dispatched: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> OkResponse:
        assert database.commits == 1
        dispatched.append({"url": url, **kwargs})
        return OkResponse()

    monkeypatch.setattr(control.requests, "post", fake_post)
    plane = bare_plane(torrent_engine_url="http://torrent")

    result = plane.create_transfer(
        site="FILECR",
        infohash=INFOHASH.upper(),
        target="gdrive",
        file_ids=[4],
    )

    assert result["state"] == "queued"
    assert result["destination_path"] == "video/Filmes/Acao/Meu_ Filme"
    assert dispatched[0]["url"] == "http://torrent/internal/materializations"
    assert dispatched[0]["json"] == {"job_id": result["id"]}
    sql = "\n".join(call[0] for call in database.calls)
    assert "INSERT INTO runtime.transfer_jobs" in sql
    assert "INSERT INTO ops.audit_events" in sql
    assert "pg_advisory_xact_lock" in sql
    assert sql.index("pg_advisory_xact_lock") < sql.index(
        "INSERT INTO runtime.transfer_jobs"
    )


def test_transfer_idempotency_returns_existing_job_under_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    existing = {
        "id": "existing-job",
        "source_site": "filecr",
        "infohash": INFOHASH,
        "target": "local",
        "state": "downloading",
        "selected_file_ids": [4],
        "destination_path": "video/Filmes/Acao/Meu_ Filme",
        "bytes_total": 123,
        "bytes_done": 12,
        "local_files": [],
        "drive_files": [],
        "error": None,
    }
    database = TransferDatabase(
        [
            {
                "id": 4,
                "path": "release/filme.mkv",
                "file_kind": "video",
                "mime_type": "video/x-matroska",
                "size": 123,
            }
        ],
        existing=existing,
    )
    install_connection(monkeypatch, database)
    monkeypatch.setattr(
        control.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("job deduplicado nao deve ser despachado"),
    )

    result = bare_plane(torrent_engine_url="http://torrent").create_transfer(
        site="filecr", infohash=INFOHASH, target="local", file_ids=[4]
    )

    assert result["id"] == "existing-job"
    assert result["deduplicated"] is True
    sql = "\n".join(call[0] for call in database.calls)
    assert "pg_advisory_xact_lock" in sql
    assert "INSERT INTO runtime.transfer_jobs" not in sql


def test_large_transfer_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
):
    large_size = LARGE_TRANSFER_BYTES + 1
    file_row = {
        "id": 4,
        "path": "release/grande.mkv",
        "file_kind": "video",
        "mime_type": "video/x-matroska",
        "size": large_size,
    }
    denied_database = TransferDatabase([file_row])
    install_connection(monkeypatch, denied_database)
    plane = bare_plane(torrent_engine_url="http://torrent")

    with pytest.raises(LargeTransferConfirmationRequired) as raised:
        plane.create_transfer(
            site="filecr", infohash=INFOHASH, target="local", file_ids=[4]
        )

    assert raised.value.bytes_total == large_size
    assert not any(
        "INSERT INTO runtime.transfer_jobs" in sql for sql, _ in denied_database.calls
    )

    allowed_database = TransferDatabase([file_row])
    install_connection(monkeypatch, allowed_database)
    monkeypatch.setattr(control.requests, "post", lambda *_args, **_kwargs: OkResponse())
    result = plane.create_transfer(
        site="filecr",
        infohash=INFOHASH,
        target="local",
        file_ids=[4],
        confirm_large=True,
    )
    assert result["bytes_total"] == large_size
    assert any(
        "INSERT INTO runtime.transfer_jobs" in sql for sql, _ in allowed_database.calls
    )


def test_playback_selection_requires_active_downloadable_drive_file(
    monkeypatch: pytest.MonkeyPatch,
):
    database = QueueDatabase(FakeResult(row=None))
    install_connection(monkeypatch, database)

    with pytest.raises(UnsafeMediaError, match="nao aprovado"):
        bare_plane().create_playback(
            site="gdrive",
            infohash=INFOHASH,
            file_id=9,
            mode="adaptive",
            quality_cap_bps=0,
        )

    selection_sql = database.calls[0][0]
    assert "t.active" in selection_sql
    assert "d.active AND d.can_download" in selection_sql


def test_transcode_capacity_closes_source_and_persisted_playback(
    monkeypatch: pytest.MonkeyPatch,
):
    database = QueueDatabase(FakeResult(row={"id": 4}))
    install_connection(monkeypatch, database)
    posts: list[str] = []
    deletes: list[str] = []

    def fake_post(url: str, **_kwargs: Any) -> OkResponse:
        posts.append(url)
        return OkResponse(status_code=429 if url.endswith("/internal/transcodes") else 200)

    def fake_delete(url: str, **_kwargs: Any) -> OkResponse:
        deletes.append(url)
        return OkResponse()

    monkeypatch.setattr(control.requests, "post", fake_post)
    monkeypatch.setattr(control.requests, "delete", fake_delete)
    plane = bare_plane(
        torrent_engine_url="http://torrent",
        drive_source_url="http://drive",
        transcoder_url="http://transcoder",
        session_pepper="p" * 32,
    )

    with pytest.raises(PlaybackCapacityError, match="transcodificacao"):
        plane.create_playback(
            site="filecr",
            infohash=INFOHASH,
            file_id=4,
            mode="adaptive",
            quality_cap_bps=0,
        )

    assert posts == [
        "http://torrent/internal/sessions",
        "http://transcoder/internal/transcodes",
    ]
    session_id = deletes[0].rsplit("/", 1)[-1]
    assert deletes == [
        f"http://transcoder/internal/transcodes/{session_id}",
        f"http://torrent/internal/sessions/{session_id}",
    ]
    sql = "\n".join(call[0] for call in database.calls)
    assert "SET state='closed',error=" in sql
    assert "UPDATE runtime.download_jobs SET state='closed'" in sql
    assert "UPDATE runtime.transcode_jobs" in sql


def test_gdrive_to_local_carries_remote_manifest_without_torrent_dispatch(
    monkeypatch: pytest.MonkeyPatch,
):
    database = TransferDatabase(
        [
            {
                "id": 9,
                "path": "Filmes/Meu Filme/filme.mkv",
                "file_kind": "video",
                "mime_type": "video/x-matroska",
                "size": 456,
                "drive_file_id": "remote_file_12345",
                "drive_relative_path": "Filmes/Meu Filme/filme.mkv",
                "drive_mime_type": "video/x-matroska",
                "md5_checksum": "abcd",
                "drive_sha256_checksum": "f" * 64,
                "can_download": True,
                "drive_active": True,
            }
        ]
    )
    install_connection(monkeypatch, database)
    monkeypatch.setattr(
        control.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("torrent engine nao deve ser chamado"),
    )

    result = bare_plane(torrent_engine_url="http://torrent").create_transfer(
        site="gdrive", infohash=INFOHASH, target="local", file_ids=[9]
    )

    assert result["state"] == "queued"
    assert "drive_files" not in result
    insert_params = next(
        params
        for sql, params in database.calls
        if "INSERT INTO runtime.transfer_jobs" in sql
    )
    assert insert_params[-1].obj == [
        {
            "drive_file_id": "remote_file_12345",
            "relative_path": "Filmes/Meu Filme/filme.mkv",
            "size": 456,
            "mime_type": "video/x-matroska",
            "md5_checksum": "abcd",
            "sha256_checksum": "f" * 64,
        }
    ]


def test_transfer_selection_rejects_unsafe_or_unowned_files(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ValueError, match="duplicatas"):
        _selected_file_ids([1, 1])
    with pytest.raises(ValueError, match="limite"):
        _selected_file_ids(list(range(1, MAX_TRANSFER_FILES + 2)))
    with pytest.raises(ValueError, match="gdrive para gdrive"):
        bare_plane().create_transfer(
            site="gdrive", infohash=INFOHASH, target="gdrive", file_ids=[1]
        )

    database = TransferDatabase([])
    install_connection(monkeypatch, database)
    with pytest.raises(UnsafeMediaError, match="nao pertence"):
        bare_plane(torrent_engine_url="http://torrent").create_transfer(
            site="filecr", infohash=INFOHASH, target="local", file_ids=[99]
        )
    assert not any("INSERT INTO runtime.transfer_jobs" in sql for sql, _ in database.calls)


def test_drive_sync_is_internal_and_audited(monkeypatch: pytest.MonkeyPatch):
    database = QueueDatabase()
    install_connection(monkeypatch, database)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, **kwargs: Any) -> OkResponse:
        calls.append((url, kwargs))
        return OkResponse({"files": 12})

    monkeypatch.setattr(control.requests, "post", fake_post)
    result = bare_plane(drive_source_url="http://drive").sync_drive()

    assert result == {"files": 12}
    assert calls[0][0] == "http://drive/internal/sync"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer internal"
    assert any("INSERT INTO ops.audit_events" in sql for sql, _ in database.calls)


def test_drive_sync_already_running_is_a_successful_idempotent_response(
    monkeypatch: pytest.MonkeyPatch,
):
    database = QueueDatabase()
    install_connection(monkeypatch, database)
    monkeypatch.setattr(
        control.requests,
        "post",
        lambda *_args, **_kwargs: OkResponse(status_code=409),
    )
    result = bare_plane(drive_source_url="http://drive").sync_drive()
    assert result == {"accepted": False, "status": "already_syncing"}


def test_authenticated_subtitle_is_resolved_and_converted_to_webvtt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    mounted = tmp_path / "mounted"
    subtitle_file = mounted / "pt-BR" / "filme.srt"
    subtitle_file.parent.mkdir(parents=True)
    subtitle_file.write_bytes(
        b"1\r\n00:00:01,000 --> 00:00:02,500\r\nOla mundo\r\n"
    )
    subtitle = {
        "torrent_path": "filme.mkv",
        "language": "pt-BR",
        "extension": ".srt",
        "subtitle_path": "D:/vault/pt-BR/filme.srt",
        "synced_path": None,
    }
    database = QueueDatabase(FakeResult(rows=[subtitle]))
    install_connection(monkeypatch, database)
    plane = bare_plane(
        subtitle_file_root=mounted,
        subtitle_host_root="D:/vault",
    )
    plane.authenticate = lambda session, token: {
        "site": "filecr",
        "infohash": INFOHASH,
    }
    selected_track = track_id("filecr", INFOHASH, "filme.mkv", "pt-BR")

    payload = plane.subtitle_webvtt("b" * 32, "capability", selected_track)

    assert payload.startswith("WEBVTT\n")
    assert "00:00:01.000 --> 00:00:02.500" in payload
    assert "Ola mundo" in payload


def test_http_routes_keep_security_headers_and_translate_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    transfer_confirmations: list[bool] = []

    class FakePlane:
        def dashboard(self) -> dict[str, int]:
            return {"titles": 1}

        def files(self, **_kwargs: Any) -> dict[str, Any]:
            return {"items": [], "total": 0}

        def transfers(self, **_kwargs: Any) -> dict[str, Any]:
            return {"items": [], "total": 0}

        def create_transfer(self, **kwargs: Any) -> dict[str, Any]:
            confirmed = kwargs.get("confirm_large") is True
            transfer_confirmations.append(confirmed)
            if kwargs.get("file_ids") == [99] and not confirmed:
                raise LargeTransferConfirmationRequired(LARGE_TRANSFER_BYTES + 1)
            return {"id": "job", "state": "queued"}

        def catalog(self, **_kwargs: Any) -> dict[str, Any]:
            return {"items": [], "total": 0}

        def create_playback(self, **kwargs: Any) -> dict[str, str]:
            if kwargs.get("file_id") == 429:
                raise PlaybackCapacityError("capacidade temporaria")
            return {"id": "b" * 32, "token": "capability"}

        def sync_drive(self) -> dict[str, bool]:
            return {"synced": True}

        def subtitle_webvtt(self, _session: str, token: str, _track: str) -> str:
            if token != "ok":
                raise PermissionError("token")
            return "WEBVTT\n\n"

    fake_plane = FakePlane()
    fake_settings = SimpleNamespace(vendor_hls_path=Path("missing"))
    monkeypatch.setattr(
        control.Settings, "from_env", classmethod(lambda cls: fake_settings)
    )
    monkeypatch.setattr(control, "ControlPlane", lambda _settings: fake_plane)
    monkeypatch.setattr(control, "start_heartbeat", lambda *_args, **_kwargs: None)
    client = control.create_app().test_client()

    assert client.get("/api/dashboard").get_json() == {"titles": 1}
    assert client.get("/api/files").status_code == 200
    assert client.get("/api/transfers").status_code == 200
    assert (
        client.post(
            "/api/transfers",
            json={"site": "filecr", "infohash": INFOHASH, "target": "local", "file_ids": [1]},
        ).status_code
        == 201
    )
    confirmation = client.post(
        "/api/transfers",
        json={
            "site": "filecr",
            "infohash": INFOHASH,
            "target": "local",
            "file_ids": [99],
        },
    )
    assert confirmation.status_code == 409
    assert confirmation.get_json()["confirmation_required"] is True
    assert confirmation.get_json()["bytes_total"] == LARGE_TRANSFER_BYTES + 1
    accepted = client.post(
        "/api/transfers",
        json={
            "site": "filecr",
            "infohash": INFOHASH,
            "target": "local",
            "file_ids": [99],
            "confirm_large": True,
        },
    )
    assert accepted.status_code == 201
    assert transfer_confirmations[-2:] == [False, True]
    assert client.post("/api/drive/sync").status_code == 200
    assert client.post(
        "/api/drive/sync", headers={"Origin": "https://attacker.example"}
    ).status_code == 403
    assert client.get("/api/catalog?page=nao-inteiro").status_code == 400
    assert client.post("/api/playback", json={}).status_code == 422
    assert client.post("/api/playback", json=[]).status_code == 422
    playback_capacity = client.post(
        "/api/playback",
        json={
            "site": "filecr",
            "infohash": INFOHASH,
            "file_id": 429,
            "mode": "adaptive",
        },
    )
    assert playback_capacity.status_code == 429
    assert playback_capacity.get_json()["retryable"] is True
    denied = client.get(f"/api/playback/{'b' * 32}/subtitles/{'c' * 24}.vtt")
    allowed = client.get(
        f"/api/playback/{'b' * 32}/subtitles/{'c' * 24}.vtt?token=ok"
    )
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.mimetype == "text/vtt"
    assert "default-src 'self'" in allowed.headers["Content-Security-Policy"]
    assert "worker-src 'self' blob:" in allowed.headers["Content-Security-Policy"]
    assert allowed.headers["Cache-Control"] == "no-store"


def test_playback_ttl_is_applied_and_expired_sessions_are_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    lookup_database = QueueDatabase(FakeResult(row=None))
    install_connection(monkeypatch, lookup_database)
    plane = bare_plane(session_pepper="p" * 32, playback_ttl_seconds=600)
    with pytest.raises(PermissionError):
        plane.authenticate("b" * 32, "invalid")
    assert lookup_database.calls[0][1] == ("b" * 32, 600)

    expired_database = QueueDatabase(
        FakeResult(rows=[{"session_id": "b" * 32, "site": "filecr"}])
    )
    install_connection(monkeypatch, expired_database)
    stopped: list[tuple[str, str]] = []
    plane._stop_workers = lambda session, site: stopped.append((session, site))
    assert plane.expire_sessions() == 1
    assert stopped == [("b" * 32, "filecr")]
    assert expired_database.calls[0][1] == (600,)
    assert any("runtime.download_jobs" in sql for sql, _ in expired_database.calls)
    assert any("runtime.transcode_jobs" in sql for sql, _ in expired_database.calls)


def test_dispatch_failure_marks_job_failed(monkeypatch: pytest.MonkeyPatch):
    database = TransferDatabase(
        [
            {
                "id": 4,
                "path": "file.bin",
                "file_kind": "other",
                "mime_type": "application/octet-stream",
                "size": 1,
            }
        ]
    )
    install_connection(monkeypatch, database)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(control.requests, "post", unavailable)
    with pytest.raises(RuntimeError, match="iniciar a transferencia"):
        bare_plane(torrent_engine_url="http://torrent").create_transfer(
            site="1337x", infohash=INFOHASH, target="local", file_ids=[4]
        )

    assert any("SET state='failed'" in sql for sql, _ in database.calls)


def test_capacity_defers_queued_transfer_without_marking_it_failed(
    monkeypatch: pytest.MonkeyPatch,
):
    database = TransferDatabase(
        [
            {
                "id": 4,
                "path": "file.bin",
                "file_kind": "other",
                "mime_type": "application/octet-stream",
                "size": 1,
            }
        ]
    )
    install_connection(monkeypatch, database)
    monkeypatch.setattr(
        control.requests,
        "post",
        lambda *_args, **_kwargs: OkResponse(status_code=429),
    )

    result = bare_plane(torrent_engine_url="http://torrent").create_transfer(
        site="1337x", infohash=INFOHASH, target="local", file_ids=[4]
    )

    assert result["state"] == "queued"
    assert result["deferred"] is True
    assert not any("SET state='failed'" in sql for sql, _ in database.calls)
    assert any(
        params and "transfer.dispatch_deferred" in params
        for _sql, params in database.calls
    )
