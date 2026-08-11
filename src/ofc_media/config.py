from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1) -> int:
    return max(int(os.environ.get(name, default)), minimum)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str
    internal_token: str
    session_pepper: str
    public_base_url: str
    torrent_engine_url: str
    drive_source_url: str
    transcoder_url: str
    filecr_db: Path
    x1337_db: Path
    metadata_db: Path
    subtitle_db: Path
    snapshot_root: Path
    filecr_torrent_root: Path
    x1337_torrent_root: Path
    filecr_host_torrent_root: str
    x1337_host_torrent_root: str
    media_root: Path
    resume_root: Path
    hls_root: Path
    sync_interval: int
    transcode_encoder: str
    max_transcodes: int
    vendor_hls_path: Path
    subtitle_file_root: Path
    subtitle_host_root: str
    gdrive_root_id: str
    gdrive_token_path: Path
    gdrive_sync_interval: int
    playback_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://ofc:ofc@127.0.0.1:5432/ofc_media"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            internal_token=os.environ.get("OFC_INTERNAL_TOKEN", ""),
            session_pepper=os.environ.get("OFC_SESSION_PEPPER", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5090"),
            torrent_engine_url=os.environ.get(
                "TORRENT_ENGINE_URL", "http://127.0.0.1:7101"
            ).rstrip("/"),
            drive_source_url=os.environ.get(
                "DRIVE_SOURCE_URL", "http://127.0.0.1:7103"
            ).rstrip("/"),
            transcoder_url=os.environ.get(
                "TRANSCODER_URL", "http://127.0.0.1:7102"
            ).rstrip("/"),
            filecr_db=Path(os.environ.get("FILECR_DB", "/sources/filecr/inventory.sqlite3")),
            x1337_db=Path(os.environ.get("X1337_DB", "/sources/1337x/inventory.sqlite3")),
            metadata_db=Path(
                os.environ.get("METADATA_DB", "/sources/web/catalog_metadata.sqlite3")
            ),
            subtitle_db=Path(
                os.environ.get("SUBTITLE_DB", "/sources/subtitles/subtitles.sqlite3")
            ),
            snapshot_root=Path(os.environ.get("SNAPSHOT_ROOT", "/snapshots")),
            filecr_torrent_root=Path(
                os.environ.get("FILECR_TORRENT_ROOT", "/torrent-sources/filecr")
            ),
            x1337_torrent_root=Path(
                os.environ.get("X1337_TORRENT_ROOT", "/torrent-sources/1337x")
            ),
            filecr_host_torrent_root=os.environ.get(
                "FILECR_HOST_TORRENT_ROOT", "D:/dev/Torrents/FileCR/torrents"
            ),
            x1337_host_torrent_root=os.environ.get(
                "X1337_HOST_TORRENT_ROOT", "D:/dev/Torrents/1337xVault/downloads"
            ),
            media_root=Path(os.environ.get("MEDIA_ROOT", "/media")),
            resume_root=Path(os.environ.get("RESUME_ROOT", "/resume")),
            hls_root=Path(os.environ.get("HLS_ROOT", "/hls")),
            sync_interval=_integer("OFC_SYNC_INTERVAL", 120, 30),
            transcode_encoder=os.environ.get("OFC_TRANSCODE_ENCODER", "auto"),
            max_transcodes=_integer("OFC_MAX_TRANSCODES", 1, 1),
            vendor_hls_path=Path(os.environ.get("VENDOR_HLS_PATH", "/app/vendor/hls.mjs")),
            subtitle_file_root=Path(
                os.environ.get("SUBTITLE_FILE_ROOT", "/subtitle-files")
            ),
            subtitle_host_root=os.environ.get(
                "SUBTITLE_HOST_ROOT", "D:/dev/Torrents/SubtitleVault/subtitles"
            ),
            gdrive_root_id=os.environ.get("OFC_GDRIVE_ROOT_ID", ""),
            gdrive_token_path=Path(
                os.environ.get("OFC_GDRIVE_TOKEN_PATH", "/run/secrets/gdrive/token.json")
            ),
            gdrive_sync_interval=_integer("OFC_GDRIVE_SYNC_INTERVAL", 300, 30),
            playback_ttl_seconds=_integer("OFC_PLAYBACK_TTL_SECONDS", 43_200, 300),
        )

    def validate_secrets(self) -> None:
        if len(self.internal_token) < 32 or len(self.session_pepper) < 32:
            raise RuntimeError("tokens internos devem ter ao menos 32 caracteres")
