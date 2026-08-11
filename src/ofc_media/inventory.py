from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .file_kinds import FILE_KINDS


SITES = frozenset({"filecr", "1337x", "gdrive"})
TRANSFER_STATES = frozenset(
    {
        "queued",
        "validating",
        "downloading",
        "downloaded",
        "classifying",
        "uploading",
        "verifying",
        "completed",
        "failed",
        "cancelled",
    }
)
TRANSFER_TARGETS = frozenset({"local", "gdrive"})
PRESENCE_FILTERS = frozenset(
    {"local", "gdrive", "not_gdrive", "both", "missing", "exact", "possible"}
)


DASHBOARD_SQL = r"""
WITH visible_files AS (
    SELECT f.id,f.file_kind,f.size
    FROM catalog.torrent_files f
    JOIN catalog.torrents t ON t.id=f.torrent_id
    WHERE t.active
      AND (
          t.site <> 'gdrive'
          OR EXISTS (
              SELECT 1 FROM catalog.drive_files d
              WHERE d.torrent_file_id=f.id AND d.active AND d.can_download
          )
      )
), local_presence AS (
    SELECT DISTINCT selected.file_id
    FROM runtime.transfer_jobs job
    CROSS JOIN LATERAL unnest(job.selected_file_ids) selected(file_id)
    JOIN visible_files f ON f.id=selected.file_id
    WHERE job.state='completed' AND jsonb_array_length(job.local_files) > 0
), kind_counts AS (
    SELECT file_kind, count(*) AS file_count, COALESCE(sum(size), 0) AS bytes_total
    FROM visible_files
    GROUP BY file_kind
), transfer_counts AS (
    SELECT state, count(*) AS job_count
    FROM runtime.transfer_jobs
    GROUP BY state
)
SELECT
    (SELECT count(*) FROM catalog.torrents WHERE active) AS torrent_count,
    (SELECT count(*) FROM visible_files) AS file_count,
    (SELECT COALESCE(sum(size), 0) FROM visible_files) AS bytes_total,
    (SELECT count(*) FROM catalog.drive_files WHERE active AND can_download)
        AS gdrive_file_count,
    (SELECT count(*) FROM local_presence) AS local_file_count,
    (SELECT count(*) FROM catalog.subtitles WHERE active) AS subtitle_count,
    (SELECT count(*) FROM runtime.transfer_jobs
     WHERE state NOT IN ('completed','failed','cancelled')) AS active_transfer_count,
    COALESCE(
        (SELECT jsonb_object_agg(file_kind, jsonb_build_object(
                    'count', file_count, 'bytes', bytes_total
                )) FROM kind_counts),
        '{}'::jsonb
    ) AS files_by_kind,
    COALESCE(
        (SELECT jsonb_object_agg(state, job_count) FROM transfer_counts),
        '{}'::jsonb
    ) AS transfers_by_state
"""


EXPLORER_SQL = r"""
WITH candidate_files AS (
    SELECT
        f.id AS file_id,
        f.torrent_id,
        f.file_index,
        t.site,
        trim(t.infohash) AS infohash,
        t.title,
        t.display_name,
        t.category,
        f.path,
        regexp_replace(f.path, '^.*/', '') AS file_name,
        lower(regexp_replace(regexp_replace(f.path, '^.*/', ''),
                             '[^[:alnum:]]+', '', 'g')) AS normalized_name,
        f.extension,
        f.file_kind,
        f.mime_type,
        f.size,
        f.is_video,
        f.is_subtitle,
        trim(f.sha256) AS sha256
    FROM catalog.torrent_files f
    JOIN catalog.torrents t ON t.id=f.torrent_id
    WHERE t.active
      AND (
          t.site <> 'gdrive'
          OR EXISTS (
              SELECT 1 FROM catalog.drive_files own_drive
              WHERE own_drive.torrent_file_id=f.id
                AND own_drive.active AND own_drive.can_download
          )
      )
      AND (%(site)s IS NULL OR t.site=%(site)s)
      AND (%(kind)s IS NULL OR f.file_kind=%(kind)s)
      AND (
          %(q)s IS NULL
          OR t.title ILIKE %(q)s ESCAPE E'\\'
          OR t.display_name ILIKE %(q)s ESCAPE E'\\'
          OR f.path ILIKE %(q)s ESCAPE E'\\'
          OR trim(t.infohash) ILIKE %(q)s ESCAPE E'\\'
      )
), matched AS (
    SELECT
        source_file.*,
        EXISTS (
            SELECT 1
            FROM runtime.transfer_jobs local_job
            WHERE local_job.target='local'
              AND local_job.state='completed'
              AND source_file.file_id=ANY(local_job.selected_file_ids)
        ) AS local_present,
        drive_match.drive_file_id,
        drive_match.relative_path AS drive_relative_path,
        drive_match.match_confidence AS drive_match_confidence
    FROM candidate_files source_file
    LEFT JOIN LATERAL (
        SELECT
            drive.drive_file_id,
            drive.relative_path,
            CASE
                WHEN drive_file.id=source_file.file_id THEN 'exact'
                WHEN source_file.sha256 IS NOT NULL
                 AND drive_file.sha256 IS NOT NULL
                 AND trim(drive_file.sha256)=source_file.sha256
                 AND drive_file.size=source_file.size THEN 'exact'
                ELSE 'possible'
            END AS match_confidence
        FROM catalog.drive_files drive
        JOIN catalog.torrent_files drive_file ON drive_file.id=drive.torrent_file_id
        WHERE drive.active
          AND drive_file.size=source_file.size
          AND (
              drive_file.id=source_file.file_id
              OR (
                  source_file.sha256 IS NOT NULL
                  AND drive_file.sha256 IS NOT NULL
                  AND trim(drive_file.sha256)=source_file.sha256
              )
              OR (
                  (source_file.sha256 IS NULL OR drive_file.sha256 IS NULL)
                  AND lower(regexp_replace(
                          regexp_replace(drive_file.path, '^.*/', ''),
                          '[^[:alnum:]]+', '', 'g'
                      ))=source_file.normalized_name
              )
          )
        ORDER BY
            CASE
                WHEN drive_file.id=source_file.file_id THEN 0
                WHEN source_file.sha256 IS NOT NULL
                 AND drive_file.sha256 IS NOT NULL
                 AND trim(drive_file.sha256)=source_file.sha256 THEN 1
                ELSE 2
            END,
            drive.updated_at DESC,
            drive.drive_file_id
        LIMIT 1
    ) drive_match ON TRUE
), with_presence AS (
    SELECT
        matched.*,
        (drive_file_id IS NOT NULL) AS gdrive_present,
        CASE
            WHEN local_present AND drive_file_id IS NOT NULL THEN 'both'
            WHEN local_present THEN 'local'
            WHEN drive_file_id IS NOT NULL THEN 'gdrive'
            ELSE 'missing'
        END AS presence,
        CASE
            WHEN drive_file_id IS NOT NULL THEN drive_match_confidence
            WHEN local_present THEN 'exact'
            ELSE 'none'
        END AS presence_confidence
    FROM matched
), selected AS (
    SELECT *
    FROM with_presence
    WHERE
        %(presence)s IS NULL
        OR (%(presence)s='local' AND local_present)
        OR (%(presence)s='gdrive' AND drive_file_id IS NOT NULL)
        OR (%(presence)s='not_gdrive' AND drive_file_id IS NULL)
        OR (%(presence)s='both' AND local_present AND drive_file_id IS NOT NULL)
        OR (%(presence)s='missing' AND NOT local_present AND drive_file_id IS NULL)
        OR (%(presence)s IN ('exact','possible') AND presence_confidence=%(presence)s)
)
SELECT
    (SELECT count(*) FROM selected) AS total_count,
    COALESCE(
        (
            SELECT jsonb_agg(to_jsonb(page_rows)
                             ORDER BY page_rows.site, page_rows.title,
                                      page_rows.path, page_rows.file_id)
            FROM (
                SELECT *
                FROM selected
                ORDER BY site, title, path, file_id
                LIMIT %(limit)s OFFSET %(offset)s
            ) page_rows
        ),
        '[]'::jsonb
    ) AS items
"""


TRANSFERS_SQL = r"""
WITH selected AS (
    SELECT
        id,
        source_site,
        trim(infohash) AS infohash,
        target,
        state,
        cardinality(selected_file_ids) AS file_count,
        destination_path,
        bytes_total,
        bytes_done,
        jsonb_array_length(local_files) AS local_file_count,
        jsonb_array_length(drive_files) AS drive_file_count,
        CASE
            WHEN error IS NULL THEN NULL
            ELSE left(
                regexp_replace(error, 'https?://[^[:space:]]+', '[URL redigida]', 'g'),
                500
            )
        END AS error,
        created_at,
        updated_at,
        started_at,
        finished_at
    FROM runtime.transfer_jobs
    WHERE (%(state)s IS NULL OR state=%(state)s)
      AND (%(target)s IS NULL OR target=%(target)s)
      AND (%(site)s IS NULL OR source_site=%(site)s)
      AND (%(infohash)s IS NULL OR infohash=%(infohash)s)
)
SELECT
    (SELECT count(*) FROM selected) AS total_count,
    COALESCE(
        (
            SELECT jsonb_agg(to_jsonb(page_rows)
                             ORDER BY page_rows.created_at DESC, page_rows.id DESC)
            FROM (
                SELECT * FROM selected
                ORDER BY created_at DESC, id DESC
                LIMIT %(limit)s OFFSET %(offset)s
            ) page_rows
        ),
        '[]'::jsonb
    ) AS items
"""


@dataclass(frozen=True, slots=True)
class Page:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": (self.total + self.page_size - 1) // self.page_size,
        }


def _validated_optional(value: str | None, allowed: Sequence[str] | set[str], name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if not normalized or normalized == "all":
        return None
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} invalido; use um de: {choices}")
    return normalized


def _pagination(page: int, page_size: int) -> tuple[int, int, int, int]:
    if isinstance(page, bool) or isinstance(page_size, bool):
        raise ValueError("paginacao deve usar numeros inteiros")
    try:
        selected_page = int(page)
        selected_size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("paginacao deve usar numeros inteiros") from exc
    if selected_page < 1:
        raise ValueError("page deve ser maior ou igual a 1")
    if not 1 <= selected_size <= 200:
        raise ValueError("page_size deve estar entre 1 e 200")
    return selected_page, selected_size, selected_size, (selected_page - 1) * selected_size


def _like_pattern(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError("InventoryService requer conexao com row_factory=dict_row")


def _json_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError("resultado items deve ser uma lista JSON")
    return [dict(item) for item in value]


class InventoryService:
    """Read-only inventory queries independent from the HTTP framework.

    ``database`` is a psycopg connection configured with ``dict_row`` (the
    project's default). All user-controlled values are passed separately from
    the static SQL strings.
    """

    def __init__(self, database: Any) -> None:
        self.database = database

    def dashboard(self) -> dict[str, Any]:
        row = _mapping(self.database.execute(DASHBOARD_SQL).fetchone())
        return row

    def explorer(
        self,
        *,
        q: str | None = None,
        site: str | None = None,
        kind: str | None = None,
        presence: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        selected_page, selected_size, limit, offset = _pagination(page, page_size)
        params = {
            "q": _like_pattern(q),
            "site": _validated_optional(site, SITES, "site"),
            "kind": _validated_optional(kind, set(FILE_KINDS), "kind"),
            "presence": _validated_optional(presence, PRESENCE_FILTERS, "presence"),
            "limit": limit,
            "offset": offset,
        }
        row = _mapping(self.database.execute(EXPLORER_SQL, params).fetchone())
        return Page(
            items=_json_list(row.get("items")),
            total=int(row.get("total_count") or 0),
            page=selected_page,
            page_size=selected_size,
        )

    def transfers(
        self,
        *,
        state: str | None = None,
        target: str | None = None,
        site: str | None = None,
        infohash: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        selected_page, selected_size, limit, offset = _pagination(page, page_size)
        selected_infohash: str | None = None
        if infohash is not None and infohash.strip():
            selected_infohash = infohash.strip().casefold()
            if len(selected_infohash) != 40 or any(
                char not in "0123456789abcdef" for char in selected_infohash
            ):
                raise ValueError("infohash deve conter 40 caracteres hexadecimais")
        params = {
            "state": _validated_optional(state, TRANSFER_STATES, "state"),
            "target": _validated_optional(target, TRANSFER_TARGETS, "target"),
            "site": _validated_optional(site, SITES, "site"),
            "infohash": selected_infohash,
            "limit": limit,
            "offset": offset,
        }
        row = _mapping(self.database.execute(TRANSFERS_SQL, params).fetchone())
        return Page(
            items=_json_list(row.get("items")),
            total=int(row.get("total_count") or 0),
            page=selected_page,
            page_size=selected_size,
        )

    # Nome explicito para consumidores que preferem uma API verbal.
    list_transfers = transfers
