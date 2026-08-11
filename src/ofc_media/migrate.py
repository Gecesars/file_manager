from __future__ import annotations

import logging

import psycopg

from .config import Settings


LOG = logging.getLogger("ofc.migrate")

SCHEMA_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS runtime;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.schema_migrations(
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS catalog.sources(
    site TEXT PRIMARY KEY CHECK (site IN ('filecr','1337x','gdrive','metadata','subtitles')),
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    last_snapshot_at TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    source_bytes BIGINT,
    source_mtime_ns BIGINT,
    row_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS catalog.torrents(
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL CHECK (site IN ('filecr','1337x','gdrive')),
    infohash CHAR(40) NOT NULL CHECK (infohash ~ '^[0-9a-f]{40}$'),
    sha256 CHAR(64),
    source_url TEXT NOT NULL,
    download_url TEXT,
    metainfo_relpath TEXT NOT NULL,
    display_name TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    uploader TEXT,
    total_size BIGINT NOT NULL CHECK (total_size >= 0),
    file_count INTEGER NOT NULL CHECK (file_count >= 0),
    metainfo_size INTEGER,
    piece_length INTEGER,
    torrent_version TEXT,
    seeders INTEGER,
    leechers INTEGER,
    peer_count INTEGER,
    downloaded_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(site, infohash)
);
CREATE INDEX IF NOT EXISTS torrents_title_trgm
    ON catalog.torrents USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS torrents_category ON catalog.torrents(site, category);
CREATE INDEX IF NOT EXISTS torrents_popular
    ON catalog.torrents(site, seeders DESC NULLS LAST, peer_count DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS catalog.torrent_files(
    id BIGSERIAL PRIMARY KEY,
    torrent_id BIGINT NOT NULL REFERENCES catalog.torrents(id) ON DELETE CASCADE,
    file_index INTEGER,
    path TEXT NOT NULL,
    extension TEXT NOT NULL DEFAULT '',
    file_kind TEXT NOT NULL DEFAULT 'other'
        CHECK (file_kind IN ('video','audio','subtitle','image','document',
                             'archive','software','dataset','other')),
    mime_type TEXT,
    size BIGINT NOT NULL CHECK (size >= 0),
    is_video BOOLEAN NOT NULL DEFAULT FALSE,
    is_subtitle BOOLEAN NOT NULL DEFAULT FALSE,
    sha256 CHAR(64) CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    media_signature_valid BOOLEAN,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(torrent_id, path)
);
CREATE INDEX IF NOT EXISTS torrent_files_video
    ON catalog.torrent_files(torrent_id, is_video, size DESC);

-- Compatibilidade com bancos criados pelas versoes 1 e 2. ADD COLUMN IF NOT
-- EXISTS mantem a mesma definicao segura tanto em instalacoes novas quanto em
-- atualizacoes, sem recriar a tabela de centenas de milhares de arquivos.
ALTER TABLE catalog.torrent_files
    ADD COLUMN IF NOT EXISTS file_kind TEXT NOT NULL DEFAULT 'other';
ALTER TABLE catalog.torrent_files
    ADD COLUMN IF NOT EXISTS mime_type TEXT;
ALTER TABLE catalog.torrent_files
    ADD COLUMN IF NOT EXISTS is_subtitle BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE catalog.torrent_files
    ADD COLUMN IF NOT EXISTS sha256 CHAR(64);
ALTER TABLE catalog.torrent_files ALTER COLUMN file_kind SET DEFAULT 'other';
ALTER TABLE catalog.torrent_files ALTER COLUMN is_subtitle SET DEFAULT FALSE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid='catalog.torrent_files'::regclass
          AND attname='file_kind' AND NOT attnotnull AND NOT attisdropped
    ) THEN
        UPDATE catalog.torrent_files SET file_kind='other' WHERE file_kind IS NULL;
        ALTER TABLE catalog.torrent_files ALTER COLUMN file_kind SET NOT NULL;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid='catalog.torrent_files'::regclass
          AND attname='is_subtitle' AND NOT attnotnull AND NOT attisdropped
    ) THEN
        UPDATE catalog.torrent_files SET is_subtitle=FALSE WHERE is_subtitle IS NULL;
        ALTER TABLE catalog.torrent_files ALTER COLUMN is_subtitle SET NOT NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='catalog.torrent_files'::regclass
          AND conname='torrent_files_file_kind_check'
    ) THEN
        ALTER TABLE catalog.torrent_files
            ADD CONSTRAINT torrent_files_file_kind_check
            CHECK (file_kind IN ('video','audio','subtitle','image','document',
                                 'archive','software','dataset','other'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='catalog.torrent_files'::regclass
          AND conname='torrent_files_sha256_check'
    ) THEN
        ALTER TABLE catalog.torrent_files
            ADD CONSTRAINT torrent_files_sha256_check
            CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$');
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS torrent_files_kind_size
    ON catalog.torrent_files(file_kind, size DESC, id);
CREATE INDEX IF NOT EXISTS torrent_files_sha256_size
    ON catalog.torrent_files(sha256, size) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS torrent_files_subtitle
    ON catalog.torrent_files(torrent_id, size DESC) WHERE is_subtitle;
CREATE INDEX IF NOT EXISTS torrent_files_file_index
    ON catalog.torrent_files(torrent_id, file_index);
CREATE INDEX IF NOT EXISTS torrent_files_normalized_name_size
    ON catalog.torrent_files(
        (lower(regexp_replace(regexp_replace(path, '^.*/', ''),
                              '[^[:alnum:]]+', '', 'g'))), size
    );
CREATE INDEX IF NOT EXISTS torrent_files_path_trgm
    ON catalog.torrent_files USING gin(path gin_trgm_ops);

-- Backfill executado uma unica vez para que o explorador seja util logo apos
-- atualizar uma instalacao v2. Ingestoes futuras usam os helpers Python.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ops.schema_migrations WHERE version=3) THEN
        UPDATE catalog.torrent_files
        SET
            extension=lower(extension),
            file_kind=CASE
                WHEN file_kind <> 'other' THEN file_kind
                WHEN is_video OR lower(extension)=ANY(ARRAY[
                    '.3gp','.asf','.avi','.divx','.flv','.m2ts','.m4v','.mkv',
                    '.mov','.mp4','.mpeg','.mpg','.mts','.ogv','.rm','.rmvb',
                    '.ts','.vob','.webm','.wmv'
                ]) THEN 'video'
                WHEN lower(extension)=ANY(ARRAY[
                    '.aac','.ac3','.aiff','.alac','.ape','.dts','.flac','.m4a',
                    '.mka','.mp3','.oga','.ogg','.opus','.wav','.wma'
                ]) THEN 'audio'
                WHEN lower(extension)=ANY(ARRAY[
                    '.ass','.dfxp','.idx','.lrc','.smi','.srt','.ssa','.sub',
                    '.sup','.ttml','.vtt'
                ]) THEN 'subtitle'
                WHEN lower(extension)=ANY(ARRAY[
                    '.avif','.bmp','.gif','.heic','.heif','.ico','.jpeg','.jpg',
                    '.jxl','.png','.svg','.tif','.tiff','.webp'
                ]) THEN 'image'
                WHEN lower(extension)=ANY(ARRAY[
                    '.azw','.azw3','.doc','.docx','.epub','.html','.htm','.md',
                    '.mobi','.odp','.ods','.odt','.pdf','.ppt','.pptx','.rtf',
                    '.tex','.txt','.xls','.xlsx'
                ]) THEN 'document'
                WHEN lower(extension)=ANY(ARRAY[
                    '.7z','.bz2','.cab','.gz','.img','.iso','.rar','.tar',
                    '.tar.bz2','.tar.gz','.tar.xz','.tar.zst','.tbz2','.tgz',
                    '.txz','.xz','.zip','.zst'
                ]) THEN 'archive'
                WHEN lower(extension)=ANY(ARRAY[
                    '.apk','.appimage','.bat','.bin','.cmd','.deb','.dll','.dmg',
                    '.dylib','.exe','.ipa','.jar','.msi','.msix','.pkg','.ps1',
                    '.rpm','.sh','.so','.whl'
                ]) THEN 'software'
                WHEN lower(extension)=ANY(ARRAY[
                    '.avro','.csv','.db','.feather','.geojson','.h5','.hdf5',
                    '.json','.jsonl','.ndjson','.npy','.npz','.orc','.parquet',
                    '.pkl','.sql','.sqlite','.sqlite3','.tsv','.xml','.yaml','.yml'
                ]) THEN 'dataset'
                ELSE 'other'
            END,
            is_subtitle=is_subtitle OR lower(extension)=ANY(ARRAY[
                '.ass','.dfxp','.idx','.lrc','.smi','.srt','.ssa','.sub',
                '.sup','.ttml','.vtt'
            ]),
            mime_type=COALESCE(mime_type, CASE lower(extension)
                WHEN '.mkv' THEN 'video/x-matroska'
                WHEN '.mp4' THEN 'video/mp4'
                WHEN '.mp3' THEN 'audio/mpeg'
                WHEN '.flac' THEN 'audio/flac'
                WHEN '.srt' THEN 'application/x-subrip'
                WHEN '.vtt' THEN 'text/vtt'
                WHEN '.pdf' THEN 'application/pdf'
                WHEN '.zip' THEN 'application/zip'
                WHEN '.json' THEN 'application/json'
                ELSE NULL
            END);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS catalog.drive_files(
    drive_file_id TEXT PRIMARY KEY CHECK (drive_file_id ~ '^[A-Za-z0-9_-]{10,200}$'),
    torrent_file_id BIGINT NOT NULL UNIQUE
        REFERENCES catalog.torrent_files(id) ON DELETE CASCADE,
    folder_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    md5_checksum TEXT,
    modified_time TIMESTAMPTZ,
    can_download BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS drive_files_active
    ON catalog.drive_files(active, modified_time DESC);

-- Bancos existentes nasceram antes da fonte Google Drive. Recriar somente as
-- restricoes de dominio torna a migracao idempotente sem tocar nos dados.
ALTER TABLE catalog.sources DROP CONSTRAINT IF EXISTS sources_site_check;
ALTER TABLE catalog.sources ADD CONSTRAINT sources_site_check
    CHECK (site IN ('filecr','1337x','gdrive','metadata','subtitles'));
ALTER TABLE catalog.torrents DROP CONSTRAINT IF EXISTS torrents_site_check;
ALTER TABLE catalog.torrents ADD CONSTRAINT torrents_site_check
    CHECK (site IN ('filecr','1337x','gdrive'));

CREATE TABLE IF NOT EXISTS catalog.metadata(
    site TEXT NOT NULL,
    infohash CHAR(40) NOT NULL,
    source_title TEXT,
    category TEXT,
    media_kind TEXT,
    query_title TEXT,
    query_year INTEGER,
    query_type TEXT,
    query_imdb_id TEXT,
    source TEXT,
    status TEXT NOT NULL,
    imdb_id TEXT,
    canonical_title TEXT,
    release_year TEXT,
    media_type TEXT,
    description TEXT,
    imdb_rating REAL,
    imdb_votes INTEGER,
    fetched_at BIGINT,
    retry_after BIGINT,
    error TEXT,
    source_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(site, infohash)
);
CREATE INDEX IF NOT EXISTS metadata_title_trgm
    ON catalog.metadata USING gin (canonical_title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS metadata_rating
    ON catalog.metadata(imdb_rating DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS catalog.subtitles(
    site TEXT NOT NULL,
    infohash CHAR(40) NOT NULL,
    torrent_path TEXT NOT NULL,
    language TEXT NOT NULL,
    file_name TEXT NOT NULL,
    normalized_name TEXT,
    extension TEXT,
    size BIGINT,
    season INTEGER,
    episode INTEGER,
    media_path TEXT,
    match_method TEXT,
    match_confidence REAL,
    status TEXT NOT NULL,
    provider TEXT,
    subtitle_path TEXT,
    synced_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_updated_at BIGINT,
    source_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(site, infohash, torrent_path, language)
);

CREATE TABLE IF NOT EXISTS catalog.swarm_samples(
    id BIGSERIAL,
    torrent_id BIGINT NOT NULL REFERENCES catalog.torrents(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    seeders INTEGER,
    leechers INTEGER,
    peers INTEGER,
    source TEXT NOT NULL,
    PRIMARY KEY(id, observed_at)
) PARTITION BY RANGE(observed_at);
CREATE TABLE IF NOT EXISTS catalog.swarm_samples_default
    PARTITION OF catalog.swarm_samples DEFAULT;
CREATE INDEX IF NOT EXISTS swarm_torrent_time
    ON catalog.swarm_samples_default(torrent_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS ops.ingestion_runs(
    id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    source_bytes BIGINT,
    source_mtime_ns BIGINT,
    rows_read BIGINT NOT NULL DEFAULT 0,
    rows_written BIGINT NOT NULL DEFAULT 0,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    checksum TEXT,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ops.ingest_checkpoints(
    source TEXT PRIMARY KEY,
    watermark TEXT,
    source_bytes BIGINT,
    source_mtime_ns BIGINT,
    checksum TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ops.service_heartbeats(
    service TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.drive_cursors(
    cursor_key TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL DEFAULT 'default',
    root_folder_id TEXT,
    page_token TEXT,
    pending_page_token TEXT,
    last_polled_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS drive_cursors_poll
    ON ops.drive_cursors(last_polled_at NULLS FIRST);

CREATE TABLE IF NOT EXISTS ops.audit_events(
    id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    correlation_id UUID,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_events_created
    ON ops.audit_events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_events_entity
    ON ops.audit_events(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_correlation
    ON ops.audit_events(correlation_id) WHERE correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime.playback_sessions(
    id UUID PRIMARY KEY,
    site TEXT NOT NULL CHECK (site IN ('filecr','1337x','gdrive')),
    infohash CHAR(40) NOT NULL,
    torrent_file_id BIGINT NOT NULL REFERENCES catalog.torrent_files(id),
    token_hash CHAR(64) NOT NULL,
    state TEXT NOT NULL,
    strategy TEXT,
    selected_profile TEXT NOT NULL DEFAULT 'auto',
    source_bitrate BIGINT,
    target_bitrate BIGINT,
    buffer_target_seconds INTEGER NOT NULL DEFAULT 30,
    download_rate_bps BIGINT NOT NULL DEFAULT 0,
    verified_buffer_bytes BIGINT NOT NULL DEFAULT 0,
    media_probe JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_item
    ON runtime.playback_sessions(site, infohash, created_at DESC);

ALTER TABLE runtime.playback_sessions
    DROP CONSTRAINT IF EXISTS playback_sessions_site_check;
ALTER TABLE runtime.playback_sessions
    ADD CONSTRAINT playback_sessions_site_check
    CHECK (site IN ('filecr','1337x','gdrive'));

CREATE TABLE IF NOT EXISTS runtime.download_jobs(
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES runtime.playback_sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS runtime.transcode_jobs(
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES runtime.playback_sessions(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    encoder TEXT,
    profiles JSONB NOT NULL DEFAULT '[]'::jsonb,
    state TEXT NOT NULL,
    process_id INTEGER,
    command_fingerprint CHAR(64),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS runtime.stream_artifacts(
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES runtime.playback_sessions(id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL,
    profile TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(session_id, relative_path)
);

CREATE TABLE IF NOT EXISTS runtime.transfer_jobs(
    id UUID PRIMARY KEY,
    source_site TEXT NOT NULL
        CHECK (source_site IN ('filecr','1337x','gdrive')),
    infohash CHAR(40) NOT NULL CHECK (infohash ~ '^[0-9a-f]{40}$'),
    target TEXT NOT NULL CHECK (target IN ('local','gdrive')),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','validating','downloading','downloaded',
                         'classifying','uploading','verifying','completed',
                         'failed','cancelled')),
    selected_file_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[]
        CHECK (array_position(selected_file_ids, NULL) IS NULL
               AND 0 < ALL(selected_file_ids)),
    destination_path TEXT NOT NULL DEFAULT '',
    bytes_total BIGINT NOT NULL DEFAULT 0 CHECK (bytes_total >= 0),
    bytes_done BIGINT NOT NULL DEFAULT 0
        CHECK (bytes_done >= 0 AND bytes_done <= bytes_total),
    local_files JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(local_files)='array'),
    external_files JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(external_files)='array'),
    drive_files JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(drive_files)='array'),
    upload_state JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(upload_state)='object'),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (source_site, infohash)
        REFERENCES catalog.torrents(site, infohash) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS transfer_jobs_queue
    ON runtime.transfer_jobs(state, created_at, id);
CREATE INDEX IF NOT EXISTS transfer_jobs_target_state
    ON runtime.transfer_jobs(target, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS transfer_jobs_source
    ON runtime.transfer_jobs(source_site, infohash, created_at DESC);
CREATE INDEX IF NOT EXISTS transfer_jobs_selected_files
    ON runtime.transfer_jobs USING gin(selected_file_ids);

-- A curadoria pode anexar legendas ja validadas no SubtitleVault ao mesmo
-- upload, sem mistura-las ao inventario imutavel do torrent.
ALTER TABLE runtime.transfer_jobs
    ADD COLUMN IF NOT EXISTS external_files JSONB NOT NULL DEFAULT '[]'::jsonb;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='runtime.transfer_jobs'::regclass
          AND conname='transfer_jobs_external_files_check'
    ) THEN
        ALTER TABLE runtime.transfer_jobs
            ADD CONSTRAINT transfer_jobs_external_files_check
            CHECK (jsonb_typeof(external_files)='array');
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION runtime.guard_transfer_job_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    transition_allowed BOOLEAN := FALSE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.state <> 'queued' THEN
            RAISE EXCEPTION 'transfer job deve iniciar em queued, recebeu %', NEW.state;
        END IF;
        NEW.created_at := COALESCE(NEW.created_at, now());
        NEW.updated_at := COALESCE(NEW.updated_at, NEW.created_at);
        RETURN NEW;
    END IF;

    NEW.updated_at := now();
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;

    transition_allowed := CASE OLD.state
        WHEN 'queued' THEN NEW.state IN ('validating','failed','cancelled')
        WHEN 'validating' THEN NEW.state IN ('downloading','downloaded','failed','cancelled')
        WHEN 'downloading' THEN NEW.state IN ('downloaded','failed','cancelled')
        WHEN 'downloaded' THEN NEW.state IN ('classifying','failed','cancelled')
        WHEN 'classifying' THEN NEW.state IN ('uploading','verifying','failed','cancelled')
        WHEN 'uploading' THEN NEW.state IN ('verifying','failed','cancelled')
        WHEN 'verifying' THEN NEW.state IN ('completed','failed','cancelled')
        WHEN 'failed' THEN NEW.state = 'queued'
        ELSE FALSE
    END;
    IF NOT transition_allowed THEN
        RAISE EXCEPTION 'transicao de transfer job invalida: % -> %', OLD.state, NEW.state;
    END IF;
    IF NEW.state='uploading' AND NEW.target <> 'gdrive' THEN
        RAISE EXCEPTION 'estado uploading exige target gdrive';
    END IF;
    IF NEW.state='validating' THEN
        NEW.started_at := COALESCE(NEW.started_at, now());
    ELSIF NEW.state IN ('completed','failed','cancelled') THEN
        NEW.finished_at := COALESCE(NEW.finished_at, now());
    ELSIF NEW.state='queued' THEN
        NEW.started_at := NULL;
        NEW.finished_at := NULL;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS transfer_jobs_state_guard ON runtime.transfer_jobs;
CREATE TRIGGER transfer_jobs_state_guard
BEFORE INSERT OR UPDATE ON runtime.transfer_jobs
FOR EACH ROW EXECUTE FUNCTION runtime.guard_transfer_job_state();

CREATE OR REPLACE VIEW catalog.video_catalog AS
SELECT
    t.id AS torrent_id, t.site, t.infohash, t.title, t.display_name,
    t.category, t.seeders, t.leechers, t.peer_count, t.total_size,
    t.file_count, t.downloaded_at,
    COALESCE(m.canonical_title, t.title) AS canonical_title,
    m.release_year, m.media_type, m.description, m.imdb_rating,
    m.imdb_votes, m.imdb_id,
    count(f.id) FILTER (
        WHERE f.is_video
          AND (
              t.site <> 'gdrive'
              OR EXISTS (
                  SELECT 1 FROM catalog.drive_files d
                  WHERE d.torrent_file_id=f.id
                    AND d.active AND d.can_download
              )
          )
    ) AS video_count
FROM catalog.torrents t
LEFT JOIN catalog.metadata m ON m.site=t.site AND m.infohash=t.infohash
LEFT JOIN catalog.torrent_files f ON f.torrent_id=t.id
WHERE t.active
GROUP BY t.id, m.canonical_title, m.release_year, m.media_type,
         m.description, m.imdb_rating, m.imdb_votes, m.imdb_id;

INSERT INTO ops.schema_migrations(version, description)
VALUES (1, 'schema inicial da OFC Media Platform v1')
ON CONFLICT(version) DO NOTHING;
INSERT INTO ops.schema_migrations(version, description)
VALUES (2, 'fonte Google Drive somente leitura')
ON CONFLICT(version) DO NOTHING;
INSERT INTO ops.schema_migrations(version, description)
VALUES (3, 'inventario canonico e transferencias locais/Google Drive')
ON CONFLICT(version) DO NOTHING;
INSERT INTO ops.schema_migrations(version, description)
VALUES (4, 'curadoria de midia e legendas externas validadas')
ON CONFLICT(version) DO NOTHING;
"""


def migrate(settings: Settings | None = None) -> None:
    selected = settings or Settings.from_env()
    with psycopg.connect(selected.database_url, autocommit=True) as database:
        database.execute(SCHEMA_SQL)
    LOG.info("schema v4 aplicado")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    migrate()


if __name__ == "__main__":
    main()
