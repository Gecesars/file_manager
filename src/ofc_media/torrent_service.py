from __future__ import annotations

import atexit
import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from flask import Flask, Response, jsonify, request, stream_with_context
from psycopg.types.json import Jsonb
from redis import Redis

from .auth import internal_token_matches, token_matches
from .config import Settings
from .db import connection
from .heartbeat import start_heartbeat
from .safety import (
    UnsafeMediaError,
    decode_metainfo,
    has_video_signature,
    is_video_name,
    metainfo_files,
    normalized_infohash,
    normalized_session_id,
    safe_owned_path,
    safe_relative_path,
)


LOG = logging.getLogger("ofc.torrent_engine")
DEFAULT_PLAYBACK_TTL_SECONDS = 43_200
MATERIALIZATION_RECOVERY_INTERVAL = 5.0
LOCAL_COMPLETION_RECOVERY_BATCH_SIZE = 32
MATERIALIZATION_STATES = ("queued", "validating", "downloading")
MATERIALIZATION_PROGRESS_STATES = ("validating", "downloading")
CANCELLABLE_TRANSFER_STATES = (
    "queued",
    "validating",
    "downloading",
    "downloaded",
    "classifying",
    "uploading",
    "verifying",
)
TERMINAL_TRANSFER_STATES = {"completed", "failed", "cancelled"}


class TorrentCapacityError(RuntimeError):
    """Capacidade transitória esgotada; o chamador pode tentar novamente."""


def _normalized_job_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise UnsafeMediaError("job invalido") from exc


def _relative_to_classified_root(relative_path: str, destination_path: str) -> str:
    """Remove somente a raiz ja representada, sem achatar a subarvore."""

    relative = safe_relative_path(relative_path).split("/")
    destination = safe_relative_path(destination_path).split("/")
    folded_relative = [unicodedata.normalize("NFKC", part).casefold() for part in relative]
    folded_destination = [
        unicodedata.normalize("NFKC", part).casefold() for part in destination
    ]
    maximum = min(len(folded_destination), max(0, len(folded_relative) - 1))
    for length in range(maximum, 0, -1):
        if folded_destination[-length:] == folded_relative[:length]:
            return "/".join(relative[length:])
    return "/".join(relative)


@dataclass(slots=True)
class SharedDownload:
    key: str
    site: str
    infohash: str
    handle: Any
    torrent_info: Any
    save_root: Path
    piece_length: int
    sessions: set[str] = field(default_factory=set)
    materializations: set[str] = field(default_factory=set)
    selected_indices: set[int] = field(default_factory=set)


@dataclass(slots=True)
class StreamSession:
    id: str
    download_key: str
    file_index: int
    file_size: int
    file_offset: int
    relative_path: str
    file_path: Path


@dataclass(frozen=True, slots=True)
class MaterializedFile:
    catalog_file_id: int
    file_index: int
    relative_path: str
    file_size: int
    file_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "file_id": self.catalog_file_id,
            "file_index": self.file_index,
            "path": self.relative_path,
            "size": self.file_size,
            "local_path": str(self.file_path),
        }


@dataclass(frozen=True, slots=True)
class Materialization:
    id: str
    download_key: str
    target: str
    files: tuple[MaterializedFile, ...]
    bytes_total: int
    destination_path: str = ""


class TorrentEngine:
    def __init__(self, settings: Settings) -> None:
        settings.validate_secrets()
        self.settings = settings
        self.settings.media_root.mkdir(parents=True, exist_ok=True)
        self.settings.resume_root.mkdir(parents=True, exist_ok=True)
        try:
            import libtorrent as lt  # type: ignore
        except ImportError as exc:
            raise RuntimeError("python3-libtorrent indisponivel") from exc
        self.lt = lt
        self.client = Redis.from_url(settings.redis_url, decode_responses=True)
        engine_settings = {
            "listen_interfaces": "0.0.0.0:0",
            "enable_upnp": False,
            "enable_natpmp": False,
            "announce_to_all_trackers": False,
            "announce_to_all_tiers": False,
            "alert_mask": int(lt.alert.category_t.status_notification | lt.alert.category_t.storage_notification),
        }
        self.engine = lt.session(engine_settings)
        self.downloads: dict[str, SharedDownload] = {}
        self.sessions: dict[str, StreamSession] = {}
        self.materializations: dict[str, Materialization] = {}
        self.lock = threading.RLock()
        self.max_active_torrents = max(
            1, int(getattr(settings, "max_active_torrents", 2))
        )
        self.materialization_admissions: dict[str, str] = {}
        self.stop = threading.Event()
        self.monitor = threading.Thread(target=self._monitor, name="torrent-monitor", daemon=True)
        self.recovery = threading.Thread(
            target=self._recovery_loop,
            name="torrent-materialization-recovery",
            daemon=True,
        )
        self.monitor.start()
        self.recovery.start()

    def _torrent_root(self, site: str) -> Path:
        if site == "filecr":
            return self.settings.filecr_torrent_root
        if site == "1337x":
            return self.settings.x1337_torrent_root
        raise UnsafeMediaError("site invalido")

    def _capacity_limit(self) -> int:
        """Retorna o limite inclusive para instancias legadas criadas via __new__."""

        configured = getattr(self, "max_active_torrents", None)
        if configured is None:
            configured = getattr(self.settings, "max_active_torrents", 2)
        return max(1, int(configured))

    def _materialization_admissions_locked(self) -> dict[str, str]:
        """Inicializa o estado novo de forma preguiçosa para fixtures legadas."""

        admissions = getattr(self, "materialization_admissions", None)
        if admissions is None:
            admissions = {}
            self.materialization_admissions = admissions
        return admissions

    def _effective_download_keys_locked(self) -> set[str]:
        admissions = self._materialization_admissions_locked()
        return set(self.downloads) | set(admissions.values())

    def _ensure_download_capacity_locked(self, key: str) -> None:
        effective = self._effective_download_keys_locked()
        if key not in effective and len(effective) >= self._capacity_limit():
            raise TorrentCapacityError(
                "capacidade de torrents ativos esgotada; tente novamente depois"
            )

    def _admit_materialization(self, job_id: str, key: str) -> None:
        with self.lock:
            admissions = self._materialization_admissions_locked()
            if job_id in admissions:
                raise TorrentCapacityError("materializacao ja esta sendo admitida")
            if (
                len(self.materializations) + len(admissions)
                >= self._capacity_limit()
            ):
                raise TorrentCapacityError(
                    "capacidade de materializacoes esgotada; tente novamente depois"
                )
            self._ensure_download_capacity_locked(key)
            admissions[job_id] = key

    def _release_materialization_admission(self, job_id: str) -> None:
        with self.lock:
            self._materialization_admissions_locked().pop(job_id, None)

    def capacity_details(self) -> dict[str, int]:
        with self.lock:
            effective = self._effective_download_keys_locked()
            admitted = len(self._materialization_admissions_locked())
            active_materializations = len(self.materializations)
            capacity_limit = self._capacity_limit()
            return {
                "sessions": len(self.sessions),
                "active_torrents": len(self.downloads),
                "reserved_torrents": max(0, len(effective) - len(self.downloads)),
                "materializations": active_materializations,
                "materialization_admissions": admitted,
                "max_active_torrents": capacity_limit,
                "available_materialization_slots": max(
                    0,
                    capacity_limit - active_materializations - admitted,
                ),
            }

    def _lookup(self, session_id: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT s.id::text,s.site,trim(s.infohash) AS infohash,s.token_hash,
                       f.id AS torrent_file_id,f.path,f.size,f.is_video,
                       t.metainfo_relpath
                FROM runtime.playback_sessions s
                JOIN catalog.torrent_files f ON f.id=s.torrent_file_id
                JOIN catalog.torrents t ON t.site=s.site AND t.infohash=s.infohash
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

    def _lookup_transfer_job(
        self, job_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT j.id::text AS id,j.source_site,trim(j.infohash) AS infohash,
                       j.target,j.state,j.selected_file_ids,j.bytes_total,j.bytes_done,
                       j.local_files,j.error,j.destination_path,
                       t.id AS torrent_id,t.metainfo_relpath
                FROM runtime.transfer_jobs j
                JOIN catalog.torrents t
                  ON t.site=j.source_site AND t.infohash=j.infohash
                WHERE j.id=%s AND t.active
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = dict(row)
            raw_ids = job.get("selected_file_ids") or []
            try:
                selected_ids = [int(value) for value in raw_ids]
            except (TypeError, ValueError) as exc:
                raise UnsafeMediaError("arquivos selecionados invalidos") from exc
            if not selected_ids or len(selected_ids) != len(set(selected_ids)):
                raise UnsafeMediaError("selecao de arquivos vazia ou duplicada")
            rows = database.execute(
                """
                SELECT id,path,size FROM catalog.torrent_files
                WHERE torrent_id=%s AND id=ANY(%s::bigint[])
                """,
                (job["torrent_id"], selected_ids),
            ).fetchall()
        by_id = {int(item["id"]): dict(item) for item in rows}
        if set(by_id) != set(selected_ids):
            raise UnsafeMediaError("arquivo selecionado nao pertence ao torrent")
        job["selected_file_ids"] = selected_ids
        return job, [by_id[file_id] for file_id in selected_ids]

    def _read_transfer_status(self, job_id: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT id::text AS id,source_site,trim(infohash) AS infohash,target,
                       state,selected_file_ids,bytes_total,bytes_done,local_files,
                       destination_path,error,updated_at
                FROM runtime.transfer_jobs WHERE id=%s
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["id"] = str(result["id"])
        result["infohash"] = str(result["infohash"]).strip().casefold()
        result["selected_file_ids"] = [
            int(value) for value in (result.get("selected_file_ids") or [])
        ]
        result["bytes_total"] = int(result.get("bytes_total") or 0)
        result["bytes_done"] = int(result.get("bytes_done") or 0)
        result["local_files"] = list(result.get("local_files") or [])
        updated_at = result.get("updated_at")
        if hasattr(updated_at, "isoformat"):
            result["updated_at"] = updated_at.isoformat()
        return result

    def _recoverable_materialization_ids(
        self,
        limit: int,
        exclude_ids: Sequence[str] = (),
    ) -> list[str]:
        """Lista jobs torrent interrompidos sem reivindicar ou alterar estado.

        ``materialize`` faz a validacao e possui a secao critica idempotente. A
        consulta deliberadamente inclui apenas torrents ainda ativos e retorna
        somente UUIDs, mantendo esta varredura barata mesmo com manifests
        grandes persistidos no job.
        """

        with connection(self.settings) as database:
            rows = database.execute(
                """
                SELECT j.id::text AS id
                FROM runtime.transfer_jobs j
                JOIN catalog.torrents t
                  ON t.site=j.source_site AND t.infohash=j.infohash AND t.active
                WHERE j.source_site IN ('filecr','1337x')
                  AND j.state IN ('queued','validating','downloading')
                  AND NOT (j.id=ANY(%s::uuid[]))
                ORDER BY j.updated_at,j.id
                LIMIT %s
                """,
                (list(exclude_ids), max(1, int(limit))),
            ).fetchall()
        return [_normalized_job_id(str(row["id"])) for row in rows]

    def _recoverable_local_completion_ids(
        self,
        limit: int = LOCAL_COMPLETION_RECOVERY_BATCH_SIZE,
        exclude_ids: Sequence[str] = (),
    ) -> list[str]:
        with connection(self.settings) as database:
            rows = database.execute(
                """
                SELECT id::text AS id
                FROM runtime.transfer_jobs
                WHERE source_site IN ('filecr','1337x') AND target='local'
                  AND state IN ('downloaded','classifying','verifying')
                  AND NOT (id=ANY(%s::uuid[]))
                ORDER BY updated_at,id
                LIMIT %s
                """,
                (list(exclude_ids), max(1, int(limit))),
            ).fetchall()
        return [_normalized_job_id(str(row["id"])) for row in rows]

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        return path.is_symlink() or bool(
            getattr(path, "is_junction", lambda: False)()
        )

    def _validated_local_completion(
        self, status: dict[str, Any]
    ) -> tuple[int, list[dict[str, object]]]:
        manifest = status.get("local_files")
        if not isinstance(manifest, list) or not manifest:
            raise UnsafeMediaError("job local sem manifesto de arquivos")
        try:
            selected_ids = [int(value) for value in status["selected_file_ids"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsafeMediaError("selecao local persistida invalida") from exc
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise UnsafeMediaError("selecao local persistida invalida")

        try:
            root = self.settings.media_root.resolve(strict=True)
        except OSError as exc:
            raise UnsafeMediaError("raiz de midia local indisponivel") from exc
        validated: list[dict[str, object]] = []
        actual_ids: list[int] = []
        seen_paths: set[Path] = set()
        total = 0
        for raw_item in manifest:
            if not isinstance(raw_item, dict):
                raise UnsafeMediaError("manifesto local invalido")
            item = dict(raw_item)
            try:
                file_id = int(item["file_id"])
                expected_size = int(item["size"])
                candidate = Path(str(item["local_path"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise UnsafeMediaError("entrada do manifesto local invalida") from exc
            if file_id <= 0 or expected_size < 0 or not candidate.is_absolute():
                raise UnsafeMediaError("entrada do manifesto local invalida")
            try:
                relative = candidate.relative_to(root)
            except ValueError as exc:
                raise UnsafeMediaError("arquivo local fora da raiz autorizada") from exc
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise UnsafeMediaError("caminho local persistido invalido")
            current = root
            try:
                for component in relative.parts:
                    current = current / component
                    if self._is_link_or_junction(current):
                        raise UnsafeMediaError(
                            "links nao sao aceitos no manifesto local"
                        )
                selected = candidate.resolve(strict=True)
                selected.relative_to(root)
                stat = selected.stat()
            except UnsafeMediaError:
                raise
            except (OSError, ValueError) as exc:
                raise UnsafeMediaError("arquivo local indisponivel") from exc
            if not selected.is_file() or stat.st_size != expected_size:
                raise UnsafeMediaError("tamanho do arquivo local diverge do manifesto")
            if selected in seen_paths:
                raise UnsafeMediaError("manifesto local possui caminho duplicado")
            if item.get("complete") is False:
                raise UnsafeMediaError("manifesto local registra arquivo incompleto")
            seen_paths.add(selected)
            actual_ids.append(file_id)
            total += expected_size
            validated.append(item)

        if set(actual_ids) != set(selected_ids) or len(actual_ids) != len(selected_ids):
            raise UnsafeMediaError("manifesto local nao corresponde a selecao")
        if int(status.get("bytes_total") or 0) != total or int(
            status.get("bytes_done") or 0
        ) != total:
            raise UnsafeMediaError("contadores do job local divergem do manifesto")
        return total, validated

    def _recover_local_completion(self, job_id: str) -> bool:
        status = self._read_transfer_status(job_id)
        state = str(status.get("state") or "").casefold()
        if (
            status.get("target") != "local"
            or status.get("source_site") not in {"filecr", "1337x"}
            or state not in {"downloaded", "classifying", "verifying"}
        ):
            return False
        try:
            total, local_files = self._validated_local_completion(status)
        except (OSError, TypeError, ValueError, UnsafeMediaError) as exc:
            self._write_transfer_error(job_id, exc)
            return False

        current_state = state
        if current_state == "downloaded":
            claimed = self._write_transfer_progress(
                job_id,
                state="classifying",
                bytes_total=total,
                bytes_done=total,
                local_files=local_files,
                error=None,
                expected_states=("downloaded",),
            )
            if claimed is False:
                return False
            current_state = "classifying"

        try:
            item = self._local_completion_materialization(status, local_files)
            local_files = self._publish_local_files(item, local_files)
        except (TypeError, ValueError, UnsafeMediaError) as exc:
            self._write_transfer_error(job_id, exc)
            return False
        recorded = self._write_transfer_progress(
            job_id,
            state=current_state,
            bytes_total=total,
            bytes_done=total,
            local_files=local_files,
            error=None,
            expected_states=(current_state,),
        )
        if recorded is False:
            return False

        transitions = {
            "classifying": ("verifying", "completed"),
            "verifying": ("completed",),
        }[current_state]
        for next_state in transitions:
            updated = self._write_transfer_progress(
                job_id,
                state=next_state,
                bytes_total=total,
                bytes_done=total,
                local_files=local_files,
                error=None,
                expected_states=(current_state,),
            )
            # Outro worker (ou um cancelamento) venceu o lock da linha. Nao
            # tente forcar a proxima transicao com uma leitura ja obsoleta.
            if updated is False:
                return False
            current_state = next_state
        return True

    @staticmethod
    def _local_completion_materialization(
        status: dict[str, Any],
        local_files: Sequence[dict[str, object]],
    ) -> Materialization:
        source_site = str(status.get("source_site") or "").strip().casefold()
        infohash = normalized_infohash(str(status.get("infohash") or ""))
        destination_path = safe_relative_path(
            str(status.get("destination_path") or "")
        )
        files: list[MaterializedFile] = []
        for ordinal, manifest in enumerate(local_files):
            try:
                file_id = int(manifest["file_id"])
                file_size = int(manifest["size"])
                relative_path = safe_relative_path(
                    str(manifest.get("relative_path") or manifest["path"])
                )
                persisted_source = manifest.get("source_path")
                source_path = Path(str(persisted_source)) if persisted_source else None
                if source_path is None or not source_path.exists():
                    source_path = Path(str(manifest["local_path"]))
                file_index = int(manifest.get("file_index") or ordinal)
            except (KeyError, TypeError, ValueError) as exc:
                raise UnsafeMediaError(
                    "manifesto local insuficiente para classificacao"
                ) from exc
            files.append(
                MaterializedFile(
                    catalog_file_id=file_id,
                    file_index=file_index,
                    relative_path=relative_path,
                    file_size=file_size,
                    file_path=source_path,
                )
            )
        return Materialization(
            id=str(status.get("id") or ""),
            download_key=f"{source_site}-{infohash}",
            target="local",
            files=tuple(files),
            bytes_total=int(status.get("bytes_total") or 0),
            destination_path=destination_path,
        )

    def _recover_materializations_once(self) -> int:
        recovered = 0
        with self.lock:
            excluded = tuple(
                set(self.materializations)
                | set(self._materialization_admissions_locked())
            )
            available_slots = max(
                0,
                self._capacity_limit()
                - len(self.materializations)
                - len(self._materialization_admissions_locked()),
            )
        recoverable_ids = (
            self._recoverable_materialization_ids(available_slots, excluded)
            if available_slots
            else []
        )
        for job_id in recoverable_ids:
            if self.stop.is_set():
                break
            with self.lock:
                if job_id in self.materializations:
                    continue
            try:
                # _add_download reaplica o .fastresume existente e materialize
                # impede um segundo handle quando o POST inicial corre em paralelo.
                item = self.materialize(job_id)
                recovered += int(item is not None)
            except TorrentCapacityError:
                # Capacidade cheia e um estado esperado. Jobs permanecem na
                # fila e o proximo ciclo recomeca pelo mais antigo.
                break
            except (KeyError, OSError, RuntimeError, UnsafeMediaError) as exc:
                LOG.warning(
                    "materializacao %s nao recuperada (%s); novo ciclo tentara novamente",
                    job_id,
                    type(exc).__name__,
                )
            except Exception:
                # Falhas transitorias de PostgreSQL/Redis nao podem encerrar o
                # reconciliador nem impedir que os demais jobs sejam tentados.
                LOG.exception("falha transitoria ao recuperar materializacao %s", job_id)
        with self.lock:
            completion_excluded = tuple(self.materializations)
        for job_id in self._recoverable_local_completion_ids(
            LOCAL_COMPLETION_RECOVERY_BATCH_SIZE,
            completion_excluded,
        ):
            if self.stop.is_set():
                break
            with self.lock:
                if job_id in self.materializations:
                    continue
            try:
                recovered += int(self._recover_local_completion(job_id))
            except KeyError:
                # O job pode ter avancado entre a listagem e a releitura.
                continue
            except Exception:
                # Erro de persistencia e transitorio; nao converter um arquivo
                # ja validado em failed apenas porque o PostgreSQL oscilou.
                LOG.exception("falha ao finalizar materializacao local %s", job_id)
        return recovered

    def _recovery_loop(
        self, poll_interval: float = MATERIALIZATION_RECOVERY_INTERVAL
    ) -> None:
        interval = max(0.0, float(poll_interval))
        while not self.stop.is_set():
            try:
                self._recover_materializations_once()
            except Exception:
                LOG.exception("varredura de recuperacao torrent falhou; sera repetida")
            if self.stop.wait(interval):
                return

    def _write_transfer_progress(
        self,
        job_id: str,
        *,
        state: str,
        bytes_total: int,
        bytes_done: int,
        local_files: Sequence[dict[str, object]],
        error: str | None = None,
        expected_states: Sequence[str] | None = None,
    ) -> bool:
        normalized_expected = tuple(
            dict.fromkeys(
                str(value).strip().casefold()
                for value in (expected_states or ())
                if str(value).strip()
            )
        )
        with connection(self.settings) as database:
            params: tuple[object, ...] = (
                state,
                max(0, int(bytes_total)),
                max(0, min(int(bytes_done), int(bytes_total))),
                Jsonb(list(local_files)),
                error,
                job_id,
            )
            if expected_states is None:
                database.execute(
                    """
                    UPDATE runtime.transfer_jobs SET state=%s,bytes_total=%s,
                      bytes_done=%s,local_files=%s,error=%s,updated_at=now()
                    WHERE id=%s
                    """,
                    params,
                )
                updated = True
            else:
                if not normalized_expected:
                    raise ValueError("estados esperados ausentes")
                # UPDATE adquire lock da linha e reavalia o predicado depois
                # de um concorrente liberar o lock. Assim cancelled/completed
                # nunca sao ressuscitados, e o trigger ve apenas transicoes
                # permitidas a partir do estado realmente persistido.
                row = database.execute(
                    """
                    UPDATE runtime.transfer_jobs SET state=%s,bytes_total=%s,
                      bytes_done=%s,local_files=%s,error=%s,updated_at=now()
                    WHERE id=%s AND state=ANY(%s::text[])
                    RETURNING state
                    """,
                    (*params, list(normalized_expected)),
                ).fetchone()
                updated = row is not None
            database.commit()
        return updated

    def _write_transfer_error(self, job_id: str, error: BaseException | str) -> bool:
        message = str(error).strip()[:2000] or type(error).__name__
        with connection(self.settings) as database:
            row = database.execute(
                """
                UPDATE runtime.transfer_jobs
                SET state='failed',error=%s,updated_at=now()
                WHERE id=%s AND state=ANY(%s::text[])
                RETURNING state
                """,
                (message, job_id, list(CANCELLABLE_TRANSFER_STATES)),
            ).fetchone()
            database.commit()
        return row is not None

    def _cancel_transfer_job(self, job_id: str) -> bool:
        """Cancela sem regravar contadores obtidos por uma leitura obsoleta."""

        with connection(self.settings) as database:
            row = database.execute(
                """
                UPDATE runtime.transfer_jobs
                SET state='cancelled',error=NULL,updated_at=now()
                WHERE id=%s AND state=ANY(%s::text[])
                RETURNING state
                """,
                (job_id, list(CANCELLABLE_TRANSFER_STATES)),
            ).fetchone()
            database.commit()
        return row is not None

    def add(self, session_id: str) -> StreamSession:
        session_id = normalized_session_id(session_id)
        row = self._lookup(session_id)
        expected_path = safe_relative_path(str(row["path"]))
        if not row["is_video"] or not is_video_name(expected_path):
            raise UnsafeMediaError("arquivo nao aprovado como video")
        site = str(row["site"])
        infohash = str(row["infohash"])
        metainfo = safe_owned_path(
            self._torrent_root(site), *Path(str(row["metainfo_relpath"])).parts
        )
        if metainfo.suffix.casefold() != ".torrent" or not metainfo.is_file():
            raise UnsafeMediaError("metainfo inventariado ausente")
        payload = metainfo.read_bytes()
        decode_metainfo(payload, infohash)
        torrent_info = self.lt.torrent_info(str(metainfo))
        files = torrent_info.files()
        match: tuple[int, str] | None = None
        for index in range(files.num_files()):
            candidate = str(files.file_path(index)).replace("\\", "/")
            comparable = candidate.split("/", 1)[-1] if "/" in candidate else candidate
            if candidate == expected_path or comparable == expected_path:
                if int(files.file_size(index)) == int(row["size"]):
                    match = (index, candidate)
                    break
        if match is None:
            raise UnsafeMediaError("arquivo do inventario diverge do metainfo")
        file_index, engine_path = match
        key = f"{site}-{infohash}"
        with self.lock:
            shared = self.downloads.get(key)
            if shared is None:
                self._ensure_download_capacity_locked(key)
                shared = self._add_download(
                    key, site, infohash, torrent_info, {file_index}
                )
                self.downloads[key] = shared
            else:
                shared.selected_indices.add(file_index)
                self._apply_file_priorities(shared)
            shared.sessions.add(session_id)
            file_relative = Path(engine_path.replace("/", os.sep))
            target = safe_owned_path(shared.save_root, *file_relative.parts)
            item = StreamSession(
                id=session_id,
                download_key=key,
                file_index=file_index,
                file_size=int(files.file_size(file_index)),
                file_offset=int(files.file_offset(file_index)),
                relative_path=engine_path,
                file_path=target,
            )
            self.sessions[session_id] = item
        self.prioritize(session_id, 0, min(item.file_size, 8 * 1024**2))
        if item.file_size > 8 * 1024**2:
            self.prioritize(
                session_id,
                max(0, item.file_size - 4 * 1024**2),
                4 * 1024**2,
                base_deadline_ms=12_000,
            )
        with connection(self.settings) as database:
            database.execute(
                "UPDATE runtime.playback_sessions SET state='buffering',updated_at=now() WHERE id=%s",
                (session_id,),
            )
            database.execute(
                "UPDATE runtime.download_jobs SET state='downloading',updated_at=now() WHERE session_id=%s",
                (session_id,),
            )
            database.commit()
        return item

    @staticmethod
    def _engine_path_matches(engine_path: str, inventory_path: str) -> bool:
        if engine_path == inventory_path:
            return True
        _root, separator, nested = engine_path.partition("/")
        return bool(separator) and nested == inventory_path

    def _validated_materialization(
        self,
        job: dict[str, Any],
        inventory_files: Sequence[dict[str, Any]],
    ) -> tuple[Any, tuple[MaterializedFile, ...]]:
        site = str(job.get("source_site") or "").strip().casefold()
        infohash = normalized_infohash(str(job.get("infohash") or ""))
        target = str(job.get("target") or "").strip()
        if not target:
            raise UnsafeMediaError("destino da materializacao ausente")
        metainfo_relpath = safe_relative_path(str(job.get("metainfo_relpath") or ""))
        metainfo = safe_owned_path(
            self._torrent_root(site), *Path(metainfo_relpath).parts
        )
        if metainfo.suffix.casefold() != ".torrent" or not metainfo.is_file():
            raise UnsafeMediaError("metainfo inventariado ausente")
        payload = metainfo.read_bytes()
        decoded = decode_metainfo(payload, infohash)
        decoded_entries = metainfo_files(decoded)
        decoded_files = {
            path: (index, size) for index, path, size in decoded_entries
        }
        if len(decoded_files) != len(decoded_entries):
            raise UnsafeMediaError("metainfo contem caminhos duplicados")

        torrent_info = self.lt.torrent_info(str(metainfo))
        engine_files = torrent_info.files()
        key = f"{site}-{infohash}"
        save_root = safe_owned_path(self.settings.media_root, key)
        selected: list[MaterializedFile] = []
        selected_indices: set[int] = set()
        for row in inventory_files:
            expected_path = safe_relative_path(str(row.get("path") or ""))
            try:
                expected_size = int(row["size"])
                catalog_file_id = int(row["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise UnsafeMediaError("arquivo inventariado invalido") from exc
            if expected_size < 0:
                raise UnsafeMediaError("tamanho inventariado invalido")
            decoded_item = decoded_files.get(expected_path)
            if decoded_item is None or int(decoded_item[1]) != expected_size:
                raise UnsafeMediaError("arquivo do inventario diverge do metainfo")
            file_index = int(decoded_item[0])
            if file_index in selected_indices or file_index >= engine_files.num_files():
                raise UnsafeMediaError("indice de arquivo invalido ou duplicado")
            engine_path = safe_relative_path(
                str(engine_files.file_path(file_index)).replace("\\", "/")
            )
            if (
                int(engine_files.file_size(file_index)) != expected_size
                or not self._engine_path_matches(engine_path, expected_path)
            ):
                raise UnsafeMediaError("libtorrent diverge do metainfo inventariado")
            file_path = safe_owned_path(
                save_root, *Path(engine_path.replace("/", os.sep)).parts
            )
            selected_indices.add(file_index)
            selected.append(
                MaterializedFile(
                    catalog_file_id=catalog_file_id,
                    file_index=file_index,
                    relative_path=expected_path,
                    file_size=expected_size,
                    file_path=file_path,
                )
            )
        if not selected:
            raise UnsafeMediaError("nenhum arquivo selecionado")
        return torrent_info, tuple(selected)

    def materialize(self, job_id: str) -> Materialization | None:
        job_id = _normalized_job_id(job_id)
        with self.lock:
            active = self.materializations.get(job_id)
        if active is not None:
            return active
        job, inventory_files = self._lookup_transfer_job(job_id)
        job_state = str(job.get("state") or "").casefold()
        if job_state == "downloaded":
            return None
        if job_state not in MATERIALIZATION_STATES:
            raise UnsafeMediaError(
                f"job nao pode ser materializado no estado {job_state or 'ausente'}"
            )
        site = str(job["source_site"]).strip().casefold()
        infohash = normalized_infohash(str(job["infohash"]))
        key = f"{site}-{infohash}"
        self._admit_materialization(job_id, key)
        try:
            if job_state == "queued":
                claimed = self._write_transfer_progress(
                    job_id,
                    state="validating",
                    bytes_total=int(job.get("bytes_total") or 0),
                    bytes_done=int(job.get("bytes_done") or 0),
                    local_files=list(job.get("local_files") or []),
                    error=None,
                    expected_states=("queued",),
                )
                if claimed is False:
                    return None
                job_state = "validating"
            try:
                torrent_info, files = self._validated_materialization(
                    job, inventory_files
                )
            except (UnsafeMediaError, OSError, RuntimeError) as exc:
                try:
                    self._write_transfer_error(job_id, exc)
                except Exception:
                    LOG.exception(
                        "falha de validacao do job %s nao foi persistida", job_id
                    )
                raise
            selected_indices = {value.file_index for value in files}
            item = Materialization(
                id=job_id,
                download_key=key,
                target=str(job["target"]),
                files=files,
                bytes_total=sum(value.file_size for value in files),
                destination_path=str(job.get("destination_path") or ""),
            )
            with self.lock:
                existing = self.materializations.get(job_id)
                if existing is not None:
                    return existing
                # A reserva vira uma materializacao ativa na mesma secao
                # critica, sem contar o mesmo job duas vezes.
                self._materialization_admissions_locked().pop(job_id, None)
                shared = self.downloads.get(key)
                if shared is None:
                    self._ensure_download_capacity_locked(key)
                    shared = self._add_download(
                        key, site, infohash, torrent_info, selected_indices
                    )
                    self.downloads[key] = shared
                else:
                    shared.selected_indices.update(selected_indices)
                    self._apply_file_priorities(shared)
                shared.materializations.add(job_id)
                self.materializations[job_id] = item
            try:
                state, bytes_done, local_files = self._materialization_snapshot(item)
                persisted_state = self._persist_materialization_snapshot(
                    item,
                    state=state,
                    bytes_done=bytes_done,
                    local_files=local_files,
                )
            except Exception:
                self._release_materialization(job_id)
                raise
            if persisted_state not in MATERIALIZATION_PROGRESS_STATES:
                self._release_materialization(job_id)
            return item
        finally:
            self._release_materialization_admission(job_id)

    def _materialization_snapshot(
        self, item: Materialization
    ) -> tuple[str, int, list[dict[str, object]]]:
        with self.lock:
            shared = self.downloads.get(item.download_key)
        if shared is None:
            raise KeyError(item.id)
        progress = shared.handle.file_progress()
        bytes_done = 0
        complete = True
        local_files: list[dict[str, object]] = []
        for selected in item.files:
            if selected.file_index >= len(progress):
                raise RuntimeError("libtorrent retornou progresso incompleto")
            file_done = max(
                0, min(int(progress[selected.file_index]), selected.file_size)
            )
            bytes_done += file_done
            on_disk = False
            try:
                on_disk = (
                    selected.file_path.is_file()
                    and selected.file_path.stat().st_size == selected.file_size
                )
            except OSError:
                on_disk = False
            file_complete = file_done == selected.file_size and on_disk
            complete = complete and file_complete
            local_files.append(
                {
                    **selected.as_dict(),
                    "bytes_done": file_done,
                    "complete": file_complete,
                }
            )
        return ("downloaded" if complete else "downloading"), bytes_done, local_files

    def _persist_materialization_snapshot(
        self,
        item: Materialization,
        *,
        state: str,
        bytes_done: int,
        local_files: Sequence[dict[str, object]],
    ) -> str:
        persisted_files = list(local_files)
        expected_states = (
            ("validating", "downloading", "downloaded")
            if state == "downloaded"
            else MATERIALIZATION_PROGRESS_STATES
        )
        updated = self._write_transfer_progress(
            item.id,
            state=state,
            bytes_total=item.bytes_total,
            bytes_done=bytes_done,
            local_files=persisted_files,
            expected_states=expected_states,
        )
        if updated is False:
            return str(self._read_transfer_status(item.id)["state"]).casefold()
        if state == "downloaded" and item.target == "local":
            claimed = self._write_transfer_progress(
                item.id,
                state="classifying",
                bytes_total=item.bytes_total,
                bytes_done=bytes_done,
                local_files=persisted_files,
                expected_states=("downloaded",),
            )
            if claimed is False:
                return str(
                    self._read_transfer_status(item.id)["state"]
                ).casefold()
            persisted_files = self._publish_local_files(item, persisted_files)
            recorded = self._write_transfer_progress(
                item.id,
                state="classifying",
                bytes_total=item.bytes_total,
                bytes_done=bytes_done,
                local_files=persisted_files,
                expected_states=("classifying",),
            )
            if recorded is False:
                return str(
                    self._read_transfer_status(item.id)["state"]
                ).casefold()
            current_state = "classifying"
            for final_state in ("verifying", "completed"):
                advanced = self._write_transfer_progress(
                    item.id,
                    state=final_state,
                    bytes_total=item.bytes_total,
                    bytes_done=bytes_done,
                    local_files=persisted_files,
                    expected_states=(current_state,),
                )
                if advanced is False:
                    return str(
                        self._read_transfer_status(item.id)["state"]
                    ).casefold()
                current_state = final_state
            return "completed"
        return state

    @staticmethod
    def _same_file_content(left: Path, right: Path, expected_size: int) -> bool:
        try:
            if (
                not left.is_file()
                or not right.is_file()
                or left.stat().st_size != expected_size
                or right.stat().st_size != expected_size
            ):
                return False
            try:
                if left.samefile(right):
                    return True
            except OSError:
                pass
            digests: list[str] = []
            for path in (left, right):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                digests.append(digest.hexdigest())
            return digests[0] == digests[1]
        except OSError:
            return False

    def _publish_destination(
        self,
        item: Materialization,
        selected: MaterializedFile,
        source: Path,
    ) -> tuple[Path, str]:
        destination_root = safe_relative_path(item.destination_path)
        relative = _relative_to_classified_root(
            selected.relative_path, destination_root
        )
        root = self.settings.media_root.resolve()
        raw_destination = root.joinpath(
            *Path(destination_root).parts,
            *Path(relative).parts,
        )
        current = root
        for component in raw_destination.relative_to(root).parts[:-1]:
            current = current / component
            if self._is_link_or_junction(current):
                raise UnsafeMediaError("destino local contem link ou junction")
        raw_destination.parent.mkdir(parents=True, exist_ok=True)
        current = root
        for component in raw_destination.relative_to(root).parts[:-1]:
            current = current / component
            if self._is_link_or_junction(current):
                raise UnsafeMediaError("destino local contem link ou junction")
        destination = safe_owned_path(
            root,
            *Path(destination_root).parts,
            *Path(relative).parts,
        )
        if self._is_link_or_junction(destination):
            raise UnsafeMediaError("arquivo local de destino e um link ou junction")
        if destination.exists() and not self._same_file_content(
            source, destination, selected.file_size
        ):
            leaf = Path(relative).name
            suffix = "".join(Path(leaf).suffixes)
            stem = leaf[: -len(suffix)] if suffix else leaf
            collision_name = f"{stem}~{selected.catalog_file_id}{suffix}"
            collision_relative = str(
                Path(relative).with_name(collision_name)
            ).replace("\\", "/")
            destination = safe_owned_path(
                root,
                *Path(destination_root).parts,
                *Path(collision_relative).parts,
            )
            relative = collision_relative
            if self._is_link_or_junction(destination):
                raise UnsafeMediaError("arquivo local de destino e um link ou junction")
        return destination, relative

    def _validated_publish_source(
        self,
        item: Materialization,
        selected: MaterializedFile,
    ) -> Path:
        try:
            root = self.settings.media_root.resolve(strict=True)
        except OSError as exc:
            raise UnsafeMediaError("raiz de midia local indisponivel") from exc
        lexical = selected.file_path.absolute()
        try:
            relative_to_root = lexical.relative_to(root)
        except ValueError as exc:
            raise UnsafeMediaError("origem torrent fora da raiz autorizada") from exc
        if not relative_to_root.parts:
            raise UnsafeMediaError("origem torrent invalida")
        current = root
        try:
            for component in relative_to_root.parts:
                current = current / component
                if self._is_link_or_junction(current):
                    raise UnsafeMediaError(
                        "origem torrent contem link ou junction"
                    )
            source = lexical.resolve(strict=True)
            source.relative_to(root)
            stat = source.stat()
        except UnsafeMediaError:
            raise
        except (OSError, ValueError) as exc:
            raise UnsafeMediaError("origem torrent local indisponivel") from exc
        if not source.is_file() or stat.st_size != selected.file_size:
            raise UnsafeMediaError("origem torrent local invalida")
        return source

    def _source_is_staging_or_destination(
        self,
        item: Materialization,
        source: Path,
        destination: Path,
    ) -> bool:
        root = self.settings.media_root.resolve(strict=True)
        staging = safe_owned_path(
            root, *Path(safe_relative_path(item.download_key)).parts
        )
        try:
            source.relative_to(staging)
            return True
        except ValueError:
            pass
        try:
            return destination.exists() and source.samefile(destination)
        except OSError:
            return False

    def _publish_local_files(
        self,
        item: Materialization,
        local_files: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not item.destination_path:
            raise UnsafeMediaError("destination_path local ausente")
        manifests = {
            int(value.get("file_id") or 0): dict(value) for value in local_files
        }
        published: list[dict[str, object]] = []
        for selected in item.files:
            manifest = manifests.get(selected.catalog_file_id)
            if manifest is None or manifest.get("complete") is not True:
                raise UnsafeMediaError("arquivo local ainda nao esta completo")
            source = self._validated_publish_source(item, selected)
            destination, relative = self._publish_destination(
                item, selected, source
            )
            if not self._source_is_staging_or_destination(
                item, source, destination
            ):
                raise UnsafeMediaError("origem local fora do staging autorizado")
            if destination.exists():
                if not self._same_file_content(source, destination, selected.file_size):
                    raise UnsafeMediaError("colisao no destino local classificado")
            else:
                try:
                    os.link(source, destination)
                except OSError as exc:
                    if exc.errno not in {
                        errno.EXDEV,
                        errno.EPERM,
                        errno.EACCES,
                        errno.ENOTSUP,
                        errno.EOPNOTSUPP,
                    }:
                        raise
                    temporary_name: str | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            dir=destination.parent,
                            prefix=f".{destination.name}.",
                            suffix=".part",
                            delete=False,
                        ) as temporary, source.open("rb") as handle:
                            temporary_name = temporary.name
                            shutil.copyfileobj(handle, temporary, length=1024 * 1024)
                            temporary.flush()
                            os.fsync(temporary.fileno())
                        temporary_path = Path(temporary_name)
                        if destination.exists():
                            if not self._same_file_content(
                                source, destination, selected.file_size
                            ):
                                raise UnsafeMediaError(
                                    "colisao concorrente no destino local"
                                )
                        else:
                            temporary_path.replace(destination)
                    finally:
                        if temporary_name:
                            Path(temporary_name).unlink(missing_ok=True)
            if not self._same_file_content(source, destination, selected.file_size):
                raise UnsafeMediaError("publicacao local diverge do torrent")
            published.append(
                {
                    **manifest,
                    "path": manifest.get("path") or selected.relative_path,
                    "relative_path": relative,
                    "source_path": str(source),
                    "local_path": str(destination),
                    "bytes_done": selected.file_size,
                    "complete": True,
                }
            )
        return published

    def _refresh_materialization(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            item = self.materializations.get(job_id)
        if item is None:
            return self._read_transfer_status(job_id)
        state, bytes_done, local_files = self._materialization_snapshot(item)
        persisted_state = self._persist_materialization_snapshot(
            item,
            state=state,
            bytes_done=bytes_done,
            local_files=local_files,
        )
        if persisted_state not in MATERIALIZATION_PROGRESS_STATES:
            self._release_materialization(job_id)
        return self._read_transfer_status(job_id)

    def materialization_status(self, job_id: str) -> dict[str, Any]:
        job_id = _normalized_job_id(job_id)
        with self.lock:
            active = job_id in self.materializations
        return (
            self._refresh_materialization(job_id)
            if active
            else self._read_transfer_status(job_id)
        )

    def _add_download(
        self,
        key: str,
        site: str,
        infohash: str,
        torrent_info: Any,
        file_indices: set[int],
    ) -> SharedDownload:
        save_root = safe_owned_path(self.settings.media_root, key)
        save_root.mkdir(parents=True, exist_ok=True)
        file_count = torrent_info.files().num_files()
        priorities = [0] * file_count
        if not file_indices or any(
            index < 0 or index >= file_count for index in file_indices
        ):
            raise UnsafeMediaError("indices selecionados invalidos")
        for file_index in file_indices:
            priorities[file_index] = 7
        resume_path = safe_owned_path(self.settings.resume_root, f"{key}.fastresume")
        parameters: Any
        if resume_path.is_file() and resume_path.stat().st_size <= 8 * 1024**2:
            try:
                parameters = self.lt.read_resume_data(resume_path.read_bytes())
                parameters.ti = torrent_info
                parameters.save_path = str(save_root)
                parameters.file_priorities = priorities
            except Exception:
                parameters = self._new_parameters(torrent_info, save_root, priorities)
        else:
            parameters = self._new_parameters(torrent_info, save_root, priorities)
        handle = self.engine.add_torrent(parameters)
        handle.prioritize_files(priorities)
        return SharedDownload(
            key=key,
            site=site,
            infohash=infohash,
            handle=handle,
            torrent_info=torrent_info,
            save_root=save_root,
            piece_length=int(torrent_info.piece_length()),
            selected_indices=set(file_indices),
        )

    def _new_parameters(self, torrent_info: Any, root: Path, priorities: list[int]) -> dict[str, Any]:
        return {
            "ti": torrent_info,
            "save_path": str(root),
            "file_priorities": priorities,
            "storage_mode": self.lt.storage_mode_t.storage_mode_sparse,
        }

    def _apply_file_priorities(self, shared: SharedDownload) -> None:
        priorities = [0] * shared.torrent_info.files().num_files()
        for index in shared.selected_indices:
            priorities[index] = 7
        shared.handle.prioritize_files(priorities)

    def _reconcile_download(self, shared: SharedDownload) -> None:
        remove = False
        with self.lock:
            selected_indices = {
                self.sessions[session_id].file_index
                for session_id in shared.sessions
                if session_id in self.sessions
            }
            for job_id in shared.materializations:
                item = self.materializations.get(job_id)
                if item is not None:
                    selected_indices.update(value.file_index for value in item.files)
            if selected_indices:
                shared.selected_indices = selected_indices
                self._apply_file_priorities(shared)
            else:
                self.downloads.pop(shared.key, None)
                remove = True
        if remove:
            self._save_resume(shared)
            try:
                self.engine.remove_torrent(shared.handle)
            except Exception as exc:
                LOG.warning("handle libtorrent nao removido para %s: %s", shared.key, exc)

    def _release_materialization(self, job_id: str) -> Materialization | None:
        with self.lock:
            item = self.materializations.pop(job_id, None)
            shared = self.downloads.get(item.download_key) if item else None
            if shared is not None:
                shared.materializations.discard(job_id)
        if shared is not None:
            self._reconcile_download(shared)
        return item

    def cancel_materialization(self, job_id: str) -> dict[str, Any]:
        job_id = _normalized_job_id(job_id)
        with self.lock:
            item = self.materializations.get(job_id)
        if item is not None:
            state, bytes_done, local_files = self._materialization_snapshot(item)
            self._write_transfer_progress(
                job_id,
                state="downloaded" if state == "downloaded" else "cancelled",
                bytes_total=item.bytes_total,
                bytes_done=bytes_done,
                local_files=local_files,
                error=None,
                expected_states=(
                    ("validating", "downloading", "downloaded")
                    if state == "downloaded"
                    else CANCELLABLE_TRANSFER_STATES
                ),
            )
            self._release_materialization(job_id)
        else:
            status = self._read_transfer_status(job_id)
            protected_local_states = (
                {"downloaded", "classifying", "verifying"}
                if status.get("target") == "local"
                and status.get("source_site") in {"filecr", "1337x"}
                else {"downloaded"}
            )
            if status["state"] not in TERMINAL_TRANSFER_STATES | protected_local_states:
                self._cancel_transfer_job(job_id)
        return self._read_transfer_status(job_id)

    def get(self, session_id: str) -> tuple[StreamSession, SharedDownload]:
        with self.lock:
            item = self.sessions.get(session_id)
            shared = self.downloads.get(item.download_key) if item else None
        if item is None or shared is None:
            raise KeyError(session_id)
        return item, shared

    def prioritize(
        self,
        session_id: str,
        offset: int,
        length: int,
        base_deadline_ms: int = 750,
    ) -> None:
        item, shared = self.get(session_id)
        start = max(0, min(offset, item.file_size - 1))
        end = max(start, min(start + max(1, length) - 1, item.file_size - 1))
        first = (item.file_offset + start) // shared.piece_length
        last = (item.file_offset + end) // shared.piece_length
        for sequence, piece in enumerate(range(first, last + 1)):
            shared.handle.set_piece_deadline(piece, base_deadline_ms + sequence * 125)

    def range_available(self, session_id: str, offset: int, length: int) -> bool:
        item, shared = self.get(session_id)
        if length <= 0 or offset < 0 or offset + length > item.file_size:
            return False
        first = (item.file_offset + offset) // shared.piece_length
        last = (item.file_offset + offset + length - 1) // shared.piece_length
        return all(shared.handle.have_piece(piece) for piece in range(first, last + 1))

    def wait_for_range(
        self, session_id: str, offset: int, length: int, timeout: float = 600
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.prioritize(session_id, offset, length)
            if self.range_available(session_id, offset, length):
                return True
            time.sleep(0.2)
        return False

    def read(self, session_id: str, start: int, end: int) -> Iterator[bytes]:
        item, _shared = self.get(session_id)
        position = start
        while position <= end:
            size = min(1024**2, end - position + 1)
            if not self.wait_for_range(session_id, position, size):
                raise TimeoutError("pecas verificadas indisponiveis")
            with item.file_path.open("rb") as source:
                source.seek(position)
                payload = source.read(size)
            if len(payload) != size:
                raise OSError("leitura incompleta")
            if position == 0 and not has_video_signature(payload[:4096]):
                raise UnsafeMediaError("assinatura nao e de video")
            yield payload
            position += size

    def metrics(self, session_id: str) -> dict[str, Any]:
        item, shared = self.get(session_id)
        status = shared.handle.status()
        progress = int(shared.handle.file_progress()[item.file_index])
        global_start = item.file_offset
        first_piece = global_start // shared.piece_length
        last_global = min(
            item.file_offset + item.file_size,
            global_start + 512 * 1024**2,
            int(shared.torrent_info.total_size()),
        )
        last_piece = max(first_piece, (last_global - 1) // shared.piece_length)
        available_end = global_start
        for piece in range(first_piece, last_piece + 1):
            if not shared.handle.have_piece(piece):
                break
            available_end = min((piece + 1) * shared.piece_length, last_global)
        return {
            "download_bytes_per_second": int(status.download_rate),
            "upload_bytes_per_second": int(status.upload_rate),
            "download_bps": int(status.download_rate) * 8,
            "seeds": int(status.num_seeds),
            "peers": int(status.num_peers),
            "progress_bytes": progress,
            "file_size": item.file_size,
            "progress": progress / item.file_size if item.file_size else 0.0,
            "verified_buffer_bytes": max(0, available_end - global_start),
            "state": str(status.state),
        }

    def close_session(self, session_id: str) -> None:
        with self.lock:
            item = self.sessions.pop(session_id, None)
            if item is None:
                return
            shared = self.downloads.get(item.download_key)
            if shared is None:
                return
            shared.sessions.discard(session_id)
        self._reconcile_download(shared)

    def _save_resume(self, shared: SharedDownload) -> None:
        try:
            shared.handle.save_resume_data(self.lt.save_resume_flags_t.flush_disk_cache)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for alert in self.engine.pop_alerts():
                    if isinstance(alert, self.lt.save_resume_data_alert):
                        payload = bytes(self.lt.write_resume_data_buf(alert.params))
                        target = safe_owned_path(self.settings.resume_root, f"{shared.key}.fastresume")
                        temporary = safe_owned_path(
                            self.settings.resume_root, f".{shared.key}.{uuid.uuid4().hex}.tmp"
                        )
                        temporary.write_bytes(payload)
                        os.replace(temporary, target)
                        return
                time.sleep(0.1)
        except Exception as exc:
            LOG.warning("fast-resume adiado para %s: %s", shared.key, exc)

    def _monitor(self) -> None:
        while not self.stop.wait(2):
            with self.lock:
                session_ids = tuple(self.sessions)
                materialization_ids = tuple(self.materializations)
            for session_id in session_ids:
                try:
                    metrics = self.metrics(session_id)
                    with connection(self.settings) as database:
                        database.execute(
                            """
                            UPDATE runtime.playback_sessions SET download_rate_bps=%s,
                              verified_buffer_bytes=%s,updated_at=now() WHERE id=%s
                            """,
                            (
                                metrics["download_bps"],
                                metrics["verified_buffer_bytes"],
                                session_id,
                            ),
                        )
                        database.execute(
                            """
                            UPDATE runtime.download_jobs SET metrics=%s,updated_at=now()
                            WHERE session_id=%s
                            """,
                            (Jsonb(metrics), session_id),
                        )
                        database.commit()
                    self.client.publish(f"session:{session_id}", json.dumps(metrics))
                except Exception:
                    continue
            for job_id in materialization_ids:
                try:
                    self._refresh_materialization(job_id)
                except (KeyError, RuntimeError, UnsafeMediaError) as exc:
                    LOG.exception("materializacao %s falhou", job_id)
                    try:
                        self._write_transfer_error(job_id, exc)
                    except Exception:
                        LOG.exception(
                            "estado de erro da materializacao %s nao foi persistido",
                            job_id,
                        )
                    self._release_materialization(job_id)
                except Exception:
                    # PostgreSQL/Redis podem ficar transitoriamente indisponiveis.
                    # O handle continua vivo e o proximo ciclo tenta persistir de novo.
                    LOG.exception(
                        "progresso da materializacao %s nao foi persistido", job_id
                    )

    def close_all(self) -> None:
        self.stop.set()
        with self.lock:
            downloads = tuple(self.downloads.values())
            self.sessions.clear()
            self.materializations.clear()
            self._materialization_admissions_locked().clear()
            self.downloads.clear()
        for shared in downloads:
            self._save_resume(shared)
            try:
                self.engine.remove_torrent(shared.handle)
            except Exception as exc:
                LOG.warning("handle libtorrent nao removido para %s: %s", shared.key, exc)


def _internal(settings: Settings) -> bool:
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return internal_token_matches(supplied, settings.internal_token)


def _parse_range(value: str | None, total: int) -> tuple[int, int, bool]:
    if not value:
        return 0, total - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("range invalido")
    first, separator, last = value[6:].partition("-")
    if not separator:
        raise ValueError("range invalido")
    if first:
        start = int(first)
        end = int(last) if last else total - 1
    else:
        suffix = int(last)
        start = max(0, total - suffix)
        end = total - 1
    if start < 0 or end < start or end >= total:
        raise ValueError("range invalido")
    return start, end, True


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    engine = TorrentEngine(settings)

    def capacity_details() -> dict[str, int]:
        details = getattr(engine, "capacity_details", None)
        if callable(details):
            return details()
        # Compatibilidade com adaptadores/fixtures que implementam o contrato
        # anterior do engine sem telemetria de capacidade.
        return {
            "sessions": len(getattr(engine, "sessions", {})),
            "materializations": len(getattr(engine, "materializations", {})),
        }

    start_heartbeat(
        "torrent-engine",
        capacity_details,
    )
    atexit.register(engine.close_all)
    app = Flask(__name__)
    app.config["engine"] = engine

    @app.get("/health")
    def health() -> Response:
        return jsonify({"status": "ok", "engine": True, **capacity_details()})

    @app.post("/internal/sessions")
    def create_session() -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        payload = request.get_json(silent=True) or {}
        try:
            item = engine.add(str(payload.get("session_id", "")))
        except TorrentCapacityError as exc:
            return jsonify({"error": str(exc), "retryable": True}), 429
        except (KeyError, UnsafeMediaError, OSError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(
            {
                "session_id": item.id,
                "file_size": item.file_size,
                "relative_path": item.relative_path,
            }
        ), 201

    @app.get("/internal/sessions/<session_id>")
    def session_status(session_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            return jsonify(engine.metrics(normalized_session_id(session_id)))
        except (KeyError, UnsafeMediaError):
            return jsonify({"error": "sessao ausente"}), 404

    @app.delete("/internal/sessions/<session_id>")
    def close_session(session_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            engine.close_session(normalized_session_id(session_id))
        except UnsafeMediaError:
            return jsonify({"error": "sessao invalida"}), 422
        return jsonify({"closed": True})

    @app.post("/internal/materializations")
    def create_materialization() -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        payload = request.get_json(silent=True) or {}
        job_id = str(payload.get("job_id") or "")
        try:
            normalized = _normalized_job_id(job_id)
            engine.materialize(normalized)
            status = engine.materialization_status(normalized)
        except TorrentCapacityError as exc:
            return jsonify({"error": str(exc), "retryable": True}), 429
        except KeyError:
            return jsonify({"error": "job ausente"}), 404
        except (UnsafeMediaError, OSError, RuntimeError) as exc:
            try:
                engine._write_transfer_error(_normalized_job_id(job_id), exc)
            except Exception:
                pass
            return jsonify({"error": str(exc)}), 422
        return jsonify(status), 200 if status["state"] == "downloaded" else 202

    @app.get("/internal/materializations/<job_id>")
    def materialization_status(job_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            return jsonify(engine.materialization_status(job_id))
        except KeyError:
            return jsonify({"error": "job ausente"}), 404
        except UnsafeMediaError as exc:
            return jsonify({"error": str(exc)}), 422

    @app.delete("/internal/materializations/<job_id>")
    def cancel_materialization(job_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            return jsonify(engine.cancel_materialization(job_id))
        except KeyError:
            return jsonify({"error": "job ausente"}), 404
        except UnsafeMediaError as exc:
            return jsonify({"error": str(exc)}), 422

    @app.route("/source/<session_id>/<token>", methods=["GET", "HEAD"])
    def source(session_id: str, token: str) -> Response:
        try:
            session_id = normalized_session_id(session_id)
            row = engine._lookup(session_id)
            if not token_matches(token, settings.session_pepper, str(row["token_hash"])):
                return jsonify({"error": "token invalido"}), 403
            item, _shared = engine.get(session_id)
            start, end, partial = _parse_range(request.headers.get("Range"), item.file_size)
        except KeyError:
            return jsonify({"error": "sessao ausente"}), 404
        except (UnsafeMediaError, ValueError):
            return Response(status=416, headers={"Content-Range": f"bytes */{item.file_size}" if 'item' in locals() else "bytes */0"})
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": "video/x-matroska" if item.file_path.suffix.casefold() == ".mkv" else "video/mp4",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{item.file_size}"
        if request.method == "HEAD":
            return Response(status=206 if partial else 200, headers=headers)
        return Response(
            stream_with_context(engine.read(session_id, start, end)),
            status=206 if partial else 200,
            headers=headers,
            direct_passthrough=True,
        )

    return app


def main() -> None:
    create_app().run(host="0.0.0.0", port=7101, threaded=True)


if __name__ == "__main__":
    main()
