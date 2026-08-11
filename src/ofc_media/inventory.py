from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .file_kinds import FILE_KINDS


SITES = frozenset({"filecr", "1337x", "gdrive"})
EXPLORER_SITES = SITES | {"torrent"}
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
WITH torrent_source_sites(site) AS (
    VALUES ('filecr'::text), ('1337x'::text)
), source_torrents AS MATERIALIZED (
    SELECT t.id,t.site
    FROM catalog.torrents t
    WHERE t.active AND t.site IN ('filecr','1337x')
), source_files AS MATERIALIZED (
    SELECT f.id,t.site,f.file_kind,f.size
    FROM catalog.torrent_files f
    JOIN source_torrents t ON t.id=f.torrent_id
), drive_presence AS MATERIALIZED (
    SELECT d.drive_file_id,f.id AS file_id,f.torrent_id,f.file_kind,f.size
    FROM catalog.drive_files d
    JOIN catalog.torrent_files f ON f.id=d.torrent_file_id
    JOIN catalog.torrents t ON t.id=f.torrent_id
    WHERE d.active AND d.can_download AND t.active AND t.site='gdrive'
), visible_files AS (
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
    SELECT DISTINCT f.id AS file_id,f.torrent_id,f.file_kind,f.size
    FROM runtime.transfer_jobs job
    CROSS JOIN LATERAL unnest(job.selected_file_ids) selected(file_id)
    JOIN catalog.torrent_files f ON f.id=selected.file_id
    WHERE job.state='completed'
      AND jsonb_array_length(job.local_files) > 0
), torrent_source_stats AS (
    SELECT
        sites.site,
        (SELECT count(*) FROM source_torrents t WHERE t.site=sites.site)
            AS torrent_count,
        (SELECT count(*) FROM source_files f WHERE f.site=sites.site)
            AS file_count,
        (SELECT COALESCE(sum(f.size), 0) FROM source_files f
         WHERE f.site=sites.site) AS bytes_total
    FROM torrent_source_sites sites
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
    (SELECT count(*) FROM source_torrents) AS source_torrent_count,
    (SELECT count(*) FROM source_files) AS source_file_count,
    (SELECT COALESCE(sum(size), 0) FROM source_files) AS source_bytes_total,
    (SELECT count(*) FROM drive_presence) AS gdrive_file_count,
    (SELECT COALESCE(sum(size), 0) FROM drive_presence) AS gdrive_bytes_total,
    (SELECT count(DISTINCT torrent_id) FROM drive_presence) AS gdrive_title_count,
    (SELECT count(*) FROM local_presence) AS local_file_count,
    (SELECT COALESCE(sum(size), 0) FROM local_presence) AS local_bytes_total,
    (SELECT count(DISTINCT torrent_id) FROM local_presence) AS local_title_count,
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
    ) AS transfers_by_state,
    COALESCE(
        (SELECT jsonb_object_agg(site, jsonb_build_object(
                    'titles', torrent_count,
                    'files', file_count,
                    'bytes', bytes_total
                ) ORDER BY site) FROM torrent_source_stats),
        '{}'::jsonb
    ) AS torrent_sources_by_site
"""


EXPLORER_SQL = r"""
WITH candidate_files AS NOT MATERIALIZED (
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
      AND (
          CAST(%(site)s AS text) IS NULL
          OR t.site=CAST(%(site)s AS text)
          OR (CAST(%(site)s AS text)='torrent'
              AND t.site IN ('filecr','1337x'))
      )
      AND (CAST(%(kind)s AS text) IS NULL OR f.file_kind=CAST(%(kind)s AS text))
      AND (
          CAST(%(q)s AS text) IS NULL
          OR t.title ILIKE CAST(%(q)s AS text) ESCAPE E'\\'
          OR t.display_name ILIKE CAST(%(q)s AS text) ESCAPE E'\\'
          OR f.path ILIKE CAST(%(q)s AS text) ESCAPE E'\\'
          OR trim(t.infohash) ILIKE CAST(%(q)s AS text) ESCAPE E'\\'
      )
), drive_inventory AS MATERIALIZED (
    SELECT
        drive.drive_file_id,
        drive.torrent_file_id AS drive_torrent_file_id,
        drive.relative_path,
        drive.updated_at,
        drive_file.size,
        trim(drive_file.sha256) AS sha256,
        lower(regexp_replace(regexp_replace(drive_file.path, '^.*/', ''),
                             '[^[:alnum:]]+', '', 'g')) AS normalized_name
    FROM catalog.drive_files drive
    JOIN catalog.torrent_files drive_file
      ON drive_file.id=drive.torrent_file_id
    WHERE drive.active AND drive.can_download
), drive_matches AS (
    SELECT
        source_file.file_id AS source_file_id,
        drive_file.drive_file_id,
        drive_file.relative_path,
        'exact'::text AS match_confidence,
        0 AS match_priority,
        drive_file.updated_at
    FROM drive_inventory drive_file
    JOIN candidate_files source_file
      ON source_file.file_id=drive_file.drive_torrent_file_id

    UNION ALL

    SELECT
        source_file.file_id AS source_file_id,
        drive_file.drive_file_id,
        drive_file.relative_path,
        'exact'::text AS match_confidence,
        1 AS match_priority,
        drive_file.updated_at
    FROM drive_inventory drive_file
    JOIN candidate_files source_file
      ON source_file.sha256 IS NOT NULL
     AND drive_file.sha256 IS NOT NULL
     AND drive_file.sha256=source_file.sha256
     AND drive_file.size=source_file.size
     AND drive_file.drive_torrent_file_id<>source_file.file_id

    UNION ALL

    SELECT
        source_file.file_id AS source_file_id,
        drive_file.drive_file_id,
        drive_file.relative_path,
        'possible'::text AS match_confidence,
        2 AS match_priority,
        drive_file.updated_at
    FROM drive_inventory drive_file
    JOIN candidate_files source_file
      ON drive_file.size=source_file.size
     AND drive_file.normalized_name=source_file.normalized_name
     AND (source_file.sha256 IS NULL OR drive_file.sha256 IS NULL)
     AND drive_file.drive_torrent_file_id<>source_file.file_id
), best_drive_match AS (
    SELECT DISTINCT ON (source_file_id)
        source_file_id,
        drive_file_id,
        relative_path,
        match_confidence
    FROM drive_matches
    ORDER BY source_file_id, match_priority, updated_at DESC, drive_file_id
), local_presence AS MATERIALIZED (
    SELECT DISTINCT selected.file_id
    FROM runtime.transfer_jobs local_job
    CROSS JOIN LATERAL unnest(local_job.selected_file_ids) selected(file_id)
    WHERE local_job.state='completed'
      AND jsonb_array_length(local_job.local_files) > 0
), matched AS (
    SELECT
        source_file.*,
        (local_file.file_id IS NOT NULL) AS local_present,
        drive_match.drive_file_id,
        drive_match.relative_path AS drive_relative_path,
        drive_match.match_confidence AS drive_match_confidence
    FROM candidate_files source_file
    LEFT JOIN local_presence local_file ON local_file.file_id=source_file.file_id
    LEFT JOIN best_drive_match drive_match
      ON drive_match.source_file_id=source_file.file_id
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
        CAST(%(presence)s AS text) IS NULL
        OR (CAST(%(presence)s AS text)='local' AND local_present)
        OR (CAST(%(presence)s AS text)='gdrive' AND drive_file_id IS NOT NULL)
        OR (CAST(%(presence)s AS text)='not_gdrive' AND drive_file_id IS NULL)
        OR (CAST(%(presence)s AS text)='both' AND local_present AND drive_file_id IS NOT NULL)
        OR (CAST(%(presence)s AS text)='missing' AND NOT local_present AND drive_file_id IS NULL)
        OR (CAST(%(presence)s AS text) IN ('exact','possible')
            AND presence_confidence=CAST(%(presence)s AS text))
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
    WHERE (CAST(%(state)s AS text) IS NULL OR state=CAST(%(state)s AS text))
      AND (CAST(%(target)s AS text) IS NULL OR target=CAST(%(target)s AS text))
      AND (CAST(%(site)s AS text) IS NULL OR source_site=CAST(%(site)s AS text))
      AND (CAST(%(infohash)s AS text) IS NULL OR infohash=CAST(%(infohash)s AS text))
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


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError(f"resultado {name} deve ser um objeto JSON")
    return dict(value)


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
        sources = _json_object(
            row.get("torrent_sources_by_site"), "torrent_sources_by_site"
        )
        row["torrent_sources_by_site"] = sources
        # Proveniencia e presenca sao dimensoes diferentes: um arquivo de
        # torrent pode estar simultaneamente local e no Drive. Manter os
        # dominios separados evita que consumidores somem copias fisicas ao
        # inventario de origem e inflem um suposto total global.
        row["domains"] = {
            "torrent": {
                "titles": int(row.get("source_torrent_count") or 0),
                "files": int(row.get("source_file_count") or 0),
                "bytes": int(row.get("source_bytes_total") or 0),
                "sources": sources,
            },
            "local": {
                "titles": int(row.get("local_title_count") or 0),
                "files": int(row.get("local_file_count") or 0),
                "bytes": int(row.get("local_bytes_total") or 0),
            },
            "gdrive": {
                "titles": int(row.get("gdrive_title_count") or 0),
                "files": int(row.get("gdrive_file_count") or 0),
                "bytes": int(row.get("gdrive_bytes_total") or 0),
            },
        }
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
            "site": _validated_optional(site, EXPLORER_SITES, "site"),
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
