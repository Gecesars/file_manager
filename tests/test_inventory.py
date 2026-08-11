from __future__ import annotations

from typing import Any

import pytest

from ofc_media.file_kinds import (
    classified_destination_path,
    classify_extension,
    classify_file,
    match_presence,
    normalize_name,
    safe_destination_path,
)
from ofc_media.inventory import (
    DASHBOARD_SQL,
    EXPLORER_SQL,
    TRANSFERS_SQL,
    InventoryService,
)
from ofc_media.migrate import SCHEMA_SQL


class FakeResult:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any]:
        return self.row


class FakeDatabase:
    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> FakeResult:
        self.calls.append((sql, params))
        return FakeResult(self.rows.pop(0))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("movie.MKV", "video"),
        ("album.flac", "audio"),
        ("pt-BR.srt", "subtitle"),
        ("cover.webp", "image"),
        ("manual.pdf", "document"),
        ("backup.tar.zst", "archive"),
        ("installer.msi", "software"),
        ("catalog.parquet", "dataset"),
        ("unknown.asset", "other"),
    ],
)
def test_extension_classification_covers_canonical_kinds(name: str, expected: str):
    assert classify_extension(name) == expected


def test_classification_returns_mime_and_subtitle_marker():
    subtitle = classify_file("legendas/final.SRT", "application/octet-stream")
    assert subtitle.extension == ".srt"
    assert subtitle.file_kind == "subtitle"
    assert subtitle.mime_type == "application/x-subrip"
    assert subtitle.is_subtitle is True

    inferred = classify_file("asset.unknown", "video/custom")
    assert inferred.file_kind == "video"
    assert inferred.mime_type == "video/custom"


def test_names_and_destination_paths_are_portable_and_deterministic():
    assert normalize_name("Filmes/Joao.Gilberto - Ação (2024).MKV") == (
        "joao gilberto acao 2024 mkv"
    )
    assert safe_destination_path("video", "Ação: final", "parte 1/filme?.mkv") == (
        "video/Ação_ final/parte 1/filme_.mkv"
    )
    assert classified_destination_path("subtitle", "Série", "pt-BR/final.srt") == (
        "subtitle/Série/pt-BR/final.srt"
    )
    assert safe_destination_path("CON", "arquivo.txt") == "_CON/arquivo.txt"


@pytest.mark.parametrize(
    "unsafe",
    ["../secreto.mkv", "/absoluto.mkv", r"C:\Windows\arquivo.mkv", "ok/../nao"],
)
def test_destination_path_rejects_escape(unsafe: str):
    with pytest.raises(ValueError):
        safe_destination_path(unsafe)


def test_deduplication_never_calls_name_fallback_exact():
    digest = "a" * 64
    assert (
        match_presence(
            left_name="Filme.mkv",
            left_size=100,
            left_sha256=digest,
            right_name="outro.mkv",
            right_size=100,
            right_sha256=digest.upper(),
        )
        == "exact"
    )
    assert (
        match_presence(
            left_name="Filme (2024).mkv",
            left_size=100,
            right_name="filme-2024.mkv",
            right_size=100,
        )
        == "possible"
    )
    assert (
        match_presence(
            left_name="same.mkv",
            left_size=100,
            left_sha256="a" * 64,
            right_name="same.mkv",
            right_size=100,
            right_sha256="b" * 64,
        )
        == "none"
    )


def test_schema_contract_is_idempotent_and_complete():
    required_columns = ("file_kind", "mime_type", "is_subtitle", "sha256")
    for column in required_columns:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS runtime.transfer_jobs" in SCHEMA_SQL
    assert "selected_file_ids BIGINT[]" in SCHEMA_SQL
    assert "target IN ('local','gdrive')" in SCHEMA_SQL
    for state in (
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
    ):
        assert f"'{state}'" in SCHEMA_SQL
    assert "runtime.guard_transfer_job_state" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS ops.drive_cursors" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS ops.audit_events" in SCHEMA_SQL
    assert "torrent_files_sha256_size" in SCHEMA_SQL
    assert "transfer_jobs_selected_files" in SCHEMA_SQL
    assert "VALUES (3, 'inventario canonico" in SCHEMA_SQL


def test_explorer_is_parameterized_and_reports_possible_presence():
    database = FakeDatabase(
        {
            "total_count": 1,
            "items": [
                {
                    "file_id": 9,
                    "path": "Movie.mkv",
                    "presence": "gdrive",
                    "presence_confidence": "possible",
                }
            ],
        }
    )
    service = InventoryService(database)
    malicious_query = "100%_' OR true --"
    page = service.explorer(
        q=malicious_query,
        site="1337X",
        kind="video",
        presence="possible",
        page=2,
        page_size=25,
    )

    sql, params = database.calls[0]
    assert malicious_query not in sql
    assert params == {
        "q": "%100\\%\\_' OR true --%",
        "site": "1337x",
        "kind": "video",
        "presence": "possible",
        "status": None,
        "group_by": None,
        "limit": 25,
        "offset": 25,
    }
    assert page.total == 1
    assert page.items[0]["presence_confidence"] == "possible"
    assert "drive_file.sha256=source_file.sha256" in EXPLORER_SQL
    assert "drive_file.size=source_file.size" in EXPLORER_SQL
    assert "'possible'::text AS match_confidence" in EXPLORER_SQL
    assert "drive_matches AS" in EXPLORER_SQL
    assert "LEFT JOIN LATERAL (" not in EXPLORER_SQL


def test_drive_visibility_and_not_gdrive_filter_are_explicit_sql_contracts():
    database = FakeDatabase({"total_count": 0, "items": []})
    page = InventoryService(database).explorer(
        site="gdrive",
        presence="not_gdrive",
        page=1,
        page_size=10,
    )

    sql, params = database.calls[0]
    assert page.total == 0
    assert params["presence"] == "not_gdrive"
    assert "CAST(%(presence)s AS text)='not_gdrive'" in sql
    assert "drive_match_confidence<>'exact'" in sql
    assert "own_drive.active AND own_drive.can_download" in sql
    assert "d.active AND d.can_download" in DASHBOARD_SQL


def test_local_presence_requires_classified_target_and_matching_manifest():
    database = FakeDatabase({"total_count": 0, "items": []})
    InventoryService(database).explorer(source="local", page=1, page_size=10)

    sql, params = database.calls[0]
    assert params["site"] == "local"
    assert "local_job.target='local'" in sql
    assert "WITH ORDINALITY AS local_manifest" in sql
    assert "NULLIF(local_manifest.value->>'local_path','') IS NOT NULL" in sql
    assert "local_job.destination_path" in sql
    assert "persistent_local.file_id IS NOT NULL" in sql
    assert "job.target='local'" in DASHBOARD_SQL
    assert "WITH ORDINALITY AS local_manifest" in DASHBOARD_SQL


def test_possible_drive_match_is_not_promoted_to_confirmed_availability():
    database = FakeDatabase(
        {
            "total_count": 1,
            "items": [
                {
                    "file_id": 9,
                    "site": "filecr",
                    "infohash": "a" * 40,
                    "path": "arquivo.bin",
                    "file_kind": "other",
                    "drive_file_id": "possible-drive-id",
                    "drive_relative_path": "arquivo.bin",
                    "drive_match_confidence": "possible",
                    "local_present": False,
                }
            ],
        }
    )

    item = InventoryService(database).explorer(page=1, page_size=10).items[0]

    assert item["status"] == "cataloged"
    assert item["locations"][-1]["status"] == "possible"
    assert item["locations"][-1]["confidence"] == "possible"
    assert "drive_match_confidence='exact'" in EXPLORER_SQL


def test_video_catalog_counts_only_downloadable_active_drive_files():
    view_sql = SCHEMA_SQL.split(
        "CREATE OR REPLACE VIEW catalog.video_catalog AS", 1
    )[1].split("INSERT INTO ops.schema_migrations", 1)[0]

    assert "catalog.drive_files" in view_sql
    assert "active" in view_sql
    assert "can_download" in view_sql
    assert "t.site <> 'gdrive'" in view_sql


def test_dashboard_and_transfer_queries_do_not_need_flask_or_postgres():
    database = FakeDatabase(
        {
            "torrent_count": 12,
            "file_count": 34,
            "source_torrent_count": 10,
            "source_file_count": 29,
            "source_bytes_total": 1_000,
            "local_file_count": 3,
            "local_bytes_total": 300,
            "local_title_count": 2,
            "gdrive_file_count": 5,
            "gdrive_bytes_total": 500,
            "gdrive_title_count": 4,
            "files_by_kind": {"video": {"count": 2, "bytes": 900}},
            "source_types_by_source": {
                "filecr": {"video": {"count": 19, "bytes": 600}},
                "1337x": {"archive": {"count": 10, "bytes": 400}},
                "gdrive": {"video": {"count": 5, "bytes": 500}},
                "local": {"video": {"count": 3, "bytes": 300}},
            },
            "source_transfers_by_source": {
                "filecr": {"downloading": 1},
                "local": {"completed": 2},
            },
            "torrent_sources_by_site": {
                "filecr": {
                    "titles": 6,
                    "files": 19,
                    "bytes": 600,
                },
                "1337x": {
                    "titles": 4,
                    "files": 10,
                    "bytes": 400,
                },
            },
        },
        {"total_count": 1, "items": [{"id": "job", "state": "queued"}]},
    )
    service = InventoryService(database)
    dashboard = service.dashboard()
    assert dashboard["torrent_count"] == 12
    assert dashboard["domains"] == {
        "torrent": {
            "titles": 10,
            "files": 29,
            "bytes": 1_000,
            "sources": dashboard["torrent_sources_by_site"],
        },
        "local": {"titles": 2, "files": 3, "bytes": 300},
        "gdrive": {"titles": 4, "files": 5, "bytes": 500},
    }
    assert [card["source"] for card in dashboard["source_cards"]] == [
        "gdrive",
        "filecr",
        "1337x",
        "local",
    ]
    filecr_card = dashboard["source_cards"][1]
    assert filecr_card == {
        "source": "filecr",
        "label": "FileCR",
        "status": "busy",
        "selectable": True,
        "location": "Catalogo FileCR",
        "location_kind": "torrent",
        "titles": 6,
        "files": 19,
        "bytes": 600,
        "types": {"video": {"count": 19, "bytes": 600}},
        "active_transfers": 1,
        "transfers_by_state": {"downloading": 1},
        "query": {"source": "filecr"},
    }
    assert dashboard["filters"]["sources"][0]["value"] == "gdrive"
    assert dashboard["filters"]["types"] == [
        {"value": "video", "label": "Video", "count": 2, "bytes": 900}
    ]
    transfers = service.list_transfers(
        state="queued",
        target="gdrive",
        site="filecr",
        infohash="A" * 40,
        page_size=10,
    )
    _, params = database.calls[1]
    assert params == {
        "state": "queued",
        "target": "gdrive",
        "site": "filecr",
        "infohash": "a" * 40,
        "limit": 10,
        "offset": 0,
    }
    assert transfers.as_dict()["items"][0]["state"] == "queued"


def test_dashboard_domains_separate_origin_from_physical_presence_without_duplicates():
    assert "t.site IN ('filecr','1337x')" in DASHBOARD_SQL
    assert "JOIN source_torrents t ON t.id=f.torrent_id" in DASHBOARD_SQL
    assert "d.drive_file_id" in DASHBOARD_SQL
    assert "d.active AND d.can_download" in DASHBOARD_SQL
    assert "SELECT DISTINCT f.id AS file_id" in DASHBOARD_SQL
    assert "job.state='completed'" in DASHBOARD_SQL
    assert "jsonb_array_length(job.local_files) > 0" in DASHBOARD_SQL
    assert "source_torrent_count" in DASHBOARD_SQL
    assert "source_file_count" in DASHBOARD_SQL
    assert "source_bytes_total" in DASHBOARD_SQL
    assert "gdrive_bytes_total" in DASHBOARD_SQL
    assert "gdrive_title_count" in DASHBOARD_SQL
    assert "local_bytes_total" in DASHBOARD_SQL
    assert "local_title_count" in DASHBOARD_SQL
    assert "torrent_sources_by_site" in DASHBOARD_SQL


def test_dashboard_accepts_json_text_for_torrent_source_breakdown():
    database = FakeDatabase(
        {
            "torrent_sources_by_site": (
                '{"filecr":{"titles":1,"files":2,"bytes":3}}'
            )
        }
    )

    result = InventoryService(database).dashboard()

    assert result["domains"]["torrent"]["sources"]["filecr"] == {
        "titles": 1,
        "files": 2,
        "bytes": 3,
    }
    assert result["domains"]["local"] == {"titles": 0, "files": 0, "bytes": 0}


def test_explorer_virtual_torrent_site_groups_real_torrent_sources_only():
    database = FakeDatabase({"total_count": 0, "items": []})

    InventoryService(database).explorer(site="torrent", page_size=10)

    sql, params = database.calls[0]
    assert params["site"] == "torrent"
    assert "CAST(%(site)s AS text)='torrent'" in sql
    assert "t.site IN ('filecr','1337x')" in sql


def test_local_source_exposes_original_and_physical_locations_and_type_groups():
    database = FakeDatabase(
        {
            "total_count": 1,
            "items": [
                {
                    "file_id": 44,
                    "site": "filecr",
                    "file_kind": "video",
                    "path": "Release/Season 01/Episode 01.mkv",
                    "local_present": True,
                    "local_relative_path": "Release/Season 01/Episode 01.mkv",
                    "local_destination_path": "video/Series/Example",
                    "drive_file_id": "drive_file_12345",
                    "drive_relative_path": "Series/Example/Season 01/Episode 01.mkv",
                }
            ],
            "groups": [
                {
                    "key": "video",
                    "label": "video",
                    "count": 1,
                    "files": 1,
                    "bytes": 123,
                }
            ],
        }
    )

    page = InventoryService(database).explorer(
        source="LOCAL",
        kind="video",
        status="available",
        group_by="type",
        page_size=20,
    )

    item = page.items[0]
    assert item["path"] == "Release/Season 01/Episode 01.mkv"
    assert item["source"] == "local"
    assert item["type"] == "video"
    assert item["status"] == "available"
    assert item["location_kind"] == "local"
    assert item["location"] == "Release/Season 01/Episode 01.mkv"
    assert item["drive_relative_path"] == "Series/Example/Season 01/Episode 01.mkv"
    assert {location["source"] for location in item["locations"]} == {
        "filecr",
        "gdrive",
        "local",
    }
    assert page.as_dict()["group_by"] == "type"
    assert page.as_dict()["groups"][0]["key"] == "video"
    _sql, params = database.calls[0]
    assert params == {
        "q": None,
        "site": "local",
        "kind": "video",
        "presence": None,
        "status": "available",
        "group_by": "type",
        "limit": 20,
        "offset": 0,
    }


def test_explorer_source_status_and_group_filters_are_bounded():
    service = InventoryService(FakeDatabase())

    with pytest.raises(ValueError, match="conflitantes"):
        service.explorer(site="filecr", source="local")
    with pytest.raises(ValueError, match="status invalido"):
        service.explorer(status="moving")
    with pytest.raises(ValueError, match="group_by invalido"):
        service.explorer(group_by="arbitrary_sql")


def test_explorer_sql_keeps_logical_paths_separate_from_physical_locations():
    assert "local_file.relative_path AS local_relative_path" in EXPLORER_SQL
    assert "local_file.destination_path AS local_destination_path" in EXPLORER_SQL
    assert "drive_match.relative_path AS drive_relative_path" in EXPLORER_SQL
    assert "AS locations" in EXPLORER_SQL
    assert "CAST(%(status)s AS text) IS NULL" in EXPLORER_SQL
    assert "CASE CAST(%(group_by)s AS text)" in EXPLORER_SQL
    assert "CAST(%(site)s AS text)='local'" in EXPLORER_SQL


def test_transfer_site_filter_does_not_accept_virtual_torrent_site():
    service = InventoryService(FakeDatabase())

    with pytest.raises(ValueError, match="site invalido"):
        service.transfers(site="torrent")


def test_public_transfer_query_redacts_internal_manifests_and_resume_url():
    assert "cardinality(selected_file_ids) AS file_count" in TRANSFERS_SQL
    assert "jsonb_array_length(local_files)" in TRANSFERS_SQL
    assert "jsonb_array_length(drive_files)" in TRANSFERS_SQL
    selected_projection = TRANSFERS_SQL.partition("FROM runtime.transfer_jobs")[0]
    assert "upload_state" not in selected_projection
    assert "selected_file_ids," not in selected_projection


def test_nullable_filters_have_explicit_postgres_types():
    assert "CAST(%(site)s AS text) IS NULL" in EXPLORER_SQL
    assert "CAST(%(kind)s AS text) IS NULL" in EXPLORER_SQL
    assert "CAST(%(q)s AS text) IS NULL" in EXPLORER_SQL
    assert "CAST(%(presence)s AS text) IS NULL" in EXPLORER_SQL
    assert "CAST(%(status)s AS text) IS NULL" in EXPLORER_SQL
    assert "CAST(%(group_by)s AS text)" in EXPLORER_SQL
    assert "CAST(%(state)s AS text) IS NULL" in TRANSFERS_SQL
    assert "CAST(%(target)s AS text) IS NULL" in TRANSFERS_SQL
    assert "CAST(%(infohash)s AS text) IS NULL" in TRANSFERS_SQL


def test_inventory_filters_and_pagination_are_bounded():
    service = InventoryService(FakeDatabase())
    with pytest.raises(ValueError, match="kind invalido"):
        service.explorer(kind="executable")
    with pytest.raises(ValueError, match="page_size"):
        service.explorer(page_size=1000)
    with pytest.raises(ValueError, match="infohash"):
        service.transfers(infohash="not-an-infohash")
