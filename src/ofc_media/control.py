from __future__ import annotations

import logging
import math
import mimetypes
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from psycopg.types.json import Jsonb
from redis import Redis

from . import __version__
from .auth import token_digest, token_matches
from .buffering import DynamicBufferController
from .config import Settings
from .curation import CurationService
from .db import connection
from .file_kinds import FILE_KINDS, safe_destination_path
from .heartbeat import start_heartbeat
from .inventory import InventoryService
from .safety import UnsafeMediaError, normalized_infohash, normalized_session_id
from .subtitle_tracks import (
    resolve_subtitle_path,
    to_webvtt,
    track_id as subtitle_track_id,
)


LOG = logging.getLogger("ofc.control")
CATALOG_SITES = frozenset({"filecr", "1337x", "gdrive"})
TRANSFER_TARGETS = frozenset({"local", "gdrive"})
MAX_TRANSFER_FILES = 200
MAX_DETAIL_FILES = 500
LARGE_TRANSFER_BYTES = 10 * 1024**3
SUBTITLE_TRACK_RE = re.compile(r"^[0-9a-f]{24}$")
STREAM_RE = re.compile(
    r"^/stream/(?P<session>[0-9a-f]{32})/(?P<token>[A-Za-z0-9_-]{32,128})/"
    r"(?P<storage>[0-9a-f]{64})/"
)


def _selected_file_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("file_ids deve ser uma lista nao vazia")
    if len(value) > MAX_TRANSFER_FILES:
        raise ValueError(f"file_ids excede o limite de {MAX_TRANSFER_FILES} arquivos")
    selected: list[int] = []
    for raw in value:
        if isinstance(raw, bool):
            raise ValueError("file_ids deve conter inteiros positivos")
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError("file_ids deve conter inteiros positivos")
        try:
            file_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("file_ids deve conter inteiros positivos") from exc
        if file_id <= 0:
            raise ValueError("file_ids deve conter inteiros positivos")
        selected.append(file_id)
    if len(selected) != len(set(selected)):
        raise ValueError("file_ids nao pode conter duplicatas")
    return sorted(selected)


class LargeTransferConfirmationRequired(ValueError):
    def __init__(self, bytes_total: int) -> None:
        super().__init__(
            "transferencia acima de 10 GiB exige confirmacao explicita"
        )
        self.bytes_total = bytes_total


class PlaybackCapacityError(RuntimeError):
    """Capacidade temporaria esgotada antes de iniciar o playback."""


def _page_payload(page: Any) -> dict[str, Any]:
    payload = page.as_dict()
    payload["per_page"] = payload["page_size"]
    return payload


class ControlPlane:
    def __init__(self, settings: Settings) -> None:
        settings.validate_secrets()
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self.buffer = DynamicBufferController()
        self.internal_headers = {"Authorization": f"Bearer {settings.internal_token}"}

    def catalog(
        self,
        *,
        query: str,
        site: str,
        category: str,
        sort: str,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        conditions = ["video_count > 0"]
        parameters: list[Any] = []
        if query:
            conditions.append("(canonical_title ILIKE %s OR title ILIKE %s OR display_name ILIKE %s)")
            pattern = f"%{query}%"
            parameters.extend([pattern, pattern, pattern])
        if site:
            conditions.append("site=%s")
            parameters.append(site)
        if category:
            conditions.append("category=%s")
            parameters.append(category)
        order = {
            "popular": "seeders DESC NULLS LAST, peer_count DESC NULLS LAST, canonical_title",
            "rating": "imdb_rating DESC NULLS LAST, imdb_votes DESC NULLS LAST",
            "recent": "downloaded_at DESC NULLS LAST",
        }[sort]
        where = " AND ".join(conditions)
        with connection(self.settings) as database:
            total = database.execute(
                f"SELECT count(*) AS n FROM catalog.video_catalog WHERE {where}",
                parameters,
            ).fetchone()["n"]
            rows = database.execute(
                f"""
                SELECT * FROM catalog.video_catalog WHERE {where}
                ORDER BY {order} LIMIT %s OFFSET %s
                """,
                (*parameters, per_page, (page - 1) * per_page),
            ).fetchall()
        total_items = int(total)
        return {
            "items": [dict(row) for row in rows],
            "total": total_items,
            "page": page,
            "per_page": per_page,
            "pages": (total_items + per_page - 1) // per_page,
        }

    def detail(self, site: str, infohash: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            item = database.execute(
                "SELECT * FROM catalog.video_catalog WHERE site=%s AND infohash=%s",
                (site, infohash),
            ).fetchone()
            if item is None:
                raise KeyError(infohash)
            files = database.execute(
                """
                SELECT f.id,f.path,f.size,f.extension,f.file_kind,f.mime_type,
                       f.is_video,f.is_subtitle,
                       (drive_match.match_confidence IS NOT NULL) AS gdrive_present,
                       drive_match.match_confidence AS drive_match_confidence
                FROM catalog.torrent_files f
                JOIN catalog.torrents t ON t.id=f.torrent_id
                LEFT JOIN LATERAL (
                  SELECT CASE
                    WHEN drive_file.id=f.id THEN 'exact'
                    WHEN f.sha256 IS NOT NULL AND drive_file.sha256 IS NOT NULL
                     AND trim(f.sha256)=trim(drive_file.sha256) THEN 'exact'
                    ELSE 'possible'
                  END AS match_confidence
                  FROM catalog.drive_files drive
                  JOIN catalog.torrent_files drive_file
                    ON drive_file.id=drive.torrent_file_id
                  WHERE drive.active AND drive.can_download
                    AND drive_file.size=f.size
                    AND (
                      drive_file.id=f.id
                      OR (
                        f.sha256 IS NOT NULL AND drive_file.sha256 IS NOT NULL
                        AND trim(f.sha256)=trim(drive_file.sha256)
                      )
                      OR (
                        (f.sha256 IS NULL OR drive_file.sha256 IS NULL)
                        AND lower(regexp_replace(
                          regexp_replace(drive_file.path, '^.*/', ''),
                          '[^[:alnum:]]+', '', 'g'
                        ))=lower(regexp_replace(
                          regexp_replace(f.path, '^.*/', ''),
                          '[^[:alnum:]]+', '', 'g'
                        ))
                      )
                    )
                  ORDER BY CASE WHEN drive_file.id=f.id THEN 0 ELSE 1 END,
                           drive.updated_at DESC
                  LIMIT 1
                ) drive_match ON TRUE
                WHERE t.site=%s AND t.infohash=%s AND t.active
                  AND (
                    t.site <> 'gdrive'
                    OR EXISTS (
                      SELECT 1 FROM catalog.drive_files own_drive
                      WHERE own_drive.torrent_file_id=f.id
                        AND own_drive.active AND own_drive.can_download
                    )
                  )
                ORDER BY f.is_video DESC,f.size DESC,f.path
                LIMIT %s
                """,
                (site, infohash, MAX_DETAIL_FILES),
            ).fetchall()
            subtitles = database.execute(
                """
                SELECT torrent_path,language,file_name,status,provider,match_confidence,
                       extension,subtitle_path,synced_path
                FROM catalog.subtitles WHERE site=%s AND infohash=%s AND active
                ORDER BY language,file_name
                """,
                (site, infohash),
            ).fetchall()
        file_items = [dict(row) for row in files]
        subtitle_items: list[dict[str, Any]] = []
        for row in subtitles:
            subtitle = dict(row)
            subtitle["track_id"] = subtitle_track_id(
                site,
                infohash,
                str(subtitle["torrent_path"]),
                str(subtitle["language"]),
            )
            subtitle.pop("subtitle_path", None)
            subtitle.pop("synced_path", None)
            subtitle_items.append(subtitle)
        return {
            **dict(item),
            "files": file_items,
            "videos": [row for row in file_items if row.get("is_video")],
            "files_returned": len(file_items),
            "files_truncated": int(item.get("file_count") or 0) > len(file_items),
            "subtitles": subtitle_items,
        }

    def categories(self, site: str) -> list[dict[str, Any]]:
        conditions = ["video_count > 0", "category <> ''"]
        parameters: list[Any] = []
        if site:
            conditions.append("site=%s")
            parameters.append(site)
        with connection(self.settings) as database:
            rows = database.execute(
                f"""
                SELECT category,count(*) AS total FROM catalog.video_catalog
                WHERE {' AND '.join(conditions)}
                GROUP BY category ORDER BY category
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _audit(
        database: Any,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        correlation_id: uuid.UUID | None,
        details: Mapping[str, Any],
    ) -> None:
        database.execute(
            """
            INSERT INTO ops.audit_events(
              actor,action,entity_type,entity_id,correlation_id,details)
            VALUES('control',%s,%s,%s,%s,%s)
            """,
            (
                action,
                entity_type,
                entity_id,
                correlation_id,
                Jsonb(dict(details)),
            ),
        )

    def dashboard(self) -> dict[str, Any]:
        with connection(self.settings) as database:
            result = InventoryService(database).dashboard()
        return {
            **result,
            "titles": int(result.get("torrent_count") or 0),
            "files": int(result.get("file_count") or 0),
            "drive_files": int(result.get("gdrive_file_count") or 0),
            "active_transfers": int(result.get("active_transfer_count") or 0),
        }

    def files(
        self,
        *,
        q: str | None,
        site: str | None,
        kind: str | None,
        presence: str | None,
        page: int | str,
        per_page: int | str,
        source: str | None = None,
        status: str | None = None,
        group_by: str | None = None,
        view: str | None = None,
        infohash: str | None = None,
        origin_site: str | None = None,
    ) -> dict[str, Any]:
        with connection(self.settings) as database:
            selected = InventoryService(database).explorer(
                q=q,
                site=site,
                source=source,
                kind=kind,
                presence=presence,
                status=status,
                group_by=group_by,
                view=view,
                infohash=infohash,
                origin_site=origin_site,
                page=page,  # type: ignore[arg-type]
                page_size=per_page,  # type: ignore[arg-type]
            )
        payload = _page_payload(selected)
        if selected.view == "torrents":
            payload["total_torrents"] = payload["total"]
        for item in payload["items"]:
            if selected.view == "torrents":
                item.setdefault(
                    "id", f"{item.get('site', '')}:{item.get('infohash', '')}"
                )
            else:
                item["id"] = item.get("file_id")
        return payload

    def transfers(
        self,
        *,
        state: str | None,
        target: str | None,
        site: str | None,
        infohash: str | None,
        page: int | str,
        per_page: int | str,
    ) -> dict[str, Any]:
        with connection(self.settings) as database:
            selected = InventoryService(database).transfers(
                state=state,
                target=target,
                site=site,
                infohash=infohash,
                page=page,  # type: ignore[arg-type]
                page_size=per_page,  # type: ignore[arg-type]
            )
        return _page_payload(selected)

    def create_transfer(
        self,
        *,
        site: str,
        infohash: str,
        target: str,
        file_ids: Any,
        confirm_large: bool = False,
        destination_override: str | None = None,
        external_files: Any = None,
    ) -> dict[str, Any]:
        selected_site = site.strip().casefold()
        selected_target = target.strip().casefold()
        if selected_site not in CATALOG_SITES:
            raise ValueError("site invalido")
        if selected_target not in TRANSFER_TARGETS:
            raise ValueError("target invalido")
        if selected_site == "gdrive" and selected_target == "gdrive":
            raise ValueError("transferencia gdrive para gdrive nao e permitida")
        selected_infohash = normalized_infohash(infohash)
        selected_ids = _selected_file_ids(file_ids)
        job_id = uuid.uuid4()

        with connection(self.settings) as database:
            torrent = database.execute(
                """
                SELECT id,title,display_name,category
                FROM catalog.torrents
                WHERE site=%s AND infohash=%s AND active
                """,
                (selected_site, selected_infohash),
            ).fetchone()
            if torrent is None:
                raise KeyError(selected_infohash)
            file_rows = database.execute(
                """
                SELECT f.id,f.path,f.file_kind,f.mime_type,f.size,
                       d.drive_file_id,d.relative_path AS drive_relative_path,
                       d.mime_type AS drive_mime_type,d.md5_checksum,
                       d.source_record->>'sha256_checksum' AS drive_sha256_checksum,
                       d.can_download,d.active AS drive_active
                FROM catalog.torrent_files f
                LEFT JOIN catalog.drive_files d ON d.torrent_file_id=f.id
                WHERE f.torrent_id=%s AND f.id=ANY(%s::bigint[])
                """,
                (torrent["id"], selected_ids),
            ).fetchall()
            by_id = {int(row["id"]): dict(row) for row in file_rows}
            if set(by_id) != set(selected_ids):
                raise UnsafeMediaError(
                    "arquivo selecionado nao pertence ao torrent informado"
                )
            files = [by_id[file_id] for file_id in selected_ids]
            kinds = {str(row.get("file_kind") or "other") for row in files}
            if not kinds.issubset(FILE_KINDS):
                raise UnsafeMediaError("tipo de arquivo inventariado invalido")
            if len(kinds) != 1:
                raise ValueError(
                    "selecione arquivos de um unico tipo; agrupe a transferencia por tipo"
                )
            destination_kind = next(iter(kinds))
            category = str(torrent.get("category") or "sem-categoria")
            title = str(
                torrent.get("title")
                or torrent.get("display_name")
                or selected_infohash
            )
            destination_path = (
                safe_destination_path(str(destination_override))
                if destination_override
                else safe_destination_path(destination_kind, category, title)
            )
            selected_external: list[dict[str, Any]] = []
            if external_files is not None:
                if not isinstance(external_files, list) or len(external_files) > MAX_TRANSFER_FILES:
                    raise ValueError("external_files excede o limite permitido")
                subtitle_root = self.settings.subtitle_file_root.resolve(strict=True)
                seen_paths: set[str] = set()
                seen_names: set[str] = set()
                for value in external_files:
                    if not isinstance(value, Mapping):
                        raise UnsafeMediaError("manifesto de legenda externa invalido")
                    source = Path(str(value.get("local_path") or "")).resolve(strict=True)
                    try:
                        source.relative_to(subtitle_root)
                    except ValueError as exc:
                        raise UnsafeMediaError("legenda externa fora do cofre") from exc
                    if not source.is_file():
                        raise UnsafeMediaError("legenda externa indisponivel")
                    relative = safe_destination_path(str(value.get("relative_path") or source.name))
                    source_key = str(source).casefold()
                    relative_key = relative.casefold()
                    if source_key in seen_paths or relative_key in seen_names:
                        raise UnsafeMediaError("legenda externa duplicada no manifesto")
                    seen_paths.add(source_key)
                    seen_names.add(relative_key)
                    selected_external.append(
                        {
                            "external_id": str(value.get("external_id") or ""),
                            "local_path": str(source),
                            "relative_path": relative,
                            "size": source.stat().st_size,
                            "mime_type": str(
                                value.get("mime_type")
                                or mimetypes.guess_type(source.name)[0]
                                or "application/octet-stream"
                            ),
                        }
                    )
            bytes_total = sum(int(row.get("size") or 0) for row in files) + sum(
                int(row["size"]) for row in selected_external
            )
            if bytes_total >= LARGE_TRANSFER_BYTES and confirm_large is not True:
                raise LargeTransferConfirmationRequired(bytes_total)
            drive_files: list[dict[str, Any]] = []
            if selected_site == "gdrive":
                for row in files:
                    if (
                        not row.get("drive_file_id")
                        or not row.get("drive_active")
                        or not row.get("can_download")
                    ):
                        raise UnsafeMediaError(
                            "arquivo Google Drive indisponivel para download"
                        )
                    relative_path = str(
                        row.get("drive_relative_path") or row.get("path") or ""
                    )
                    drive_files.append(
                        {
                            "drive_file_id": str(row["drive_file_id"]),
                            "relative_path": relative_path,
                            "size": int(row.get("size") or 0),
                            "mime_type": row.get("drive_mime_type")
                            or row.get("mime_type"),
                            "md5_checksum": row.get("md5_checksum"),
                            "sha256_checksum": row.get(
                                "drive_sha256_checksum"
                            ),
                        }
                    )
            idempotency_key = ":".join(
                (
                    selected_site,
                    selected_infohash,
                    selected_target,
                    ",".join(str(file_id) for file_id in selected_ids),
                    ",".join(
                        str(value.get("external_id") or value["relative_path"])
                        for value in selected_external
                    ),
                )
            )
            database.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (idempotency_key,),
            )
            existing = database.execute(
                """
                SELECT id::text AS id,source_site,trim(infohash) AS infohash,
                       target,state,cardinality(selected_file_ids) AS file_count,
                       destination_path,bytes_total,bytes_done,error
                FROM runtime.transfer_jobs
                WHERE source_site=%s AND infohash=%s AND target=%s
                  AND selected_file_ids=%s::bigint[] AND destination_path=%s
                  AND external_files=%s::jsonb
                  AND state NOT IN ('failed','cancelled')
                  AND (state <> 'completed'
                       OR finished_at > now() - interval '5 minutes')
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    selected_site,
                    selected_infohash,
                    selected_target,
                    selected_ids,
                    destination_path,
                    Jsonb(selected_external),
                ),
            ).fetchone()
            if existing is not None:
                database.commit()
                duplicate = dict(existing)
                duplicate["id"] = str(duplicate["id"])
                duplicate["deduplicated"] = True
                return duplicate
            database.execute(
                """
                INSERT INTO runtime.transfer_jobs(
                  id,source_site,infohash,target,state,selected_file_ids,
                  destination_path,bytes_total,bytes_done,external_files,drive_files)
                VALUES(%s,%s,%s,%s,'queued',%s,%s,%s,0,%s,%s)
                """,
                (
                    job_id,
                    selected_site,
                    selected_infohash,
                    selected_target,
                    selected_ids,
                    destination_path,
                    bytes_total,
                    Jsonb(selected_external),
                    Jsonb(drive_files),
                ),
            )
            self._audit(
                database,
                action="transfer.created",
                entity_type="transfer_job",
                entity_id=str(job_id),
                correlation_id=job_id,
                details={
                    "site": selected_site,
                    "infohash": selected_infohash,
                    "target": selected_target,
                    "file_ids": selected_ids,
                    "destination_path": destination_path,
                    "bytes_total": bytes_total,
                    "external_file_count": len(selected_external),
                },
            )
            database.commit()

        result = {
            "id": str(job_id),
            "source_site": selected_site,
            "infohash": selected_infohash,
            "target": selected_target,
            "state": "queued",
            "file_count": len(selected_ids),
            "external_file_count": len(selected_external),
            "destination_path": destination_path,
            "bytes_total": bytes_total,
            "bytes_done": 0,
            "deduplicated": False,
        }
        if selected_site == "gdrive":
            return result

        try:
            response = requests.post(
                f"{self.settings.torrent_engine_url}/internal/materializations",
                headers=self.internal_headers,
                json={"job_id": str(job_id)},
                timeout=30,
            )
            if response.status_code == 429:
                with connection(self.settings) as database:
                    self._audit(
                        database,
                        action="transfer.dispatch_deferred",
                        entity_type="transfer_job",
                        entity_id=str(job_id),
                        correlation_id=job_id,
                        details={"reason": "torrent_capacity"},
                    )
                    database.commit()
                result["deferred"] = True
                return result
            response.raise_for_status()
        except requests.RequestException as exc:
            message = f"torrent-engine indisponivel: {exc}"[:2000]
            try:
                with connection(self.settings) as database:
                    database.execute(
                        """
                        UPDATE runtime.transfer_jobs
                        SET state='failed',error=%s WHERE id=%s
                        """,
                        (message, job_id),
                    )
                    self._audit(
                        database,
                        action="transfer.dispatch_failed",
                        entity_type="transfer_job",
                        entity_id=str(job_id),
                        correlation_id=job_id,
                        details={"error": message},
                    )
                    database.commit()
            except Exception:
                LOG.exception("falha ao persistir erro do job %s", job_id)
            raise RuntimeError("nao foi possivel iniciar a transferencia") from exc
        return result

    def curation_media(
        self,
        *,
        query: str | None,
        media_kind: str | None,
        subtitles: str | None,
        availability: str | None,
        page: int | str,
        per_page: int | str,
    ) -> dict[str, Any]:
        with connection(self.settings) as database:
            selected = CurationService(database).list_media(
                query=query,
                media_kind=media_kind,
                subtitles=subtitles,
                availability=availability,
                page=page,
                page_size=per_page,
            )
        return selected.as_dict()

    def _curation_plan(self, site: str, infohash: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            plan = CurationService(database).publication_plan(site, infohash)
        external_manifest: list[dict[str, Any]] = []
        for subtitle in plan["external_subtitles"]:
            stored = str(subtitle.get("synced_path") or subtitle.get("subtitle_path") or "")
            selected = resolve_subtitle_path(
                stored,
                mounted_root=self.settings.subtitle_file_root,
                host_root=self.settings.subtitle_host_root,
            )
            identity = subtitle_track_id(
                plan["site"],
                plan["infohash"],
                str(subtitle.get("torrent_path") or ""),
                str(subtitle.get("language") or ""),
            )
            language = re.sub(
                r"[^A-Za-z0-9-]+", "-", str(subtitle.get("language") or "und")
            ).strip("-") or "und"
            name = f"{selected.stem}.{language}.{identity[:8]}{selected.suffix.casefold()}"
            external_manifest.append(
                {
                    "external_id": identity,
                    "local_path": str(selected),
                    "relative_path": safe_destination_path("Subtitles", name),
                    "size": selected.stat().st_size,
                    "mime_type": mimetypes.guess_type(selected.name)[0]
                    or "application/octet-stream",
                }
            )
        if len(external_manifest) > MAX_TRANSFER_FILES:
            raise ValueError(
                f"titulo possui mais de {MAX_TRANSFER_FILES} legendas externas; refine por temporada"
            )
        plan["external_manifest"] = external_manifest
        plan["bytes_total"] = int(plan.get("bytes_total") or 0) + sum(
            int(value["size"]) for value in external_manifest
        )
        return plan

    def curation_preview(self, site: str, infohash: str) -> dict[str, Any]:
        plan = self._curation_plan(site, infohash)
        return {
            "site": plan["site"],
            "infohash": plan["infohash"],
            "title": plan["title"],
            "media_kind": plan["media_kind"],
            "destination_path": plan["destination_path"],
            "drive_path": f"#Avideos/{plan['destination_path']}",
            "video_count": len(plan["video_files"]),
            "embedded_subtitle_count": len(plan["embedded_subtitle_files"]),
            "external_subtitle_count": len(plan["external_manifest"]),
            "bytes_total": plan["bytes_total"],
            "confirmation_required": True,
            "large_confirmation_required": plan["bytes_total"] >= LARGE_TRANSFER_BYTES,
            "automatic_download": False,
        }

    def publish_curation(
        self,
        *,
        site: str,
        infohash: str,
        confirmed: bool,
        confirm_large: bool,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("a publicacao exige confirmacao explicita")
        plan = self._curation_plan(site, infohash)
        if plan["bytes_total"] >= LARGE_TRANSFER_BYTES and confirm_large is not True:
            raise LargeTransferConfirmationRequired(plan["bytes_total"])

        groups: list[tuple[list[int], list[dict[str, Any]]]] = []
        video_ids = [int(value["id"]) for value in plan["video_files"]]
        subtitle_ids = [int(value["id"]) for value in plan["embedded_subtitle_files"]]
        for start in range(0, len(video_ids), MAX_TRANSFER_FILES):
            groups.append((video_ids[start : start + MAX_TRANSFER_FILES], []))
        for start in range(0, len(subtitle_ids), MAX_TRANSFER_FILES):
            groups.append((subtitle_ids[start : start + MAX_TRANSFER_FILES], []))
        if not groups:
            raise ValueError("nenhum arquivo elegivel para publicar")
        groups[0] = (groups[0][0], list(plan["external_manifest"]))

        jobs: list[dict[str, Any]] = []
        for file_ids, external in groups:
            jobs.append(
                self.create_transfer(
                    site=plan["site"],
                    infohash=plan["infohash"],
                    target="gdrive",
                    file_ids=file_ids,
                    confirm_large=confirm_large,
                    destination_override=plan["destination_path"],
                    external_files=external,
                )
            )
        return {
            "accepted": True,
            "site": plan["site"],
            "infohash": plan["infohash"],
            "title": plan["title"],
            "destination_path": plan["destination_path"],
            "drive_path": f"#Avideos/{plan['destination_path']}",
            "bytes_total": plan["bytes_total"],
            "jobs": jobs,
        }

    def sync_drive(self) -> dict[str, Any]:
        correlation_id = uuid.uuid4()
        with connection(self.settings) as database:
            self._audit(
                database,
                action="drive.sync_requested",
                entity_type="drive_catalog",
                entity_id="gdrive",
                correlation_id=correlation_id,
                details={},
            )
            database.commit()
        try:
            response = requests.post(
                f"{self.settings.drive_source_url}/internal/sync",
                headers=self.internal_headers,
                timeout=30,
            )
            if response.status_code == 409:
                return {"accepted": False, "status": "already_syncing"}
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("nao foi possivel sincronizar o Google Drive") from exc
        return dict(payload) if isinstance(payload, Mapping) else {"result": payload}

    def provider_url(self, site: str) -> str:
        return (
            self.settings.drive_source_url
            if site == "gdrive"
            else self.settings.torrent_engine_url
        )

    def create_playback(
        self,
        *,
        site: str,
        infohash: str,
        file_id: int,
        mode: str,
        quality_cap_bps: int,
    ) -> dict[str, str]:
        if site not in CATALOG_SITES or mode not in {"auto", "adaptive"}:
            raise ValueError("parametros invalidos")
        infohash = normalized_infohash(infohash)
        with connection(self.settings) as database:
            selected = database.execute(
                """
                SELECT f.id FROM catalog.torrent_files f
                JOIN catalog.torrents t ON t.id=f.torrent_id
                WHERE f.id=%s AND t.site=%s AND t.infohash=%s
                  AND t.active AND f.is_video
                  AND (
                    t.site <> 'gdrive'
                    OR EXISTS (
                      SELECT 1 FROM catalog.drive_files d
                      WHERE d.torrent_file_id=f.id
                        AND d.active AND d.can_download
                    )
                  )
                """,
                (file_id, site, infohash),
            ).fetchone()
            if selected is None:
                raise UnsafeMediaError("arquivo nao aprovado")
        session_uuid = uuid.uuid4()
        session_id = session_uuid.hex
        token = secrets.token_urlsafe(32)
        download_id = uuid.uuid4()
        transcode_id = uuid.uuid4()
        with connection(self.settings) as database:
            database.execute(
                """
                INSERT INTO runtime.playback_sessions(
                  id,site,infohash,torrent_file_id,token_hash,state,selected_profile)
                VALUES(%s,%s,%s,%s,%s,'starting',%s)
                """,
                (
                    session_uuid,
                    site,
                    infohash,
                    file_id,
                    token_digest(token, self.settings.session_pepper),
                    mode,
                ),
            )
            database.execute(
                "INSERT INTO runtime.download_jobs(id,session_id,state,storage_key) VALUES(%s,%s,'queued',%s)",
                (download_id, session_uuid, f"{site}-{infohash}"),
            )
            database.execute(
                """
                INSERT INTO runtime.transcode_jobs(id,session_id,strategy,state)
                VALUES(%s,%s,'pending','queued')
                """,
                (transcode_id, session_uuid),
            )
            database.commit()
        try:
            source_response = requests.post(
                f"{self.provider_url(site)}/internal/sessions",
                headers=self.internal_headers,
                json={"session_id": session_id},
                timeout=30,
            )
            if source_response.status_code == 429:
                raise PlaybackCapacityError(
                    "capacidade de torrents esgotada; tente novamente depois"
                )
            source_response.raise_for_status()
            transcode_response = requests.post(
                f"{self.settings.transcoder_url}/internal/transcodes",
                headers=self.internal_headers,
                json={
                    "session_id": session_id,
                    "token": token,
                    "mode": mode,
                    "quality_cap_bps": max(0, quality_cap_bps),
                },
                timeout=30,
            )
            if transcode_response.status_code == 429:
                raise PlaybackCapacityError(
                    "capacidade de transcodificacao esgotada; tente novamente depois"
                )
            transcode_response.raise_for_status()
        except (PlaybackCapacityError, requests.RequestException) as exc:
            # A origem pode ter sido aberta antes de o transcoder recusar o
            # trabalho. Encerrar ambos os lados evita reter handle de torrent,
            # token e slot ate o TTL da sessao.
            self._stop_workers(session_id, site)
            with connection(self.settings) as database:
                database.execute(
                    """
                    UPDATE runtime.playback_sessions
                    SET state='closed',error=%s,closed_at=now(),updated_at=now()
                    WHERE id=%s
                    """,
                    (f"servico interno: {exc}", session_uuid),
                )
                database.execute(
                    "UPDATE runtime.download_jobs SET state='closed',updated_at=now() WHERE session_id=%s",
                    (session_uuid,),
                )
                database.execute(
                    """
                    UPDATE runtime.transcode_jobs
                    SET state='closed',finished_at=COALESCE(finished_at,now()),
                        updated_at=now()
                    WHERE session_id=%s
                    """,
                    (session_uuid,),
                )
                database.commit()
            if isinstance(exc, PlaybackCapacityError):
                raise
            raise RuntimeError("nao foi possivel iniciar os workers") from exc
        return {"id": session_id, "token": token, "status_url": f"/api/playback/{session_id}?token={token}"}

    def authenticate(self, session_id: str, token: str) -> dict[str, Any]:
        session_id = normalized_session_id(session_id)
        ttl = int(getattr(self.settings, "playback_ttl_seconds", 43_200))
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT *,id::text AS uuid_text FROM runtime.playback_sessions
                WHERE id=%s AND closed_at IS NULL
                  AND created_at >= now()-make_interval(secs=>%s)
                """,
                (session_id, ttl),
            ).fetchone()
        if row is None or not token_matches(token, self.settings.session_pepper, str(row["token_hash"])):
            raise PermissionError("token invalido")
        return dict(row)

    def subtitle_webvtt(
        self, session_id: str, token: str, requested_track_id: str
    ) -> str:
        session = self.authenticate(session_id, token)
        selected_track = requested_track_id.strip().casefold()
        if not SUBTITLE_TRACK_RE.fullmatch(selected_track):
            raise KeyError(requested_track_id)
        site = str(session["site"]).strip().casefold()
        infohash = normalized_infohash(str(session["infohash"]))
        with connection(self.settings) as database:
            rows = database.execute(
                """
                SELECT torrent_path,language,extension,subtitle_path,synced_path
                FROM catalog.subtitles
                WHERE site=%s AND infohash=%s AND active
                """,
                (site, infohash),
            ).fetchall()
        subtitle: dict[str, Any] | None = None
        for row in rows:
            candidate = dict(row)
            candidate_id = subtitle_track_id(
                site,
                infohash,
                str(candidate["torrent_path"]),
                str(candidate["language"]),
            )
            if secrets.compare_digest(candidate_id, selected_track):
                subtitle = candidate
                break
        if subtitle is None:
            raise KeyError(requested_track_id)

        stored_paths = [
            str(value)
            for value in (subtitle.get("synced_path"), subtitle.get("subtitle_path"))
            if value
        ]
        last_error: Exception | None = None
        for stored_path in dict.fromkeys(stored_paths):
            try:
                resolved = resolve_subtitle_path(
                    stored_path,
                    mounted_root=self.settings.subtitle_file_root,
                    host_root=self.settings.subtitle_host_root,
                )
                extension = str(subtitle.get("extension") or resolved.suffix)
                return to_webvtt(resolved.read_bytes(), extension)
            except (OSError, ValueError) as exc:
                last_error = exc
        raise FileNotFoundError("legenda indisponivel") from last_error

    def playback_status(self, session_id: str, token: str) -> dict[str, Any]:
        session = self.authenticate(session_id, token)
        with connection(self.settings) as database:
            download = database.execute(
                "SELECT state,metrics,error FROM runtime.download_jobs WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            transcode = database.execute(
                "SELECT strategy,encoder,profiles,state,error FROM runtime.transcode_jobs WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            artifact = database.execute(
                """
                SELECT storage_key FROM runtime.stream_artifacts
                WHERE session_id=%s AND relative_path='master.m3u8' AND ready
                """,
                (session_id,),
            ).fetchone()
        metrics = dict(download["metrics"] or {}) if download else {}
        bitrate = int(session.get("target_bitrate") or session.get("source_bitrate") or 3_000_000)
        verified = int(metrics.get("verified_buffer_bytes") or 0)
        buffered = verified * 8 / max(bitrate, 1)
        rate = int(metrics.get("download_bps") or 0)
        history_key = f"rates:{session_id}"
        pipeline = self.redis.pipeline()
        pipeline.lpush(history_key, rate)
        pipeline.ltrim(history_key, 0, 9)
        pipeline.expire(history_key, 3600)
        pipeline.execute()
        rates = [int(value) for value in self.redis.lrange(history_key, 0, 9)]
        mean = sum(rates) / len(rates) if rates else 0
        jitter = (
            min(1.0, math.sqrt(sum((value - mean) ** 2 for value in rates) / len(rates)) / mean)
            if mean > 0 and rates
            else 0.0
        )
        decision = self.buffer.decide(
            download_bps=rate,
            rendition_bps=bitrate,
            jitter=jitter,
            buffered_seconds=buffered,
        )
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE runtime.playback_sessions SET buffer_target_seconds=%s,
                  verified_buffer_bytes=%s,updated_at=now() WHERE id=%s
                """,
                (decision.target_seconds, verified, session_id),
            )
            database.commit()
        stream_url = None
        if artifact:
            stream_url = (
                f"/stream/{session_id}/{token}/{artifact['storage_key']}/master.m3u8"
            )
        return {
            "id": session_id,
            "state": session["state"],
            "strategy": session.get("strategy"),
            "error": session.get("error"),
            "stream_url": stream_url,
            "source_url": (
                f"/source/{session_id}/{token}" if session["site"] != "gdrive" else None
            ),
            "torrent": metrics,
            "transcode": dict(transcode) if transcode else None,
            "buffered_seconds_estimate": round(buffered, 1),
            "buffer": decision.to_dict(),
            "media": session.get("media_probe"),
        }

    def _stop_workers(self, session_id: str, site: str) -> None:
        for base, path in (
            (self.settings.transcoder_url, f"/internal/transcodes/{session_id}"),
            (self.provider_url(site), f"/internal/sessions/{session_id}"),
        ):
            try:
                requests.delete(base + path, headers=self.internal_headers, timeout=10)
            except requests.RequestException:
                pass

    def expire_sessions(self) -> int:
        ttl = int(getattr(self.settings, "playback_ttl_seconds", 43_200))
        with connection(self.settings) as database:
            rows = database.execute(
                """
                WITH candidate AS (
                  SELECT id FROM runtime.playback_sessions
                  WHERE closed_at IS NULL
                    AND created_at < now()-make_interval(secs=>%s)
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED LIMIT 100
                )
                UPDATE runtime.playback_sessions session
                SET state='closed',closed_at=now(),updated_at=now(),
                    error=COALESCE(session.error,'sessao expirada por TTL')
                FROM candidate
                WHERE session.id=candidate.id
                RETURNING session.id::text AS session_id,session.site
                """,
                (ttl,),
            ).fetchall()
            session_ids = [row["session_id"] for row in rows]
            if session_ids:
                database.execute(
                    """
                    UPDATE runtime.download_jobs SET state='closed',updated_at=now()
                    WHERE session_id=ANY(%s::uuid[])
                    """,
                    (session_ids,),
                )
                database.execute(
                    """
                    UPDATE runtime.transcode_jobs
                    SET state='closed',finished_at=COALESCE(finished_at,now()),
                        updated_at=now()
                    WHERE session_id=ANY(%s::uuid[])
                    """,
                    (session_ids,),
                )
            database.commit()
        for row in rows:
            self._stop_workers(str(row["session_id"]), str(row["site"]))
        return len(rows)

    def close(self, session_id: str, token: str) -> None:
        session = self.authenticate(session_id, token)
        self._stop_workers(session_id, str(session["site"]))
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE runtime.playback_sessions SET state='closed',closed_at=now(),
                  updated_at=now() WHERE id=%s
                """,
                (session_id,),
            )
            database.commit()

    def services(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, url in (
            ("torrent-engine", self.settings.torrent_engine_url),
            ("gdrive-source", self.settings.drive_source_url),
            ("transcoder", self.settings.transcoder_url),
        ):
            started = time.monotonic()
            try:
                response = requests.get(f"{url}/health", timeout=3)
                result[name] = {
                    "healthy": response.ok,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "details": response.json(),
                }
            except requests.RequestException as exc:
                result[name] = {"healthy": False, "error": str(exc)}
        try:
            result["redis"] = {"healthy": bool(self.redis.ping())}
        except Exception as exc:
            result["redis"] = {"healthy": False, "error": str(exc)}
        with connection(self.settings) as database:
            database.execute("SELECT 1")
            heartbeats = database.execute(
                "SELECT service,status,details,updated_at FROM ops.service_heartbeats ORDER BY service"
            ).fetchall()
        result["postgres"] = {"healthy": True}
        result["heartbeats"] = [dict(row) for row in heartbeats]
        return result


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    plane = ControlPlane(settings)
    start_heartbeat(
        "control", lambda: {"expired_sessions": plane.expire_sessions()}
    )
    app = Flask(__name__)
    app.config["plane"] = plane

    @app.before_request
    def same_origin_mutations() -> Any:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if not request.path.startswith("/api/"):
            return None
        fetch_site = request.headers.get("Sec-Fetch-Site", "").casefold()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return jsonify({"error": "origem nao autorizada"}), 403
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify({"error": "origem nao autorizada"}), 403
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health() -> Response:
        try:
            with connection(settings) as database:
                database.execute("SELECT 1")
            return jsonify({"status": "ok", "version": __version__})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503

    @app.get("/")
    def index() -> str:
        return render_template("index.html", version=__version__)

    @app.get("/vendor/hls.mjs")
    def hls_vendor() -> Response:
        if not settings.vendor_hls_path.is_file():
            return jsonify({"error": "hls.js indisponivel"}), 503
        return send_file(settings.vendor_hls_path, mimetype="text/javascript")

    @app.get("/api/dashboard")
    def dashboard() -> Response:
        return jsonify(plane.dashboard())

    @app.get("/api/curation/media")
    def curation_media() -> Response:
        try:
            result = plane.curation_media(
                query=request.args.get("q"),
                media_kind=request.args.get("media_kind"),
                subtitles=request.args.get("subtitles"),
                availability=request.args.get("availability"),
                page=request.args.get("page", "1"),
                per_page=request.args.get("per_page", "24"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.get("/api/curation/media/<site>/<infohash>/preview")
    def curation_preview(site: str, infohash: str) -> Response:
        try:
            result = plane.curation_preview(site, normalized_infohash(infohash))
        except KeyError:
            return jsonify({"error": "titulo ausente"}), 404
        except (ValueError, UnsafeMediaError, OSError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(result)

    @app.post("/api/curation/media/<site>/<infohash>/publish")
    def curation_publish(site: str, infohash: str) -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            payload = {}
        try:
            result = plane.publish_curation(
                site=site,
                infohash=normalized_infohash(infohash),
                confirmed=payload.get("confirm") is True,
                confirm_large=payload.get("confirm_large") is True,
            )
        except KeyError:
            return jsonify({"error": "titulo ausente"}), 404
        except LargeTransferConfirmationRequired as exc:
            return (
                jsonify(
                    {
                        "error": str(exc),
                        "confirmation_required": True,
                        "bytes_total": exc.bytes_total,
                        "threshold_bytes": LARGE_TRANSFER_BYTES,
                    }
                ),
                409,
            )
        except (ValueError, UnsafeMediaError, OSError) as exc:
            return jsonify({"error": str(exc)}), 422
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(result), 201

    @app.get("/api/files")
    def files() -> Response:
        try:
            result = plane.files(
                q=request.args.get("q"),
                site=request.args.get("site"),
                source=request.args.get("source"),
                kind=request.args.get("kind"),
                presence=request.args.get("presence"),
                status=request.args.get("status"),
                group_by=request.args.get("group_by"),
                view=request.args.get("view"),
                infohash=request.args.get("infohash"),
                origin_site=request.args.get("origin_site"),
                page=request.args.get("page", "1"),
                per_page=request.args.get("per_page", "50"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.get("/api/transfers")
    def transfers() -> Response:
        try:
            result = plane.transfers(
                state=request.args.get("state"),
                target=request.args.get("target"),
                site=request.args.get("site"),
                infohash=request.args.get("infohash"),
                page=request.args.get("page", "1"),
                per_page=request.args.get("per_page", "50"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/transfers")
    def create_transfer() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            payload = {}
        try:
            result = plane.create_transfer(
                site=str(payload.get("site") or ""),
                infohash=str(payload.get("infohash") or ""),
                target=str(payload.get("target") or ""),
                file_ids=payload.get("file_ids"),
                confirm_large=payload.get("confirm_large") is True,
            )
        except KeyError:
            return jsonify({"error": "torrent ausente"}), 404
        except LargeTransferConfirmationRequired as exc:
            return (
                jsonify(
                    {
                        "error": str(exc),
                        "confirmation_required": True,
                        "bytes_total": exc.bytes_total,
                        "threshold_bytes": LARGE_TRANSFER_BYTES,
                    }
                ),
                409,
            )
        except (ValueError, UnsafeMediaError) as exc:
            return jsonify({"error": str(exc)}), 422
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(result), 200 if result.get("deduplicated") else 201

    @app.post("/api/drive/sync")
    def sync_drive() -> Response:
        try:
            return jsonify(plane.sync_drive())
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.get("/api/catalog")
    def catalog() -> Response:
        site = request.args.get("site", "")
        sort = request.args.get("sort", "popular")
        if site not in ({""} | CATALOG_SITES) or sort not in {"popular", "rating", "recent"}:
            return jsonify({"error": "filtro invalido"}), 400
        try:
            page = max(1, int(request.args.get("page", 1)))
            per_page = min(100, max(1, int(request.args.get("per_page", 30))))
        except (TypeError, ValueError):
            return jsonify({"error": "paginacao invalida"}), 400
        return jsonify(
            plane.catalog(
                query=request.args.get("q", "")[:200],
                site=site,
                category=request.args.get("category", "")[:100],
                sort=sort,
                page=page,
                per_page=per_page,
            )
        )

    @app.get("/api/categories")
    def categories() -> Response:
        site = request.args.get("site", "")
        if site not in ({""} | CATALOG_SITES):
            return jsonify({"error": "filtro invalido"}), 400
        return jsonify({"items": plane.categories(site)})

    @app.get("/api/catalog/<site>/<infohash>")
    def detail(site: str, infohash: str) -> Response:
        try:
            return jsonify(plane.detail(site, normalized_infohash(infohash)))
        except (KeyError, UnsafeMediaError):
            return jsonify({"error": "titulo ausente"}), 404

    @app.post("/api/playback")
    def playback() -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            payload = {}
        try:
            result = plane.create_playback(
                site=str(payload.get("site", "")),
                infohash=str(payload.get("infohash", "")),
                file_id=int(payload.get("file_id")),
                mode=str(payload.get("mode", "adaptive")),
                quality_cap_bps=int(payload.get("quality_cap_bps") or 0),
            )
        except (TypeError, ValueError, UnsafeMediaError) as exc:
            return jsonify({"error": str(exc)}), 422
        except PlaybackCapacityError as exc:
            return jsonify({"error": str(exc), "retryable": True}), 429
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(result), 201

    @app.get("/api/playback/<session_id>")
    def playback_status(session_id: str) -> Response:
        try:
            return jsonify(plane.playback_status(session_id, request.args.get("token", "")))
        except (PermissionError, UnsafeMediaError):
            return jsonify({"error": "sessao invalida"}), 403

    @app.get("/api/playback/<session_id>/subtitles/<track_id>.vtt")
    def playback_subtitle(session_id: str, track_id: str) -> Response:
        try:
            payload = plane.subtitle_webvtt(
                session_id, request.args.get("token", ""), track_id
            )
        except (PermissionError, UnsafeMediaError):
            return jsonify({"error": "sessao invalida"}), 403
        except (KeyError, FileNotFoundError):
            return jsonify({"error": "legenda indisponivel"}), 404
        return Response(payload, content_type="text/vtt; charset=utf-8")

    @app.delete("/api/playback/<session_id>")
    def close_playback(session_id: str) -> Response:
        try:
            plane.close(session_id, request.args.get("token", ""))
        except (PermissionError, UnsafeMediaError):
            return jsonify({"error": "sessao invalida"}), 403
        return jsonify({"closed": True})

    @app.post("/api/playback/<session_id>/close")
    def close_playback_beacon(session_id: str) -> Response:
        try:
            plane.close(session_id, request.args.get("token", ""))
        except (PermissionError, UnsafeMediaError):
            return jsonify({"error": "sessao invalida"}), 403
        return Response(status=204)

    @app.get("/api/services")
    def services() -> Response:
        return jsonify(plane.services())

    @app.get("/internal/authorize")
    def authorize_stream() -> Response:
        original = request.headers.get("X-Original-URI", "")
        match = STREAM_RE.match(original)
        if not match:
            return Response(status=403)
        try:
            plane.authenticate(match["session"], match["token"])
            with connection(settings) as database:
                artifact = database.execute(
                    """
                    SELECT 1 FROM runtime.stream_artifacts
                    WHERE session_id=%s AND storage_key=%s AND ready
                    """,
                    (match["session"], match["storage"]),
                ).fetchone()
            return Response(status=204 if artifact else 403)
        except (PermissionError, UnsafeMediaError):
            return Response(status=403)

    return app


def main() -> None:
    create_app().run(host="0.0.0.0", port=7100, threaded=True)


if __name__ == "__main__":
    main()
