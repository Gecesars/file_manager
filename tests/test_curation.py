from __future__ import annotations

from typing import Any

import pytest

from ofc_media.curation import (
    CurationService,
    destination_path,
    display_title,
    priority_key,
)


INFOHASH = "a" * 40


class Result:
    def __init__(self, *, row: dict[str, Any] | None = None, rows=None) -> None:
        self.row = row
        self.rows = list(rows or [])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Database:
    def __init__(self, *results: Result) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        return self.results.pop(0)


def candidate(**values: Any) -> dict[str, Any]:
    return {
        "torrent_id": 7,
        "site": "1337x",
        "infohash": INFOHASH,
        "title": "The.Walking.Dead.Dead.City.S03E03.1080p.mkv",
        "display_name": "Walking Dead",
        "canonical_title": "The Walking Dead: Dead City",
        "category": "TV",
        "media_kind": "tv",
        "video_count": 1,
        "video_bytes": 2_000_000_000,
        "embedded_subtitle_count": 0,
        "embedded_subtitle_bytes": 0,
        "external_subtitle_count": 1,
        "subtitle_languages": ["pt-BR"],
        "subtitles_ready": True,
        "local_video_count": 0,
        "drive_exact_video_count": 0,
        "drive_possible_video_count": 0,
        "availability": "torrent",
        "seeders": 80,
        "peer_count": 100,
        "popularity_score": 88.123,
        "total_count": 1,
        **values,
    }


def test_priority_matching_is_prefix_based_and_rejects_false_dexter():
    assert priority_key("Dexter: New Blood S01") == "dexter"
    assert priority_key("The Walking Dead: Dead City") == "walking-dead"
    assert priority_key("House of the Damned (Maury Dexter)") is None


def test_destination_uses_media_tree_and_single_season():
    tv = {
        "category": "TV",
        "media_kind": "tv",
        "canonical_title": "The Walking Dead: Dead City",
        "title": "The.Walking.Dead.Dead.City.S03E03.1080p",
    }
    movie = {
        "category": "Movies",
        "media_kind": "movie",
        "canonical_title": "Hairspray",
        "release_year": 1988,
    }
    assert display_title(tv) == "The Walking Dead: Dead City"
    assert destination_path(tv, ["Release/S03E03.mkv"]) == "TV/The Walking Dead_ Dead City/Season 03"
    assert destination_path(movie) == "Movies/Hairspray (1988)"


def test_list_media_keeps_requested_gaps_separate_from_ranked_candidates():
    row = candidate()
    database = Database(Result(rows=[row]), Result(rows=[row]))

    page = CurationService(database).list_media(
        media_kind="tv", subtitles="ready", availability="torrent", page=1, page_size=24
    ).as_dict()

    assert page["total"] == 1
    assert page["items"][0]["actionable"] is True
    assert page["items"][0]["destination_path"] == "TV/The Walking Dead_ Dead City/Season 03"
    priorities = {item["key"]: item for item in page["priorities"]}
    assert priorities["walking-dead"]["status"] == "ready"
    assert priorities["breaking-bad"]["status"] == "missing"
    assert page["policy"]["automatic_download"] is False
    assert database.calls[0][1][-2:] == (24, 0)


@pytest.mark.parametrize(
    ("name", "value"),
    (("media_kind", "software"), ("subtitles", "unknown"), ("availability", "cache")),
)
def test_list_media_rejects_non_media_filters(name: str, value: str):
    with pytest.raises(ValueError):
        CurationService(Database()).list_media(**{name: value})


def test_publication_plan_requires_subtitles_and_excludes_drive_exact_video():
    torrent = {
        "torrent_id": 9,
        "site": "1337x",
        "infohash": INFOHASH,
        "title": "Example.S01E02",
        "display_name": "Example",
        "canonical_title": "Example",
        "category": "TV",
        "media_type": "series",
    }
    files = [
        {
            "id": 1,
            "path": "S01E01.mkv",
            "size": 1_000_000_000,
            "file_kind": "video",
            "is_video": True,
            "is_subtitle": False,
            "drive_exact": True,
        },
        {
            "id": 2,
            "path": "S01E02.mkv",
            "size": 1_100_000_000,
            "file_kind": "video",
            "is_video": True,
            "is_subtitle": False,
            "drive_exact": False,
        },
    ]
    external = [
        {
            "torrent_path": "S01E02.mkv",
            "language": "pt-BR",
            "subtitle_path": "D:/vault/S01E02.srt",
            "status": "downloaded",
        }
    ]
    database = Database(Result(row=torrent), Result(rows=files), Result(rows=external))

    plan = CurationService(database).publication_plan("1337x", INFOHASH)

    assert [item["id"] for item in plan["video_files"]] == [2]
    assert plan["external_subtitles"] == external
    assert plan["destination_path"] == "TV/Example/Season 01"


def test_publication_plan_blocks_media_without_validated_subtitle():
    database = Database(
        Result(
            row={
                "torrent_id": 9,
                "site": "1337x",
                "infohash": INFOHASH,
                "title": "Movie 2026",
                "display_name": "Movie",
                "category": "Movies",
            }
        ),
        Result(
            rows=[
                {
                    "id": 2,
                    "path": "Movie.mkv",
                    "size": 1_100_000_000,
                    "file_kind": "video",
                    "is_video": True,
                    "is_subtitle": False,
                    "drive_exact": False,
                }
            ]
        ),
        Result(rows=[]),
    )
    with pytest.raises(ValueError, match="nenhuma legenda"):
        CurationService(database).publication_plan("1337x", INFOHASH)
