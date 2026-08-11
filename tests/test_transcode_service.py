from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ofc_media import transcode_service
from ofc_media.media import MediaPlan, Rendition
from ofc_media.transcode_service import (
    TranscodeCapacityError,
    TranscodeManager,
    read_text_tail,
)


SESSION_ID = "b" * 32
CAPABILITY = "S" * 48


def manager_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        validate_secrets=lambda: None,
        hls_root=tmp_path / "hls",
        transcode_encoder="auto",
        max_transcodes=1,
        max_transcode_queue=1,
        ffmpeg_log_tail_bytes=4096,
        drive_source_url="http://drive:7103",
        torrent_engine_url="http://torrent:7101",
        internal_token="I" * 32,
        vendor_hls_path=tmp_path / "missing.mjs",
    )


class FakeMedia:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.commands: list[list[str]] = []
        self.ready_calls = 0
        self.selected_plan = MediaPlan(
            strategy="remux",
            encoder="copy",
            renditions=(Rendition("720p", 720, 3_000_000),),
            video_copy=True,
            audio_copy=True,
            source_bitrate=3_000_000,
        )

    def capabilities(self) -> dict[str, object]:
        return {"selected_encoder": "copy"}

    def probe(self, source: str) -> dict[str, object]:
        self.sources.append(source)
        return {"format": {"bit_rate": "3000000"}, "streams": []}

    def plan(self, *_args: object, **_kwargs: object) -> MediaPlan:
        return self.selected_plan

    def ready(self, _root: Path, _plan: MediaPlan) -> bool:
        self.ready_calls += 1
        return self.ready_calls >= 2

    def command(
        self, *, source: str, output_root: Path, probe: object, plan: MediaPlan
    ) -> list[str]:
        command = ["ffmpeg", "-i", source, "-f", "hls", str(output_root / "master.m3u8")]
        self.commands.append(command)
        return command


class RecordingDatabase:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> SimpleNamespace:
        self.calls.append((sql, params))
        return SimpleNamespace(fetchone=lambda: self.row)

    def commit(self) -> None:
        return None


def install_database(
    monkeypatch: pytest.MonkeyPatch, database: RecordingDatabase
) -> None:
    @contextmanager
    def fake_connection(_settings: object):
        yield database

    monkeypatch.setattr(transcode_service, "connection", fake_connection)


def test_transcode_argv_fingerprint_and_log_never_contain_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manager = TranscodeManager(manager_settings(tmp_path))
    media = FakeMedia()
    manager.media = media
    manager._session = lambda _session_id: {
        "site": "filecr",
        "infohash": "a" * 40,
        "torrent_file_id": 7,
    }
    manager._register_ready = lambda *_args, **_kwargs: None
    manager._state = lambda *_args, **_kwargs: None
    database = RecordingDatabase()
    install_database(monkeypatch, database)
    popen_calls: list[list[str]] = []

    class FakeProcess:
        pid = 321

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        popen_calls.append(list(command))
        log = kwargs["stderr"]
        log.write(f"Input source: {command[2]}\n".encode())
        log.flush()
        return FakeProcess()

    monkeypatch.setattr(transcode_service.subprocess, "Popen", fake_popen)

    manager._run("job", SESSION_ID, CAPABILITY, "auto", 0)

    command = popen_calls[0]
    command_text = "\0".join(command)
    proxy_url = f"http://127.0.0.1:7102/internal/source-proxy/{SESSION_ID}"
    assert command[2] == proxy_url
    assert CAPABILITY not in command_text
    assert media.sources == [proxy_url]
    fingerprint_call = next(
        params for sql, params in database.calls if "command_fingerprint" in sql
    )
    expected_fingerprint = hashlib.sha256(
        "\0".join(command[1:]).encode()
    ).hexdigest()
    assert fingerprint_call[-2] == expected_fingerprint
    assert CAPABILITY not in str(fingerprint_call[-2])
    log_text = next((tmp_path / "hls" / "cache").glob("*/ffmpeg.log")).read_text()
    assert proxy_url in log_text
    assert CAPABILITY not in log_text
    assert SESSION_ID not in manager.source_capabilities


def test_loopback_proxy_forwards_range_and_forgets_capability_on_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    settings = manager_settings(tmp_path)
    manager = TranscodeManager(settings)
    manager._register_source_capability(
        SESSION_ID, settings.torrent_engine_url, CAPABILITY
    )
    upstream_calls: list[dict[str, Any]] = []

    class FakeUpstream:
        status_code = 206
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": "bytes 4-6/10",
            "Content-Length": "3",
            "Content-Type": "video/mp4",
        }

        def __init__(self) -> None:
            self.closed = False

        def iter_content(self, chunk_size: int):
            assert chunk_size == 256 * 1024
            yield b"abc"

        def close(self) -> None:
            self.closed = True

    upstream = FakeUpstream()

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeUpstream:
        upstream_calls.append({"method": method, "url": url, **kwargs})
        return upstream

    monkeypatch.setattr(transcode_service.requests, "request", fake_request)
    monkeypatch.setattr(
        transcode_service.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(transcode_service, "TranscodeManager", lambda _settings: manager)
    monkeypatch.setattr(transcode_service, "start_heartbeat", lambda *_args, **_kwargs: None)
    client = transcode_service.create_app().test_client()
    path = f"/internal/source-proxy/{SESSION_ID}"

    denied = client.get(
        path,
        headers={"Range": "bytes=4-6"},
        environ_overrides={"REMOTE_ADDR": "10.1.2.3"},
    )
    assert denied.status_code == 403
    assert upstream_calls == []

    response = client.get(
        path,
        headers={"Range": "bytes=4-6", "If-Range": '"version"'},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 206
    assert response.data == b"abc"
    assert response.headers["Content-Range"] == "bytes 4-6/10"
    assert upstream_calls[0]["headers"] == {
        "Accept-Encoding": "identity",
        "Range": "bytes=4-6",
        "If-Range": '"version"',
    }
    assert upstream_calls[0]["url"].endswith(
        f"/source/{SESSION_ID}/{CAPABILITY}"
    )
    assert upstream.closed is True

    manager.close(SESSION_ID)
    assert SESSION_ID not in manager.source_capabilities
    assert client.get(path).status_code == 404


def test_transcode_lookup_enforces_default_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manager = TranscodeManager(manager_settings(tmp_path))
    database = RecordingDatabase({"site": "filecr", "id": SESSION_ID})
    install_database(monkeypatch, database)

    assert manager._session(SESSION_ID)["site"] == "filecr"

    sql, params = database.calls[0]
    assert "created_at >= now()-make_interval(secs => %s)" in sql
    assert params == (SESSION_ID, 43_200)


def test_loopback_address_validation_accepts_only_local_interfaces():
    assert transcode_service.is_loopback_remote("127.0.0.1")
    assert transcode_service.is_loopback_remote("::1")
    assert transcode_service.is_loopback_remote("::ffff:127.0.0.1")
    assert not transcode_service.is_loopback_remote("10.0.0.1")
    assert not transcode_service.is_loopback_remote(None)


def test_transcode_admission_bounds_waiting_threads_and_returns_429(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    settings = manager_settings(tmp_path)
    manager = TranscodeManager(settings)
    created: list[object] = []

    class ParkedThread:
        def __init__(self, **_kwargs: object) -> None:
            self.started = False
            created.append(self)

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return self.started

    monkeypatch.setattr(transcode_service.threading, "Thread", ParkedThread)
    manager.start("a" * 32, CAPABILITY)
    manager.start("b" * 32, CAPABILITY)

    with pytest.raises(TranscodeCapacityError, match="capacidade"):
        manager.start("c" * 32, CAPABILITY)

    assert len(created) == 2
    monkeypatch.setattr(
        transcode_service.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(transcode_service, "TranscodeManager", lambda _settings: manager)
    monkeypatch.setattr(transcode_service, "start_heartbeat", lambda *_args: None)
    client = transcode_service.create_app().test_client()
    response = client.post(
        "/internal/transcodes",
        json={"session_id": "c" * 32, "token": CAPABILITY},
        headers={"Authorization": f"Bearer {settings.internal_token}"},
    )
    assert response.status_code == 429
    assert response.get_json()["retryable"] is True


def test_active_cache_key_is_released_only_by_its_owner(tmp_path: Path):
    manager = TranscodeManager(manager_settings(tmp_path))

    assert manager._claim_cache_key("cache", "owner") is True
    assert manager._claim_cache_key("cache", "waiter") is False
    manager._release_cache_key("cache", "waiter")
    assert manager.active_keys == {"cache": "owner"}
    manager._release_cache_key("cache", "owner")
    assert manager.active_keys == {}


def test_ffmpeg_log_reader_reads_only_bounded_tail(tmp_path: Path):
    log_path = tmp_path / "ffmpeg.log"
    log_path.write_bytes(b"prefix-secret\n" + b"x" * 10_000 + b"tail-marker")

    tail = read_text_tail(log_path, 64)

    assert len(tail.encode()) <= 64
    assert tail.endswith("tail-marker")
    assert "prefix-secret" not in tail


def test_transcode_slot_reaps_stubborn_process_before_releasing_capacity(
    tmp_path: Path,
):
    manager = TranscodeManager(manager_settings(tmp_path))
    events: list[str] = []

    class TrackingSlots:
        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    class StubbornProcess:
        def poll(self) -> None:
            events.append("poll")
            return None

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            if timeout is not None:
                raise transcode_service.subprocess.TimeoutExpired("ffmpeg", timeout)
            return -9

        def kill(self) -> None:
            events.append("kill")

    manager.slots = TrackingSlots()  # type: ignore[assignment]
    process = StubbornProcess()

    with pytest.raises(RuntimeError, match="falha simulada"):
        with manager._transcode_slot(lambda: process):  # type: ignore[arg-type]
            events.append("body")
            raise RuntimeError("falha simulada")

    assert events == [
        "acquire",
        "body",
        "poll",
        "terminate",
        "wait:5.0",
        "kill",
        "wait:None",
        "release",
    ]


def test_details_probes_capabilities_outside_manager_lock(tmp_path: Path):
    manager = TranscodeManager(manager_settings(tmp_path))

    class TrackingLock:
        held = False

        def __enter__(self) -> None:
            assert self.held is False
            self.held = True

        def __exit__(self, *_args: object) -> None:
            self.held = False

    class CapabilityProbe:
        def capabilities(self) -> dict[str, str]:
            assert lock.held is False
            return {"selected_encoder": "copy"}

    lock = TrackingLock()
    manager.lock = lock  # type: ignore[assignment]
    manager.media = CapabilityProbe()  # type: ignore[assignment]

    details = manager.details()

    assert details["capabilities"] == {"selected_encoder": "copy"}
