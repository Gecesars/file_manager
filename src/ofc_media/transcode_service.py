from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from flask import Flask, Response, jsonify, request, stream_with_context
from psycopg.types.json import Jsonb

from .auth import internal_token_matches
from .config import Settings
from .db import connection
from .heartbeat import start_heartbeat
from .media import MediaPlan, MediaToolchain
from .safety import UnsafeMediaError, normalized_session_id, safe_owned_path


LOG = logging.getLogger("ofc.transcoder")
CACHE_FORMAT = "hls-mpegts-stereo-v2"
CACHE_COMPLETE = ".complete.json"
DEFAULT_PLAYBACK_TTL_SECONDS = 43_200
LOOPBACK_SOURCE_PROXY = "http://127.0.0.1:7102/internal/source-proxy"
CAPABILITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
PROCESS_TERMINATE_TIMEOUT_SECONDS = 5.0
PROXY_RESPONSE_HEADERS = (
    "Accept-Ranges",
    "Content-Length",
    "Content-Range",
    "Content-Type",
    "ETag",
    "Last-Modified",
)


class TranscodeCapacityError(RuntimeError):
    """Fila de transcode cheia; a solicitacao pode ser repetida depois."""


def read_text_tail(path: Path, max_bytes: int) -> str:
    """Decodifica somente a cauda limitada de um log potencialmente grande."""

    limit = max(1, int(max_bytes))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit), os.SEEK_SET)
        return handle.read(limit).decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class SourceCapability:
    source_base: str
    token: str = field(repr=False)


def is_loopback_remote(value: str | None) -> bool:
    try:
        address = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


class TranscodeManager:
    def __init__(self, settings: Settings) -> None:
        settings.validate_secrets()
        self.settings = settings
        self.settings.hls_root.mkdir(parents=True, exist_ok=True)
        self.cache_root = safe_owned_path(self.settings.hls_root, "cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.media = MediaToolchain(settings.transcode_encoder)
        self.max_transcodes = max(1, int(settings.max_transcodes))
        self.max_transcode_queue = max(
            0, int(getattr(settings, "max_transcode_queue", 1))
        )
        self.log_tail_bytes = max(
            4_096, int(getattr(settings, "ffmpeg_log_tail_bytes", 65_536))
        )
        self.slots = threading.BoundedSemaphore(self.max_transcodes)
        self.admission = threading.BoundedSemaphore(
            self.max_transcodes + self.max_transcode_queue
        )
        self.jobs: dict[str, threading.Thread] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.running_jobs: set[str] = set()
        self.active_keys: dict[str, str] = {}
        self.source_capabilities: dict[str, SourceCapability] = {}
        self.cancelled_sessions: set[str] = set()
        self.lock = threading.RLock()

    def details(self) -> dict[str, Any]:
        with self.lock:
            admitted = sum(thread.is_alive() for thread in self.jobs.values())
            running = len(self.running_jobs)
        capabilities = self.media.capabilities()
        return {
            "active_jobs": running,
            "queued_jobs": max(0, admitted - running),
            "admitted_jobs": admitted,
            "max_transcodes": self.max_transcodes,
            "max_transcode_queue": self.max_transcode_queue,
            "capabilities": capabilities,
        }

    @staticmethod
    def _terminate_and_reap(
        process: subprocess.Popen[bytes],
        terminate_timeout: float = PROCESS_TERMINATE_TIMEOUT_SECONDS,
    ) -> None:
        """Encerra o filho e confirma seu reap antes de devolver capacidade."""

        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(0.0, float(terminate_timeout)))
            return
        except subprocess.TimeoutExpired:
            LOG.warning("FFmpeg nao encerrou no prazo; aplicando kill")
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()

    @contextmanager
    def _transcode_slot(
        self,
        process_getter: Callable[[], subprocess.Popen[bytes] | None],
    ) -> Iterator[None]:
        self.slots.acquire()
        try:
            yield
        finally:
            process = process_getter()
            if process is not None:
                self._terminate_and_reap(process)
            self.slots.release()

    def _claim_cache_key(self, storage_key: str, job_id: str) -> bool:
        with self.lock:
            if storage_key in self.active_keys:
                return False
            self.active_keys[storage_key] = job_id
            return True

    def _release_cache_key(self, storage_key: str, job_id: str) -> None:
        with self.lock:
            if self.active_keys.get(storage_key) == job_id:
                self.active_keys.pop(storage_key, None)

    def start(
        self,
        session_id: str,
        token: str,
        mode: str = "auto",
        quality_cap_bps: int = 0,
    ) -> str:
        session_id = normalized_session_id(session_id)
        if not CAPABILITY_TOKEN_RE.fullmatch(token):
            raise UnsafeMediaError("capability de playback invalida")
        if not self.admission.acquire(blocking=False):
            raise TranscodeCapacityError(
                "capacidade de transcode esgotada; tente novamente depois"
            )
        job_id = uuid.uuid4().hex
        thread = threading.Thread(
            target=self._run,
            args=(job_id, session_id, token, mode, quality_cap_bps, True),
            name=f"transcode-{session_id}",
            daemon=True,
        )
        try:
            with self.lock:
                if session_id in self.jobs and self.jobs[session_id].is_alive():
                    raise RuntimeError("transcodificacao ja ativa")
                self.cancelled_sessions.discard(session_id)
                self.jobs[session_id] = thread
            thread.start()
        except Exception:
            with self.lock:
                if self.jobs.get(session_id) is thread:
                    self.jobs.pop(session_id, None)
            self.admission.release()
            raise
        return job_id

    def _session(self, session_id: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT s.id::text,s.site,trim(s.infohash) AS infohash,s.torrent_file_id,
                       f.path,f.size
                FROM runtime.playback_sessions s
                JOIN catalog.torrent_files f ON f.id=s.torrent_file_id
                WHERE s.id=%s AND s.closed_at IS NULL
                  AND s.created_at >= now()-make_interval(secs => %s)
                """,
                (
                    session_id,
                    max(
                        1,
                        int(
                            getattr(
                                self.settings,
                                "playback_ttl_seconds",
                                DEFAULT_PLAYBACK_TTL_SECONDS,
                            )
                        ),
                    ),
                ),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def _register_source_capability(
        self, session_id: str, source_base: str, token: str
    ) -> str:
        session_id = normalized_session_id(session_id)
        if not CAPABILITY_TOKEN_RE.fullmatch(token):
            raise UnsafeMediaError("capability de playback invalida")
        with self.lock:
            if session_id in self.cancelled_sessions:
                raise RuntimeError("transcodificacao encerrada")
            self.source_capabilities[session_id] = SourceCapability(
                source_base=source_base.rstrip("/"),
                token=token,
            )
        return f"{LOOPBACK_SOURCE_PROXY}/{session_id}"

    def _forget_source_capability(self, session_id: str) -> None:
        with self.lock:
            self.source_capabilities.pop(session_id, None)

    def open_source_proxy(
        self,
        session_id: str,
        *,
        method: str,
        range_header: str | None,
        if_range: str | None,
    ) -> requests.Response:
        session_id = normalized_session_id(session_id)
        selected_method = method.upper()
        if selected_method not in {"GET", "HEAD"}:
            raise UnsafeMediaError("metodo de proxy invalido")
        with self.lock:
            capability = self.source_capabilities.get(session_id)
        if capability is None:
            raise KeyError(session_id)
        headers = {"Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        if if_range:
            headers["If-Range"] = if_range
        source_url = (
            f"{capability.source_base}/source/{session_id}/{capability.token}"
        )
        try:
            upstream = requests.request(
                selected_method,
                source_url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(10, 180),
            )
        except requests.RequestException:
            # Nao encadear a excecao: requests inclui a URL (e portanto a
            # capability) em varias mensagens de erro.
            raise RuntimeError("fonte interna indisponivel") from None
        if 300 <= upstream.status_code < 400:
            upstream.close()
            raise RuntimeError("redirecionamento da fonte interna recusado")
        return upstream

    def _run(
        self,
        job_id: str,
        session_id: str,
        token: str,
        mode: str,
        quality_cap_bps: int,
        admission_owned: bool = False,
    ) -> None:
        process: subprocess.Popen[bytes] | None = None
        owns_active_key = False
        try:
            with self._transcode_slot(lambda: process):
                with self.lock:
                    self.running_jobs.add(session_id)
                item = self._session(session_id)
                source_base = (
                    self.settings.drive_source_url
                    if item["site"] == "gdrive"
                    else self.settings.torrent_engine_url
                )
                source = self._register_source_capability(
                    session_id, source_base, token
                )
                # A unica copia duradoura fica no mapa protegido; argv,
                # fingerprint e ffmpeg.log recebem apenas a URL loopback.
                token = ""
                self._state(session_id, "probing")
                probe = None
                for attempt in range(3):
                    try:
                        probe = self.media.probe(source)
                        break
                    except (UnsafeMediaError, subprocess.TimeoutExpired):
                        if attempt == 2:
                            raise
                        time.sleep(3)
                if probe is None:
                    raise RuntimeError("FFprobe nao retornou dados")
                plan = self.media.plan(
                    probe, mode=mode, quality_cap_bps=max(0, quality_cap_bps)
                )
                storage_key = hashlib.sha256(
                    f"{CACHE_FORMAT}:{item['site']}:{item['infohash']}:{item['torrent_file_id']}:{plan.fingerprint()}".encode()
                ).hexdigest()
                output = safe_owned_path(self.cache_root, storage_key)
                completion_marker = safe_owned_path(output, CACHE_COMPLETE)
                if self.media.ready(output, plan) and completion_marker.is_file():
                    self._register_ready(session_id, storage_key, plan, probe, cache_hit=True)
                    return
                owns_active_key = self._claim_cache_key(storage_key, job_id)
                already_active = not owns_active_key
                if already_active:
                    for _ in range(600):
                        if self.media.ready(output, plan):
                            self._register_ready(session_id, storage_key, plan, probe, cache_hit=True)
                            return
                        time.sleep(0.5)
                    raise TimeoutError("cache compartilhado nao ficou pronto")
                if output.exists():
                    status = output.lstat()
                    if output.is_symlink() or getattr(status, "st_file_attributes", 0) & 0x400:
                        raise UnsafeMediaError("cache parcial e um link")
                    shutil.rmtree(output)
                output.mkdir(parents=True)
                command = self.media.command(
                    source=source, output_root=output, probe=probe, plan=plan
                )
                fingerprint = hashlib.sha256("\0".join(command[1:]).encode()).hexdigest()
                log_path = safe_owned_path(output, "ffmpeg.log")
                with log_path.open("wb") as log:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=log,
                    )
                with self.lock:
                    self.processes[session_id] = process
                with connection(self.settings) as database:
                    database.execute(
                        """
                        UPDATE runtime.transcode_jobs SET strategy=%s,encoder=%s,profiles=%s,
                          state='transcoding',process_id=%s,command_fingerprint=%s,updated_at=now()
                        WHERE session_id=%s
                        """,
                        (
                            plan.strategy,
                            plan.encoder,
                            Jsonb([item.name for item in plan.renditions]),
                            process.pid,
                            fingerprint,
                            session_id,
                        ),
                    )
                    database.commit()
                for _ in range(1200):
                    if self.media.ready(output, plan):
                        self._register_ready(session_id, storage_key, plan, probe, cache_hit=False)
                        break
                    if process.poll() is not None:
                        error = read_text_tail(log_path, self.log_tail_bytes)[-3000:]
                        raise RuntimeError(error or "FFmpeg encerrou antes do manifesto")
                    time.sleep(0.5)
                else:
                    raise TimeoutError("manifesto HLS nao ficou pronto")
                return_code = process.wait()
                log_text = read_text_tail(log_path, self.log_tail_bytes)
                premature = any(
                    marker in log_text.casefold()
                    for marker in (
                        "stream ends prematurely",
                        "error during demuxing",
                        "input/output error",
                        "leitura incompleta",
                    )
                )
                complete = return_code == 0 and not premature
                if complete:
                    temporary_marker = safe_owned_path(output, f"{CACHE_COMPLETE}.tmp")
                    temporary_marker.write_text(
                        json.dumps({"format": CACHE_FORMAT, "completed_at": time.time()}),
                        encoding="utf-8",
                    )
                    temporary_marker.replace(completion_marker)
                with connection(self.settings) as database:
                    database.execute(
                        """
                        UPDATE runtime.transcode_jobs SET state=%s,error=%s,finished_at=now(),updated_at=now()
                        WHERE session_id=%s
                        """,
                        (
                            "complete" if complete else "ready_partial",
                            None if complete else (
                                "fonte terminou antes do arquivo completo"
                                if premature
                                else f"FFmpeg exit {return_code}"
                            ),
                            session_id,
                        ),
                    )
                    database.commit()
        except Exception as exc:
            LOG.exception("transcode %s falhou", session_id)
            self._state(session_id, "error", f"{type(exc).__name__}: {exc}")
            with connection(self.settings) as database:
                database.execute(
                    """
                    UPDATE runtime.transcode_jobs SET state='error',error=%s,
                      finished_at=now(),updated_at=now() WHERE session_id=%s
                    """,
                    (f"{type(exc).__name__}: {exc}", session_id),
                )
                database.commit()
        finally:
            with self.lock:
                self.processes.pop(session_id, None)
                self.running_jobs.discard(session_id)
                self.source_capabilities.pop(session_id, None)
                self.cancelled_sessions.discard(session_id)
                current = self.jobs.get(session_id)
                if current is threading.current_thread():
                    self.jobs.pop(session_id, None)
            if owns_active_key and "storage_key" in locals():
                self._release_cache_key(storage_key, job_id)
            if admission_owned:
                self.admission.release()

    def _register_ready(
        self,
        session_id: str,
        storage_key: str,
        plan: MediaPlan,
        probe: dict[str, Any],
        *,
        cache_hit: bool,
    ) -> None:
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE runtime.playback_sessions SET state='ready',strategy=%s,
                  source_bitrate=%s,target_bitrate=%s,media_probe=%s,error=NULL,updated_at=now()
                WHERE id=%s
                """,
                (
                    plan.strategy,
                    plan.source_bitrate,
                    max(item.bitrate for item in plan.renditions),
                    Jsonb(probe),
                    session_id,
                ),
            )
            database.execute(
                """
                INSERT INTO runtime.stream_artifacts(session_id,storage_key,profile,kind,
                  relative_path,ready)
                VALUES(%s,%s,'master','hls','master.m3u8',TRUE)
                ON CONFLICT(session_id,relative_path) DO UPDATE SET
                  storage_key=excluded.storage_key,ready=TRUE,updated_at=now()
                """,
                (session_id, storage_key),
            )
            database.execute(
                """
                UPDATE runtime.transcode_jobs SET strategy=%s,encoder=%s,profiles=%s,
                  state='ready',error=NULL,updated_at=now() WHERE session_id=%s
                """,
                (
                    plan.strategy,
                    plan.encoder,
                    Jsonb([item.name for item in plan.renditions]),
                    session_id,
                ),
            )
            database.commit()
        LOG.info("sessao %s pronta (%s, cache=%s)", session_id, plan.strategy, cache_hit)

    def _state(self, session_id: str, state: str, error: str | None = None) -> None:
        with connection(self.settings) as database:
            database.execute(
                "UPDATE runtime.playback_sessions SET state=%s,error=%s,updated_at=now() WHERE id=%s AND closed_at IS NULL",
                (state, error, session_id),
            )
            database.commit()

    def close(self, session_id: str) -> None:
        session_id = normalized_session_id(session_id)
        with self.lock:
            thread = self.jobs.get(session_id)
            if thread is not None and thread.is_alive():
                self.cancelled_sessions.add(session_id)
            self.source_capabilities.pop(session_id, None)
            process = self.processes.get(session_id)
        if process is not None:
            self._terminate_and_reap(process)


def _internal(settings: Settings) -> bool:
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return internal_token_matches(supplied, settings.internal_token)


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    manager = TranscodeManager(settings)
    start_heartbeat("transcoder", manager.details)
    app = Flask(__name__)
    app.config["manager"] = manager

    @app.get("/health")
    def health() -> Response:
        details = manager.details()
        return jsonify({"status": "ok", **details})

    @app.post("/internal/transcodes")
    def create_transcode() -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        payload = request.get_json(silent=True) or {}
        try:
            job_id = manager.start(
                str(payload.get("session_id", "")),
                str(payload.get("token", "")),
                str(payload.get("mode", "auto")),
                int(payload.get("quality_cap_bps") or 0),
            )
        except TranscodeCapacityError as exc:
            return jsonify({"error": str(exc), "retryable": True}), 429
        except (UnsafeMediaError, ValueError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify({"job_id": job_id}), 202

    @app.delete("/internal/transcodes/<session_id>")
    def close_transcode(session_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            manager.close(session_id)
        except UnsafeMediaError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify({"closed": True})

    @app.route(
        "/internal/source-proxy/<session_id>", methods=["GET", "HEAD"]
    )
    def source_proxy(session_id: str) -> Response:
        if not is_loopback_remote(request.remote_addr):
            return jsonify({"error": "proxy disponivel somente em loopback"}), 403
        try:
            upstream = manager.open_source_proxy(
                session_id,
                method=request.method,
                range_header=request.headers.get("Range"),
                if_range=request.headers.get("If-Range"),
            )
        except (KeyError, UnsafeMediaError):
            return jsonify({"error": "fonte proxy ausente"}), 404
        except RuntimeError:
            return jsonify({"error": "fonte interna indisponivel"}), 502
        headers = {
            name: upstream.headers[name]
            for name in PROXY_RESPONSE_HEADERS
            if name in upstream.headers
        }
        if request.method == "HEAD":
            upstream.close()
            return Response(status=upstream.status_code, headers=headers)

        def body() -> Iterator[bytes]:
            try:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(body()),
            status=upstream.status_code,
            headers=headers,
        )

    return app


def main() -> None:
    create_app().run(host="0.0.0.0", port=7102, threaded=True)


if __name__ == "__main__":
    main()
