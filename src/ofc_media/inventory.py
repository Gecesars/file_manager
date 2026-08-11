from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .file_kinds import FILE_KINDS


SITES = frozenset({"filecr", "1337x", "gdrive"})
SOURCE_CARDS = ("gdrive", "filecr", "1337x", "local")
EXPLORER_SITES = SITES | {"torrent", "local"}
EXPLORER_STATUSES = frozenset({"available", "cataloged"})
EXPLORER_GROUPS = frozenset({"type", "status", "source", "presence", "location"})
EXPLORER_VIEWS = frozenset({"files", "torrents"})
SOURCE_METADATA: dict[str, tuple[str, str, str]] = {
    "gdrive": ("Google Drive", "Google Drive", "gdrive"),
    "filecr": ("FileCR", "Catalogo FileCR", "torrent"),
    "1337x": ("1337x", "Catalogo 1337x", "torrent"),
    "local": ("Local", "Armazenamento local", "local"),
}
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
    CROSS JOIN LATERAL unnest(job.selected_file_ids)
         WITH ORDINALITY AS selected(file_id, ordinal)
    JOIN LATERAL jsonb_array_elements(job.local_files)
         WITH ORDINALITY AS local_manifest(value, ordinal)
      ON local_manifest.value->>'file_id'=selected.file_id::text
      OR (
          local_manifest.value->>'file_id' IS NULL
          AND local_manifest.ordinal=selected.ordinal
      )
    JOIN catalog.torrent_files f ON f.id=selected.file_id
    WHERE job.state='completed' AND job.target='local'
      AND jsonb_array_length(job.local_files) > 0
      AND NULLIF(local_manifest.value->>'local_path','') IS NOT NULL
      AND right(
          replace(local_manifest.value->>'local_path', E'\\', '/'),
          length('/' || job.destination_path || '/' || COALESCE(
              local_manifest.value->>'relative_path',
              local_manifest.value->>'path'
          ))
      ) = '/' || job.destination_path || '/' || COALESCE(
          local_manifest.value->>'relative_path',
          local_manifest.value->>'path'
      )
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
), source_kind_counts AS (
    SELECT site AS source,file_kind,count(*) AS file_count,
           COALESCE(sum(size), 0) AS bytes_total
    FROM source_files
    GROUP BY site,file_kind

    UNION ALL

    SELECT 'gdrive'::text AS source,file_kind,count(*) AS file_count,
           COALESCE(sum(size), 0) AS bytes_total
    FROM drive_presence
    GROUP BY file_kind

    UNION ALL

    SELECT 'local'::text AS source,file_kind,count(*) AS file_count,
           COALESCE(sum(size), 0) AS bytes_total
    FROM local_presence
    GROUP BY file_kind
), source_type_objects AS (
    SELECT source,jsonb_object_agg(
        file_kind,
        jsonb_build_object('count', file_count, 'bytes', bytes_total)
        ORDER BY file_kind
    ) AS types
    FROM source_kind_counts
    GROUP BY source
), transfer_counts AS (
    SELECT state, count(*) AS job_count
    FROM runtime.transfer_jobs
    GROUP BY state
), source_transfer_counts AS (
    SELECT source,state,count(*) AS job_count
    FROM (
        SELECT source_site AS source,state FROM runtime.transfer_jobs
        UNION ALL
        SELECT target AS source,state FROM runtime.transfer_jobs
        WHERE target IN ('local','gdrive')
    ) transfers
    GROUP BY source,state
), source_transfer_objects AS (
    SELECT source,jsonb_object_agg(state, job_count ORDER BY state) AS states
    FROM source_transfer_counts
    GROUP BY source
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
    ) AS torrent_sources_by_site,
    COALESCE(
        (SELECT jsonb_object_agg(source, types ORDER BY source)
         FROM source_type_objects),
        '{}'::jsonb
    ) AS source_types_by_source,
    COALESCE(
        (SELECT jsonb_object_agg(source, states ORDER BY source)
         FROM source_transfer_objects),
        '{}'::jsonb
    ) AS source_transfers_by_source
"""


_EXPLORER_SELECTED_SQL = r"""
WITH completed_local_files AS MATERIALIZED (
    SELECT DISTINCT selected.file_id
    FROM runtime.transfer_jobs local_job
    CROSS JOIN LATERAL unnest(local_job.selected_file_ids)
         WITH ORDINALITY AS selected(file_id, ordinal)
    JOIN LATERAL jsonb_array_elements(local_job.local_files)
         WITH ORDINALITY AS local_manifest(value, ordinal)
      ON local_manifest.value->>'file_id'=selected.file_id::text
      OR (
          local_manifest.value->>'file_id' IS NULL
          AND local_manifest.ordinal=selected.ordinal
      )
    WHERE CAST(%(site)s AS text)='local'
      AND local_job.state='completed' AND local_job.target='local'
      AND jsonb_array_length(local_job.local_files) > 0
      AND NULLIF(local_manifest.value->>'local_path','') IS NOT NULL
      AND right(
          replace(local_manifest.value->>'local_path', E'\\', '/'),
          length('/' || local_job.destination_path || '/' || COALESCE(
              local_manifest.value->>'relative_path',
              local_manifest.value->>'path'
          ))
      ) = '/' || local_job.destination_path || '/' || COALESCE(
          local_manifest.value->>'relative_path',
          local_manifest.value->>'path'
      )
), torrent_file_totals AS NOT MATERIALIZED (
    SELECT f.torrent_id,t.site,count(*) AS total_files,
           COALESCE(sum(f.size), 0) AS total_bytes
    FROM catalog.torrent_files f
    JOIN catalog.torrents t ON t.id=f.torrent_id
    LEFT JOIN completed_local_files persistent_local
      ON persistent_local.file_id=f.id
    WHERE (
          t.active
          OR (CAST(%(site)s AS text)='local'
              AND persistent_local.file_id IS NOT NULL)
      )
      AND (
          t.site <> 'gdrive'
          OR EXISTS (
              SELECT 1 FROM catalog.drive_files own_drive
              WHERE own_drive.torrent_file_id=f.id
                AND own_drive.active AND own_drive.can_download
          )
          OR (CAST(%(site)s AS text)='local'
              AND persistent_local.file_id IS NOT NULL)
      )
      AND (
          CAST(%(site)s AS text) IS NULL
          OR t.site=CAST(%(site)s AS text)
          OR (CAST(%(site)s AS text)='torrent'
              AND t.site IN ('filecr','1337x'))
          OR (CAST(%(site)s AS text)='local'
              AND persistent_local.file_id IS NOT NULL)
      )
      AND (
          CAST(%(origin_site)s AS text) IS NULL
          OR t.site=CAST(%(origin_site)s AS text)
      )
      AND (
          CAST(%(infohash)s AS text) IS NULL
          OR lower(trim(t.infohash))=CAST(%(infohash)s AS text)
      )
    GROUP BY f.torrent_id,t.site
), candidate_files AS NOT MATERIALIZED (
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
    LEFT JOIN completed_local_files persistent_local
      ON persistent_local.file_id=f.id
    WHERE (
          t.active
          OR (CAST(%(site)s AS text)='local'
              AND persistent_local.file_id IS NOT NULL)
      )
      AND (
          t.site <> 'gdrive'
          OR EXISTS (
              SELECT 1 FROM catalog.drive_files own_drive
              WHERE own_drive.torrent_file_id=f.id
                AND own_drive.active AND own_drive.can_download
          )
          OR (CAST(%(site)s AS text)='local'
              AND persistent_local.file_id IS NOT NULL)
      )
      AND (
          CAST(%(site)s AS text) IS NULL
          OR t.site=CAST(%(site)s AS text)
          OR (CAST(%(site)s AS text)='torrent'
              AND t.site IN ('filecr','1337x'))
          OR CAST(%(site)s AS text)='local'
      )
      AND (
          CAST(%(origin_site)s AS text) IS NULL
          OR t.site=CAST(%(origin_site)s AS text)
      )
      AND (
          CAST(%(infohash)s AS text) IS NULL
          OR lower(trim(t.infohash))=CAST(%(infohash)s AS text)
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
    SELECT DISTINCT ON (selected.file_id)
        selected.file_id,
        COALESCE(local_manifest.value->>'relative_path',
                 local_manifest.value->>'path') AS relative_path,
        local_job.destination_path,
        local_job.updated_at
    FROM runtime.transfer_jobs local_job
    CROSS JOIN LATERAL unnest(local_job.selected_file_ids)
         WITH ORDINALITY AS selected(file_id, ordinal)
    JOIN candidate_files local_candidate ON local_candidate.file_id=selected.file_id
    JOIN LATERAL jsonb_array_elements(local_job.local_files)
         WITH ORDINALITY AS local_manifest(value, ordinal)
      ON local_manifest.value->>'file_id'=selected.file_id::text
      OR (
          local_manifest.value->>'file_id' IS NULL
          AND local_manifest.ordinal=selected.ordinal
      )
    WHERE local_job.state='completed' AND local_job.target='local'
      AND jsonb_array_length(local_job.local_files) > 0
      AND NULLIF(local_manifest.value->>'local_path','') IS NOT NULL
      AND right(
          replace(local_manifest.value->>'local_path', E'\\', '/'),
          length('/' || local_job.destination_path || '/' || COALESCE(
              local_manifest.value->>'relative_path',
              local_manifest.value->>'path'
          ))
      ) = '/' || local_job.destination_path || '/' || COALESCE(
          local_manifest.value->>'relative_path',
          local_manifest.value->>'path'
      )
    ORDER BY selected.file_id,local_job.updated_at DESC,local_job.id DESC
), matched AS (
    SELECT
        source_file.*,
        (local_file.file_id IS NOT NULL) AS local_present,
        local_file.relative_path AS local_relative_path,
        local_file.destination_path AS local_destination_path,
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
        (drive_file_id IS NOT NULL AND drive_match_confidence='exact') AS gdrive_present,
        CASE
            WHEN local_present AND drive_file_id IS NOT NULL
                 AND drive_match_confidence='exact' THEN 'both'
            WHEN local_present THEN 'local'
            WHEN drive_file_id IS NOT NULL AND drive_match_confidence='exact'
                THEN 'gdrive'
            ELSE 'missing'
        END AS presence,
        CASE
            WHEN drive_file_id IS NOT NULL THEN drive_match_confidence
            WHEN local_present THEN 'exact'
            ELSE 'none'
        END AS presence_confidence
    FROM matched
), enriched AS (
    SELECT
        with_presence.*,
        file_kind AS type,
        CASE
            WHEN CAST(%(site)s AS text)='local' THEN 'local'
            ELSE site
        END AS source,
        CASE
            WHEN local_present OR (
                drive_file_id IS NOT NULL AND drive_match_confidence='exact'
            ) THEN 'available'
            ELSE 'cataloged'
        END AS status,
        CASE
            WHEN CAST(%(site)s AS text)='local' THEN 'local'
            WHEN site='gdrive' THEN 'gdrive'
            ELSE 'torrent'
        END AS location_kind,
        CASE
            WHEN CAST(%(site)s AS text)='local'
                THEN COALESCE(local_relative_path, path)
            WHEN site='gdrive'
                THEN COALESCE(drive_relative_path, path)
            ELSE path
        END AS location,
        CASE
            WHEN local_present AND drive_file_id IS NOT NULL
                 AND drive_match_confidence='exact' THEN 'both'
            WHEN local_present THEN 'local'
            WHEN drive_file_id IS NOT NULL AND drive_match_confidence='exact'
                THEN 'gdrive'
            ELSE 'torrent'
        END AS location_group,
        (
            CASE WHEN site IN ('filecr','1337x')
                THEN jsonb_build_array(jsonb_build_object(
                    'source', site,
                    'location_kind', 'torrent',
                    'path', path,
                    'status', 'cataloged'
                ))
                ELSE '[]'::jsonb
            END
            || CASE WHEN drive_file_id IS NOT NULL
                THEN jsonb_build_array(jsonb_build_object(
                    'source', 'gdrive',
                    'location_kind', 'gdrive',
                    'path', COALESCE(drive_relative_path, path),
                    'status', CASE drive_match_confidence
                        WHEN 'exact' THEN 'available'
                        ELSE 'possible'
                    END,
                    'confidence', drive_match_confidence,
                    'drive_file_id', drive_file_id
                ))
                ELSE '[]'::jsonb
            END
            || CASE WHEN local_present
                THEN jsonb_build_array(jsonb_build_object(
                    'source', 'local',
                    'location_kind', 'local',
                    'path', COALESCE(local_relative_path, path),
                    'destination_path', local_destination_path,
                    'status', 'available'
                ))
                ELSE '[]'::jsonb
            END
        ) AS locations
    FROM with_presence
), selected AS (
    SELECT *
    FROM enriched
    WHERE
        (CAST(%(site)s AS text) IS NULL
         OR CAST(%(site)s AS text)<>'local'
         OR local_present)
        AND (
            CAST(%(presence)s AS text) IS NULL
            OR (CAST(%(presence)s AS text)='local' AND local_present)
            OR (CAST(%(presence)s AS text)='gdrive'
                AND drive_file_id IS NOT NULL AND drive_match_confidence='exact')
            OR (CAST(%(presence)s AS text)='not_gdrive'
                AND (drive_file_id IS NULL OR drive_match_confidence<>'exact'))
            OR (CAST(%(presence)s AS text)='both' AND local_present
                AND drive_file_id IS NOT NULL AND drive_match_confidence='exact')
            OR (CAST(%(presence)s AS text)='missing' AND NOT local_present
                AND (drive_file_id IS NULL OR drive_match_confidence<>'exact'))
            OR (CAST(%(presence)s AS text) IN ('exact','possible')
                AND presence_confidence=CAST(%(presence)s AS text))
        )
        AND (CAST(%(status)s AS text) IS NULL
             OR status=CAST(%(status)s AS text))
)
"""


EXPLORER_SQL = _EXPLORER_SELECTED_SQL + r"""
, groupable AS (
    SELECT
        CASE CAST(%(group_by)s AS text)
            WHEN 'type' THEN type
            WHEN 'status' THEN status
            WHEN 'source' THEN source
            WHEN 'presence' THEN presence
            WHEN 'location' THEN location_group
            ELSE NULL
        END AS group_key,
        size
    FROM selected
), grouped AS (
    SELECT group_key,count(*) AS file_count,COALESCE(sum(size), 0) AS bytes_total
    FROM groupable
    WHERE group_key IS NOT NULL
    GROUP BY group_key
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
    ) AS items,
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'key', group_key,
                    'label', group_key,
                    'count', file_count,
                    'files', file_count,
                    'bytes', bytes_total
                ) ORDER BY group_key
            )
            FROM grouped
        ),
        '[]'::jsonb
    ) AS groups
"""


TORRENT_EXPLORER_SQL = _EXPLORER_SELECTED_SQL + r"""
, torrent_keys AS (
    SELECT torrent_id,site,infohash,title,display_name,category
    FROM selected
    GROUP BY torrent_id,site,infohash,title,display_name,category
), paged_torrents AS (
    SELECT *
    FROM torrent_keys
    ORDER BY site,title,infohash,torrent_id
    LIMIT %(limit)s OFFSET %(offset)s
), torrent_page_files AS (
    SELECT selected.*
    FROM selected
    JOIN paged_torrents USING (torrent_id,site)
), torrent_type_counts AS (
    SELECT torrent_id,site,file_kind,count(*) AS file_count,
           COALESCE(sum(size), 0) AS bytes_total
    FROM torrent_page_files
    GROUP BY torrent_id,site,file_kind
), torrent_types AS (
    SELECT torrent_id,site,jsonb_object_agg(
        file_kind,
        jsonb_build_object(
            'count', file_count,
            'files', file_count,
            'bytes', bytes_total
        ) ORDER BY file_kind
    ) AS types
    FROM torrent_type_counts
    GROUP BY torrent_id,site
), torrent_status_counts AS (
    SELECT torrent_id,site,status,count(*) AS file_count
    FROM torrent_page_files
    GROUP BY torrent_id,site,status
), torrent_statuses AS (
    SELECT torrent_id,site,jsonb_object_agg(status, file_count ORDER BY status)
           AS status_counts
    FROM torrent_status_counts
    GROUP BY torrent_id,site
), torrent_presence_counts AS (
    SELECT torrent_id,site,presence,count(*) AS file_count
    FROM torrent_page_files
    GROUP BY torrent_id,site,presence
), torrent_presences AS (
    SELECT torrent_id,site,jsonb_object_agg(presence, file_count ORDER BY presence)
           AS presence_counts
    FROM torrent_presence_counts
    GROUP BY torrent_id,site
), torrent_location_counts AS (
    SELECT torrent_id,site,location_group,count(*) AS file_count
    FROM torrent_page_files
    GROUP BY torrent_id,site,location_group
), torrent_locations AS (
    SELECT torrent_id,site,jsonb_object_agg(
        location_group, file_count ORDER BY location_group
    ) AS location_counts
    FROM torrent_location_counts
    GROUP BY torrent_id,site
), torrent_rows AS (
    SELECT
        page_file.torrent_id,
        page_file.site,
        min(page_file.source) AS source,
        page_file.infohash,
        page_file.title,
        page_file.display_name,
        page_file.category,
        count(*) AS file_count,
        count(*) AS matched_file_count,
        max(torrent_file_totals.total_files) AS total_files,
        COALESCE(sum(page_file.size), 0) AS bytes,
        COALESCE(sum(page_file.size), 0) AS matched_bytes,
        max(torrent_file_totals.total_bytes) AS total_bytes,
        COALESCE(sum(page_file.size), 0) AS size,
        count(*) FILTER (WHERE page_file.status='available')
            AS available_file_count,
        count(*) FILTER (WHERE page_file.status='cataloged')
            AS cataloged_file_count,
        count(*) FILTER (WHERE page_file.local_present) AS local_file_count,
        count(*) FILTER (
            WHERE page_file.drive_file_id IS NOT NULL
              AND page_file.drive_match_confidence='exact'
        ) AS gdrive_file_count,
        CASE
            WHEN count(DISTINCT page_file.file_kind)=1 THEN min(page_file.file_kind)
            ELSE 'mixed'
        END AS type,
        CASE
            WHEN count(DISTINCT page_file.file_kind)=1 THEN min(page_file.file_kind)
            ELSE 'mixed'
        END AS file_kind,
        CASE
            WHEN count(*) FILTER (WHERE page_file.status='available')=count(*)
                THEN 'available'
            WHEN count(*) FILTER (WHERE page_file.status='cataloged')=count(*)
                THEN 'cataloged'
            ELSE 'partial'
        END AS status,
        CASE
            WHEN count(DISTINCT page_file.presence)=1 THEN min(page_file.presence)
            ELSE 'mixed'
        END AS presence,
        CASE
            WHEN count(DISTINCT page_file.location_group)=1
                THEN min(page_file.location_group)
            ELSE 'mixed'
        END AS location_group,
        CASE
            WHEN count(DISTINCT page_file.location_kind)=1
                THEN min(page_file.location_kind)
            ELSE 'mixed'
        END AS location_kind,
        torrent_types.types,
        torrent_statuses.status_counts,
        torrent_presences.presence_counts,
        torrent_locations.location_counts
    FROM torrent_page_files page_file
    JOIN torrent_file_totals USING (torrent_id,site)
    JOIN torrent_types USING (torrent_id,site)
    JOIN torrent_statuses USING (torrent_id,site)
    JOIN torrent_presences USING (torrent_id,site)
    JOIN torrent_locations USING (torrent_id,site)
    GROUP BY
        page_file.torrent_id,page_file.site,page_file.infohash,page_file.title,
        page_file.display_name,page_file.category,torrent_types.types,
        torrent_statuses.status_counts,torrent_presences.presence_counts,
        torrent_locations.location_counts
), groupable AS (
    SELECT
        CASE CAST(%(group_by)s AS text)
            WHEN 'type' THEN type
            WHEN 'status' THEN status
            WHEN 'source' THEN source
            WHEN 'presence' THEN presence
            WHEN 'location' THEN location_group
            ELSE NULL
        END AS group_key,
        torrent_id,
        site,
        size
    FROM selected
), grouped AS (
    SELECT group_key,count(DISTINCT (site,torrent_id)) AS torrent_count,
           count(*) AS file_count,COALESCE(sum(size), 0) AS bytes_total
    FROM groupable
    WHERE group_key IS NOT NULL
    GROUP BY group_key
)
SELECT
    (SELECT count(*) FROM torrent_keys) AS total_count,
    COALESCE(
        (
            SELECT jsonb_agg(to_jsonb(page_rows)
                             ORDER BY page_rows.site,page_rows.title,
                                      page_rows.infohash,page_rows.torrent_id)
            FROM (
                SELECT *
                FROM torrent_rows
                ORDER BY site,title,infohash,torrent_id
            ) page_rows
        ),
        '[]'::jsonb
    ) AS items,
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'key', group_key,
                    'label', group_key,
                    'count', torrent_count,
                    'torrents', torrent_count,
                    'files', file_count,
                    'bytes', bytes_total
                ) ORDER BY group_key
            )
            FROM grouped
        ),
        '[]'::jsonb
    ) AS groups
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
    groups: list[dict[str, Any]] = field(default_factory=list)
    group_by: str | None = None
    view: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": (self.total + self.page_size - 1) // self.page_size,
        }
        if self.group_by is not None:
            payload["group_by"] = self.group_by
            payload["groups"] = self.groups
        if self.view is not None:
            payload["view"] = self.view
            if self.view == "torrents":
                payload["total_torrents"] = self.total
        return payload


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


def _exact_infohash(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().casefold()
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("infohash deve conter 40 caracteres hexadecimais")
    return normalized


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


def _source_filter(site: str | None, source: str | None) -> str | None:
    selected_site = _validated_optional(site, EXPLORER_SITES, "site")
    selected_source = _validated_optional(source, EXPLORER_SITES, "source")
    if (
        selected_site is not None
        and selected_source is not None
        and selected_site != selected_source
    ):
        raise ValueError("site e source conflitantes")
    return selected_source or selected_site


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _item_contract(item: dict[str, Any], selected_source: str | None) -> dict[str, Any]:
    """Add UI aliases without replacing the canonical inventory fields."""

    site = str(item.get("site") or "")
    local_present = bool(item.get("local_present"))
    drive_candidate = bool(item.get("drive_file_id"))
    drive_confidence = str(
        item.get("presence_confidence")
        or item.get("drive_match_confidence")
        or "exact"
    )
    drive_present = bool(
        item.get("gdrive_present")
        or (drive_candidate and drive_confidence != "possible")
    )
    source = "local" if selected_source == "local" else site
    item.setdefault("source", source)
    item.setdefault("type", item.get("file_kind") or "other")
    item.setdefault("status", "available" if local_present or drive_present else "cataloged")

    if selected_source == "local":
        location_kind = "local"
        location = item.get("local_relative_path") or item.get("path")
    elif site == "gdrive":
        location_kind = "gdrive"
        location = item.get("drive_relative_path") or item.get("path")
    else:
        location_kind = "torrent"
        location = item.get("path")
    item.setdefault("location_kind", location_kind)
    item.setdefault("location", location)

    if "locations" not in item:
        locations: list[dict[str, Any]] = []
        if site in {"filecr", "1337x"}:
            locations.append(
                {
                    "source": site,
                    "location_kind": "torrent",
                    "path": item.get("path"),
                    "status": "cataloged",
                }
            )
        if drive_candidate or drive_present:
            locations.append(
                {
                    "source": "gdrive",
                    "location_kind": "gdrive",
                    "path": item.get("drive_relative_path") or item.get("path"),
                    "status": "available" if drive_present else "possible",
                    "confidence": drive_confidence,
                    "drive_file_id": item.get("drive_file_id"),
                }
            )
        if local_present:
            locations.append(
                {
                    "source": "local",
                    "location_kind": "local",
                    "path": item.get("local_relative_path") or item.get("path"),
                    "destination_path": item.get("local_destination_path"),
                    "status": "available",
                }
            )
        item["locations"] = locations
    return item


def _torrent_contract(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a lightweight torrent summary without embedding its files."""

    site = str(item.get("site") or "")
    infohash = str(item.get("infohash") or "")
    item["id"] = f"{site}:{infohash}"
    for field_name in ("types", "status_counts", "presence_counts", "location_counts"):
        item[field_name] = _json_object(item.get(field_name), field_name)
    item["file_count"] = _integer(item.get("file_count"))
    item["matched_file_count"] = _integer(
        item.get("matched_file_count", item["file_count"])
    )
    item["total_files"] = _integer(item.get("total_files", item["file_count"]))
    item["bytes"] = _integer(item.get("bytes"))
    item["matched_bytes"] = _integer(item.get("matched_bytes", item["bytes"]))
    item["total_bytes"] = _integer(item.get("total_bytes", item["bytes"]))
    item.setdefault("size", item["bytes"])
    return item


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
        source_types = _json_object(
            row.get("source_types_by_source"), "source_types_by_source"
        )
        source_transfers = _json_object(
            row.get("source_transfers_by_source"), "source_transfers_by_source"
        )
        files_by_kind = _json_object(row.get("files_by_kind"), "files_by_kind")
        row["torrent_sources_by_site"] = sources
        row["source_types_by_source"] = source_types
        row["source_transfers_by_source"] = source_transfers
        row["files_by_kind"] = files_by_kind
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
        active_states = TRANSFER_STATES - {"completed", "failed", "cancelled"}
        cards: list[dict[str, Any]] = []
        for source in SOURCE_CARDS:
            source_stat = sources.get(source)
            if not isinstance(source_stat, Mapping):
                source_stat = {}
            if source == "gdrive":
                titles = _integer(row.get("gdrive_title_count"))
                files = _integer(row.get("gdrive_file_count"))
                bytes_total = _integer(row.get("gdrive_bytes_total"))
            elif source == "local":
                titles = _integer(row.get("local_title_count"))
                files = _integer(row.get("local_file_count"))
                bytes_total = _integer(row.get("local_bytes_total"))
            else:
                titles = _integer(source_stat.get("titles"))
                files = _integer(source_stat.get("files"))
                bytes_total = _integer(source_stat.get("bytes"))

            raw_types = source_types.get(source)
            types: dict[str, dict[str, int]] = {}
            if isinstance(raw_types, Mapping):
                for kind in FILE_KINDS:
                    metrics = raw_types.get(kind)
                    if isinstance(metrics, Mapping) and _integer(metrics.get("count")):
                        types[kind] = {
                            "count": _integer(metrics.get("count")),
                            "bytes": _integer(metrics.get("bytes")),
                        }

            raw_states = source_transfers.get(source)
            states: dict[str, int] = {}
            if isinstance(raw_states, Mapping):
                states = {
                    str(state): _integer(count)
                    for state, count in raw_states.items()
                    if _integer(count)
                }
            active_transfers = sum(states.get(state, 0) for state in active_states)
            label, location, location_kind = SOURCE_METADATA[source]
            cards.append(
                {
                    "source": source,
                    "label": label,
                    "status": "busy" if active_transfers else ("ready" if files else "empty"),
                    "selectable": True,
                    "location": location,
                    "location_kind": location_kind,
                    "titles": titles,
                    "files": files,
                    "bytes": bytes_total,
                    "types": types,
                    "active_transfers": active_transfers,
                    "transfers_by_state": states,
                    "query": {"source": source},
                }
            )
        row["source_cards"] = cards

        type_filters: list[dict[str, Any]] = []
        for kind in FILE_KINDS:
            metrics = files_by_kind.get(kind)
            if not isinstance(metrics, Mapping) or not _integer(metrics.get("count")):
                continue
            type_filters.append(
                {
                    "value": kind,
                    "label": kind.replace("_", " ").title(),
                    "count": _integer(metrics.get("count")),
                    "bytes": _integer(metrics.get("bytes")),
                }
            )
        row["filters"] = {
            "sources": [
                {
                    "value": card["source"],
                    "label": card["label"],
                    "count": card["files"],
                    "bytes": card["bytes"],
                    "status": card["status"],
                }
                for card in cards
            ],
            "types": type_filters,
            "statuses": [
                {"value": "available", "label": "Disponivel"},
                {"value": "cataloged", "label": "Catalogado"},
            ],
            "presences": [
                {"value": value, "label": value.replace("_", " ").title()}
                for value in sorted(PRESENCE_FILTERS)
            ],
            "group_by": [
                {"value": value, "label": value.replace("_", " ").title()}
                for value in ("type", "status", "source", "presence", "location")
            ],
        }
        return row

    def explorer(
        self,
        *,
        q: str | None = None,
        site: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        presence: str | None = None,
        status: str | None = None,
        group_by: str | None = None,
        view: str | None = None,
        infohash: str | None = None,
        origin_site: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        selected_page, selected_size, limit, offset = _pagination(page, page_size)
        selected_source = _source_filter(site, source)
        selected_group = _validated_optional(group_by, EXPLORER_GROUPS, "group_by")
        selected_view = _validated_optional(view, EXPLORER_VIEWS, "view") or "files"
        selected_origin = _validated_optional(origin_site, SITES, "origin_site")
        if (
            selected_origin is not None
            and selected_source in SITES
            and selected_origin != selected_source
        ):
            raise ValueError("source e origin_site conflitantes")
        params = {
            "q": _like_pattern(q),
            "site": selected_source,
            "origin_site": selected_origin,
            "infohash": _exact_infohash(infohash),
            "kind": _validated_optional(kind, set(FILE_KINDS), "kind"),
            "presence": _validated_optional(presence, PRESENCE_FILTERS, "presence"),
            "status": _validated_optional(status, EXPLORER_STATUSES, "status"),
            "group_by": selected_group,
            "limit": limit,
            "offset": offset,
        }
        query = TORRENT_EXPLORER_SQL if selected_view == "torrents" else EXPLORER_SQL
        row = _mapping(self.database.execute(query, params).fetchone())
        raw_items = _json_list(row.get("items"))
        if selected_view == "torrents":
            items = [_torrent_contract(item) for item in raw_items]
        else:
            items = [_item_contract(item, selected_source) for item in raw_items]
        return Page(
            items=items,
            total=int(row.get("total_count") or 0),
            page=selected_page,
            page_size=selected_size,
            groups=_json_list(row.get("groups")),
            group_by=selected_group,
            view=selected_view,
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
