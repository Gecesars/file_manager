from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .file_kinds import safe_destination_path
from .safety import normalized_infohash


PRIORITY_TITLES: tuple[tuple[str, str], ...] = (
    ("breaking-bad", "Breaking Bad"),
    ("game-of-thrones", "Game of Thrones"),
    ("dexter", "Dexter"),
    ("walking-dead", "The Walking Dead"),
)
MEDIA_KINDS = frozenset({"all", "tv", "movie"})
SUBTITLE_FILTERS = frozenset({"any", "ready", "missing"})
AVAILABILITY_FILTERS = frozenset({"all", "torrent", "local", "drive", "partial"})
MIN_MAIN_VIDEO_BYTES = 100 * 1024**2
MAX_PAGE_SIZE = 60


_BASE_CTE = r"""
WITH local_ids AS MATERIALIZED (
    SELECT DISTINCT (manifest->>'file_id')::bigint AS file_id
    FROM runtime.transfer_jobs job
    CROSS JOIN LATERAL jsonb_array_elements(job.local_files) manifest
    WHERE job.target='local' AND job.state='completed'
      AND manifest ? 'file_id'
      AND manifest->>'file_id' ~ '^[1-9][0-9]*$'
),
drive_profile AS MATERIALIZED (
    SELECT
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) LIKE '%%terror%%') AS horror,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) ~ '(thriller|crime)') AS crime,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) LIKE '%%drama%%') AS drama,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) ~ '(acao|ação)') AS action,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) ~ '(ficcao|ficção|sci)') AS scifi,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) LIKE '%%aventura%%') AS adventure,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) LIKE '%%comedia%%') AS comedy,
      count(DISTINCT t.id) FILTER (WHERE lower(t.category) LIKE '%%anima%%') AS animation
    FROM catalog.torrents t
    WHERE t.site='gdrive' AND t.active
),
media_torrent_ids AS MATERIALIZED (
    SELECT id
    FROM catalog.torrents
    WHERE active AND site='1337x'
      AND (lower(category) LIKE 'tv%%' OR lower(category) LIKE 'movie%%')
),
subtitle_summary AS (
    SELECT site,trim(infohash) AS infohash,
           count(*) FILTER (
             WHERE status IN ('downloaded','synced')
               AND COALESCE(synced_path,subtitle_path,'') <> ''
           ) AS external_subtitle_count,
           array_agg(DISTINCT language) FILTER (
             WHERE status IN ('downloaded','synced')
               AND COALESCE(synced_path,subtitle_path,'') <> ''
           ) AS subtitle_languages
    FROM catalog.subtitles
    WHERE active
    GROUP BY site,trim(infohash)
),
file_summary AS (
    SELECT f.torrent_id,
           count(*) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
           ) AS video_count,
           COALESCE(sum(f.size) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
           ),0) AS video_bytes,
           count(*) FILTER (WHERE f.is_subtitle) AS embedded_subtitle_count,
           COALESCE(sum(f.size) FILTER (WHERE f.is_subtitle),0) AS embedded_subtitle_bytes,
           count(*) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
               AND local.file_id IS NOT NULL
           ) AS local_video_count,
           count(*) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
               AND EXISTS (
                 SELECT 1
                 FROM catalog.drive_files drive
                 JOIN catalog.torrent_files remote_file
                   ON remote_file.id=drive.torrent_file_id
                 WHERE drive.active AND drive.can_download
                   AND remote_file.size=f.size
                   AND f.sha256 IS NOT NULL AND remote_file.sha256 IS NOT NULL
                   AND trim(remote_file.sha256)=trim(f.sha256)
               )
           ) AS drive_exact_video_count,
           count(*) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
               AND f.sha256 IS NULL
               AND EXISTS (
                 SELECT 1
                 FROM catalog.drive_files drive
                 JOIN catalog.torrent_files remote_file
                   ON remote_file.id=drive.torrent_file_id
                 WHERE drive.active AND drive.can_download
                   AND remote_file.size=f.size
                   AND lower(regexp_replace(regexp_replace(remote_file.path,'^.*/',''),
                                             '[^[:alnum:]]+','','g'))
                       = lower(regexp_replace(regexp_replace(f.path,'^.*/',''),
                                              '[^[:alnum:]]+','','g'))
               )
           ) AS drive_possible_video_count
    FROM catalog.torrent_files f
    JOIN media_torrent_ids eligible ON eligible.id=f.torrent_id
    LEFT JOIN local_ids local ON local.file_id=f.id
    GROUP BY f.torrent_id
),
candidates AS (
    SELECT t.id AS torrent_id,t.site,trim(t.infohash) AS infohash,
           t.title,t.display_name,t.category,t.total_size,t.file_count,
           COALESCE(t.seeders,0) AS seeders,
           COALESCE(t.peer_count,0) AS peer_count,
           COALESCE(NULLIF(m.canonical_title,''),NULLIF(t.title,''),t.display_name) AS canonical_title,
           m.release_year,m.media_type,m.description,m.imdb_rating,m.imdb_votes,m.imdb_id,
           CASE
             WHEN lower(t.category) LIKE 'tv%%' OR lower(COALESCE(m.media_type,'')) IN ('series','episode')
               THEN 'tv'
             ELSE 'movie'
           END AS media_kind,
           fs.video_count,fs.video_bytes,fs.embedded_subtitle_count,
           fs.embedded_subtitle_bytes,fs.local_video_count,
           fs.drive_exact_video_count,fs.drive_possible_video_count,
           COALESCE(ss.external_subtitle_count,0) AS external_subtitle_count,
           COALESCE(ss.subtitle_languages,ARRAY[]::text[]) AS subtitle_languages,
           (fs.embedded_subtitle_count > 0 OR COALESCE(ss.external_subtitle_count,0) > 0)
             AS subtitles_ready,
           CASE
             WHEN fs.drive_exact_video_count >= fs.video_count THEN 'drive'
             WHEN fs.local_video_count >= fs.video_count THEN 'local'
             WHEN fs.drive_exact_video_count > 0 OR fs.local_video_count > 0 THEN 'partial'
             ELSE 'torrent'
           END AS availability,
           (
             COALESCE(m.imdb_rating,0) * 10
             + ln(COALESCE(m.imdb_votes,0) + 1) * 3
             + ln(COALESCE(t.seeders,0) + 1) * 4
             + ln(COALESCE(t.peer_count,0) + 1) * 2
           ) AS popularity_score,
           CASE
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(horror|terror|slasher)' THEN 'terror'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(crime|thriller|detective|murder)' THEN 'crime e thriller'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(science fiction|sci-fi|space|alien)' THEN 'ficcao cientifica'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(action|war|soldier|assassin)' THEN 'acao'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(adventure|quest|journey)' THEN 'aventura'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(comedy|comic|funny)' THEN 'comedia'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(animation|animated)' THEN 'animacao'
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(drama|family|relationship)' THEN 'drama'
             ELSE NULL
           END AS similarity_genre,
           CASE
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(horror|terror|slasher)' THEN ln(dp.horror + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(crime|thriller|detective|murder)' THEN ln(dp.crime + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(science fiction|sci-fi|space|alien)' THEN ln(dp.scifi + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(action|war|soldier|assassin)' THEN ln(dp.action + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(adventure|quest|journey)' THEN ln(dp.adventure + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(comedy|comic|funny)' THEN ln(dp.comedy + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(animation|animated)' THEN ln(dp.animation + 1)
             WHEN lower(COALESCE(m.description,'') || ' ' || t.title) ~ '(drama|family|relationship)' THEN ln(dp.drama + 1)
             ELSE 0
           END AS similarity_score
    FROM catalog.torrents t
    JOIN file_summary fs ON fs.torrent_id=t.id AND fs.video_count > 0
    LEFT JOIN catalog.metadata m ON m.site=t.site AND m.infohash=t.infohash
    LEFT JOIN subtitle_summary ss ON ss.site=t.site AND ss.infohash=trim(t.infohash)
    CROSS JOIN drive_profile dp
    WHERE t.active AND t.site='1337x'
      AND (lower(t.category) LIKE 'tv%%' OR lower(t.category) LIKE 'movie%%')
)
"""


CURATION_LIST_SQL = _BASE_CTE + r"""
,filtered AS (
    SELECT *,count(*) OVER() AS total_count
    FROM candidates
    WHERE (%s='' OR media_kind=%s)
      AND (%s='any' OR (%s='ready' AND subtitles_ready)
                    OR (%s='missing' AND NOT subtitles_ready))
      AND (%s='all' OR availability=%s)
      AND (%s='' OR canonical_title ILIKE %s OR title ILIKE %s OR display_name ILIKE %s)
)
SELECT * FROM filtered
ORDER BY subtitles_ready DESC,
         (availability <> 'drive') DESC,
         similarity_score DESC,
         popularity_score DESC,seeders DESC,canonical_title,infohash
LIMIT %s OFFSET %s
"""


CURATION_PRIORITY_SQL = r"""
WITH priority_torrents AS MATERIALIZED (
    SELECT t.id AS torrent_id,t.site,trim(t.infohash) AS infohash,t.title,
           t.display_name,t.category,t.total_size,t.file_count,
           COALESCE(t.seeders,0) AS seeders,COALESCE(t.peer_count,0) AS peer_count,
           COALESCE(NULLIF(m.canonical_title,''),NULLIF(t.title,''),t.display_name) AS canonical_title,
           m.release_year,m.media_type,m.description,m.imdb_rating,m.imdb_votes,m.imdb_id,
           CASE WHEN lower(t.category) LIKE 'tv%%'
                     OR lower(COALESCE(m.media_type,'')) IN ('series','episode')
                THEN 'tv' ELSE 'movie' END AS media_kind
    FROM catalog.torrents t
    LEFT JOIN catalog.metadata m ON m.site=t.site AND m.infohash=t.infohash
    WHERE t.active AND t.site='1337x'
      AND (lower(t.category) LIKE 'tv%%' OR lower(t.category) LIKE 'movie%%')
      AND (
        lower(regexp_replace(COALESCE(m.canonical_title,t.title),'[^[:alnum:]]+',' ','g')) ~
          '^(breaking bad|game of thrones|dexter|the walking dead|walking dead)( |$)'
        OR lower(regexp_replace(t.title,'[^[:alnum:]]+',' ','g')) ~
          '^(breaking bad|game of thrones|dexter|the walking dead|walking dead)( |$)'
      )
), file_counts AS (
    SELECT p.torrent_id,
           count(*) FILTER (
             WHERE f.is_video AND f.size >= 104857600
               AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)'
           ) AS video_count,
           COALESCE(sum(f.size) FILTER (WHERE f.is_video),0) AS video_bytes,
           count(*) FILTER (WHERE f.is_subtitle) AS embedded_subtitle_count,
           COALESCE(sum(f.size) FILTER (WHERE f.is_subtitle),0) AS embedded_subtitle_bytes
    FROM priority_torrents p
    JOIN catalog.torrent_files f ON f.torrent_id=p.torrent_id
    GROUP BY p.torrent_id
), external_counts AS (
    SELECT p.torrent_id,
           count(s.*) FILTER (
             WHERE s.status IN ('downloaded','synced')
               AND COALESCE(s.synced_path,s.subtitle_path,'') <> ''
           ) AS external_subtitle_count,
           array_agg(DISTINCT s.language) FILTER (
             WHERE s.status IN ('downloaded','synced')
               AND COALESCE(s.synced_path,s.subtitle_path,'') <> ''
           ) AS subtitle_languages
    FROM priority_torrents p
    LEFT JOIN catalog.subtitles s ON s.site=p.site AND s.infohash=p.infohash AND s.active
    GROUP BY p.torrent_id
)
SELECT p.*,fc.video_count,fc.video_bytes,fc.embedded_subtitle_count,
       fc.embedded_subtitle_bytes,0 AS local_video_count,
       0 AS drive_exact_video_count,0 AS drive_possible_video_count,
       COALESCE(ec.external_subtitle_count,0) AS external_subtitle_count,
       COALESCE(ec.subtitle_languages,ARRAY[]::text[]) AS subtitle_languages,
       (fc.embedded_subtitle_count > 0 OR COALESCE(ec.external_subtitle_count,0) > 0)
         AS subtitles_ready,
       'torrent' AS availability,
       (COALESCE(p.imdb_rating,0) * 10
        + ln(COALESCE(p.imdb_votes,0) + 1) * 3
        + ln(p.seeders + 1) * 4 + ln(p.peer_count + 1) * 2) AS popularity_score,
       NULL::text AS similarity_genre,0::double precision AS similarity_score
FROM priority_torrents p
JOIN file_counts fc ON fc.torrent_id=p.torrent_id AND fc.video_count > 0
LEFT JOIN external_counts ec ON ec.torrent_id=p.torrent_id
ORDER BY popularity_score DESC,p.seeders DESC,p.canonical_title,p.infohash
"""


SELECTION_TORRENT_SQL = r"""
SELECT t.id AS torrent_id,t.site,trim(t.infohash) AS infohash,t.title,
       t.display_name,t.category,m.canonical_title,m.release_year,m.media_type
FROM catalog.torrents t
LEFT JOIN catalog.metadata m ON m.site=t.site AND m.infohash=t.infohash
WHERE t.site=%s AND t.infohash=%s AND t.active
  AND t.site='1337x'
  AND (lower(t.category) LIKE 'tv%%' OR lower(t.category) LIKE 'movie%%')
"""


SELECTION_FILES_SQL = r"""
SELECT f.id,f.path,f.size,f.file_kind,f.mime_type,f.is_video,f.is_subtitle,
       EXISTS (
         SELECT 1 FROM catalog.drive_files drive
         JOIN catalog.torrent_files remote_file ON remote_file.id=drive.torrent_file_id
         WHERE drive.active AND drive.can_download AND remote_file.size=f.size
           AND f.sha256 IS NOT NULL AND remote_file.sha256 IS NOT NULL
           AND trim(f.sha256)=trim(remote_file.sha256)
       ) AS drive_exact
FROM catalog.torrent_files f
WHERE f.torrent_id=%s AND (
  (f.is_video AND f.size >= %s
   AND lower(f.path) !~ '(^|[/_. -])(sample|trailer|featurette)([/_. -]|$)')
  OR f.is_subtitle
)
ORDER BY f.is_video DESC,f.size DESC,f.path,f.id
"""


SELECTION_SUBTITLES_SQL = r"""
SELECT torrent_path,language,file_name,status,provider,extension,size,
       subtitle_path,synced_path,season,episode
FROM catalog.subtitles
WHERE site=%s AND infohash=%s AND active
  AND status IN ('downloaded','synced')
  AND COALESCE(synced_path,subtitle_path,'') <> ''
ORDER BY language,torrent_path
"""


def _text_key(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def priority_key(value: Any) -> str | None:
    normalized = _text_key(value)
    aliases = (
        ("breaking-bad", ("breaking bad",)),
        ("game-of-thrones", ("game of thrones",)),
        ("dexter", ("dexter",)),
        ("walking-dead", ("the walking dead", "walking dead")),
    )
    for key, names in aliases:
        if any(normalized == name or normalized.startswith(f"{name} ") for name in names):
            return key
    return None


_RELEASE_SUFFIX = re.compile(
    r"(?ix)(?:\bS\d{1,2}(?:E\d{1,3})?\b|\b(?:19|20)\d{2}\b|"
    r"\b(?:2160p|1080p|720p|480p|bluray|web[- .]?dl|webrip|hdtv|x26[45]|hevc)\b).*"
)
_SEASON_RE = re.compile(r"(?i)(?:^|[^a-z0-9])S(?:eason[ ._-]*)?(\d{1,2})(?:E\d{1,3})?")


def display_title(row: Mapping[str, Any]) -> str:
    canonical = str(row.get("canonical_title") or "").strip()
    if canonical:
        return canonical
    source = str(row.get("title") or row.get("display_name") or row.get("infohash")).strip()
    cleaned = _RELEASE_SUFFIX.sub("", source.replace(".", " ").replace("_", " "))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -._[]()")
    return cleaned or source


def destination_path(row: Mapping[str, Any], file_paths: Sequence[str] = ()) -> str:
    media_kind = str(row.get("media_kind") or "").casefold()
    if not media_kind:
        category = str(row.get("category") or "").casefold()
        media_type = str(row.get("media_type") or "").casefold()
        media_kind = "tv" if category.startswith("tv") or media_type in {"series", "episode"} else "movie"
    title = display_title(row)
    if media_kind == "tv":
        seasons: set[int] = set()
        for value in (str(row.get("title") or ""), *file_paths):
            seasons.update(int(match) for match in _SEASON_RE.findall(value))
        parts = ["TV", title]
        if len(seasons) == 1:
            parts.append(f"Season {next(iter(seasons)):02d}")
        return safe_destination_path(*parts)
    raw_year = row.get("release_year")
    year = str(raw_year).strip() if raw_year not in (None, "") else ""
    label = f"{title} ({year})" if re.fullmatch(r"(?:19|20)\d{2}", year) else title
    return safe_destination_path("Movies", label)


def _public_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["infohash"] = str(item.get("infohash") or "").strip()
    item["media_kind"] = str(item.get("media_kind") or "movie")
    item["display_title"] = display_title(item)
    item["destination_path"] = destination_path(item)
    item["subtitle_count"] = int(item.get("embedded_subtitle_count") or 0) + int(
        item.get("external_subtitle_count") or 0
    )
    item["subtitles_ready"] = bool(item.get("subtitles_ready"))
    item["actionable"] = bool(
        item["subtitles_ready"]
        and int(item.get("drive_exact_video_count") or 0) < int(item.get("video_count") or 0)
    )
    item["popularity_score"] = round(float(item.get("popularity_score") or 0), 2)
    item["similarity_score"] = round(float(item.get("similarity_score") or 0), 2)
    item["priority"] = priority_key(item.get("canonical_title")) or priority_key(item.get("title"))
    if item["priority"]:
        item["recommendation_reason"] = "prioridade solicitada"
    elif item.get("similarity_genre"):
        item["recommendation_reason"] = (
            f"popular em {item['similarity_genre']}, perfil frequente no Drive"
        )
    else:
        item["recommendation_reason"] = "popularidade no IMDb e no swarm"
    return item


@dataclass(frozen=True, slots=True)
class CurationPage:
    items: list[dict[str, Any]]
    priorities: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "priorities": self.priorities,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "per_page": self.page_size,
            "pages": max(1, math.ceil(self.total / self.page_size)),
            "policy": {
                "source": "1337x",
                "excluded_sources": ["filecr"],
                "drive_root": "#Avideos",
                "media_kinds": ["tv", "movie"],
                "subtitles_required_for_action": True,
                "automatic_download": False,
            },
        }


class CurationService:
    def __init__(self, database: Any) -> None:
        self.database = database

    def list_media(
        self,
        *,
        query: str | None = None,
        media_kind: str | None = None,
        subtitles: str | None = None,
        availability: str | None = None,
        page: int | str = 1,
        page_size: int | str = 24,
    ) -> CurationPage:
        selected_kind = str(media_kind or "all").strip().casefold()
        selected_subtitles = str(subtitles or "any").strip().casefold()
        selected_availability = str(availability or "all").strip().casefold()
        if selected_kind not in MEDIA_KINDS:
            raise ValueError("tipo de midia invalido")
        if selected_subtitles not in SUBTITLE_FILTERS:
            raise ValueError("filtro de legenda invalido")
        if selected_availability not in AVAILABILITY_FILTERS:
            raise ValueError("filtro de disponibilidade invalido")
        try:
            selected_page = max(1, int(page))
            selected_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
        except (TypeError, ValueError) as exc:
            raise ValueError("paginacao invalida") from exc
        selected_query = str(query or "").strip()[:200]
        pattern = f"%{selected_query}%"
        rows = self.database.execute(
            CURATION_LIST_SQL,
            (
                "" if selected_kind == "all" else selected_kind,
                selected_kind,
                selected_subtitles,
                selected_subtitles,
                selected_subtitles,
                selected_availability,
                selected_availability,
                selected_query,
                pattern,
                pattern,
                pattern,
                selected_size,
                (selected_page - 1) * selected_size,
            ),
        ).fetchall()
        total = int(rows[0].get("total_count") or 0) if rows else 0
        priority_rows = self.database.execute(CURATION_PRIORITY_SQL).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in PRIORITY_TITLES}
        for row in priority_rows:
            public = _public_item(row)
            key = public.get("priority")
            if key in grouped:
                grouped[str(key)].append(public)
        priorities: list[dict[str, Any]] = []
        for rank, (key, title) in enumerate(PRIORITY_TITLES, start=1):
            matches = grouped[key]
            priorities.append(
                {
                    "key": key,
                    "rank": rank,
                    "title": title,
                    "status": "ready" if any(item["actionable"] for item in matches) else "found" if matches else "missing",
                    "candidate_count": len(matches),
                    "actionable_count": sum(bool(item["actionable"]) for item in matches),
                    "best_candidate": matches[0] if matches else None,
                }
            )
        return CurationPage(
            items=[_public_item(row) for row in rows],
            priorities=priorities,
            total=total,
            page=selected_page,
            page_size=selected_size,
        )

    def publication_plan(self, site: str, infohash: str) -> dict[str, Any]:
        selected_site = str(site or "").strip().casefold()
        selected_hash = normalized_infohash(infohash)
        torrent = self.database.execute(
            SELECTION_TORRENT_SQL, (selected_site, selected_hash)
        ).fetchone()
        if torrent is None:
            raise KeyError(selected_hash)
        row = dict(torrent)
        row["media_kind"] = (
            "tv"
            if str(row.get("category") or "").casefold().startswith("tv")
            or str(row.get("media_type") or "").casefold() in {"series", "episode"}
            else "movie"
        )
        files = [
            dict(value)
            for value in self.database.execute(
                SELECTION_FILES_SQL,
                (row["torrent_id"], MIN_MAIN_VIDEO_BYTES),
            ).fetchall()
        ]
        videos = [value for value in files if value.get("is_video") and not value.get("drive_exact")]
        embedded = [
            value for value in files if value.get("is_subtitle") and not value.get("drive_exact")
        ]
        external = [
            dict(value)
            for value in self.database.execute(
                SELECTION_SUBTITLES_SQL, (selected_site, selected_hash)
            ).fetchall()
        ]
        if not embedded and not external:
            raise ValueError("nenhuma legenda pronta e validada para este titulo")
        if not videos:
            raise ValueError("todos os videos principais ja estao no Drive")
        selected_paths = [str(value.get("path") or "") for value in videos]
        return {
            "site": selected_site,
            "infohash": selected_hash,
            "title": display_title(row),
            "media_kind": row["media_kind"],
            "destination_path": destination_path(row, selected_paths),
            "video_files": videos,
            "embedded_subtitle_files": embedded,
            "external_subtitles": external,
            "bytes_total": sum(int(value.get("size") or 0) for value in videos + embedded),
        }
