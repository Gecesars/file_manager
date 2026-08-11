from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
GPU_OVERRIDE = (ROOT / "compose.gpu.yaml").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
WSL_ENV_EXAMPLE = (ROOT / ".env.wsl.example").read_text(encoding="utf-8")
WINDOWS_CONFIGURATOR = (ROOT / "scripts" / "configurar.ps1").read_text(
    encoding="utf-8"
)
APP_CONFIG = (ROOT / "src" / "ofc_media" / "config.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _service(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        COMPOSE,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"servico ausente: {name}"
    return match.group("body")


def test_all_services_have_conservative_compose_memory_limits() -> None:
    expected = {
        "postgres": ("1280m", "384m"),
        "redis": ("384m", "96m"),
        "migrate": ("512m", "96m"),
        "catalog-sync": ("512m", "128m"),
        "torrent-engine": ("1536m", "384m"),
        "gdrive-source": ("512m", "128m"),
        "transcoder": ("2048m", "256m"),
        "control": ("512m", "128m"),
        "gateway": ("128m", "32m"),
    }

    for service, (limit, reservation) in expected.items():
        body = _service(service)
        assert re.search(rf"^    mem_limit: {re.escape(limit)}$", body, re.MULTILINE)
        assert re.search(
            rf"^    mem_reservation: {re.escape(reservation)}$", body, re.MULTILINE
        )


def test_redis_dataset_limit_leaves_process_headroom() -> None:
    body = _service("redis")
    assert '"--maxmemory", "256mb"' in body
    assert '"--maxmemory-policy", "allkeys-lru"' in body
    assert '"--save", ""' in body
    assert '"--appendonly", "no"' in body


def test_deploy_limits_match_compose_limits() -> None:
    for service, limit in {
        "torrent-engine": "1536m",
        "gdrive-source": "512m",
        "transcoder": "2048m",
    }.items():
        body = _service(service)
        assert re.search(rf"^          memory: {re.escape(limit)}$", body, re.MULTILINE)


def test_catalog_sync_defaults_to_ten_minutes() -> None:
    assert "OFC_SYNC_INTERVAL: ${OFC_SYNC_INTERVAL:-600}" in _service("catalog-sync")
    assert "OFC_SYNC_INTERVAL=600" in ENV_EXAMPLE
    assert "OFC_SYNC_INTERVAL=600" in WSL_ENV_EXAMPLE
    assert "'OFC_SYNC_INTERVAL=600'" in WINDOWS_CONFIGURATOR
    assert '_integer("OFC_SYNC_INTERVAL", 600, 30)' in APP_CONFIG
    assert "`OFC_SYNC_INTERVAL=600`" in README


def test_windows_configurator_persists_runtime_memory_guards() -> None:
    assert "'OFC_MAX_ACTIVE_TORRENTS=2'" in WINDOWS_CONFIGURATOR
    assert "'OFC_MAX_TRANSCODE_QUEUE=1'" in WINDOWS_CONFIGURATOR
    assert "'OFC_FFMPEG_LOG_TAIL_BYTES=65536'" in WINDOWS_CONFIGURATOR


def test_control_uses_one_threaded_worker_to_avoid_duplicate_app_heaps() -> None:
    body = _service("control")
    assert '"--workers", "1", "--threads", "8"' in body


def test_cpu_is_default_and_gpu_requires_explicit_override() -> None:
    transcoder = _service("transcoder")
    assert "OFC_TRANSCODE_ENCODER: libx264" in transcoder
    assert "gpus:" not in COMPOSE
    assert "gpus: all" in GPU_OVERRIDE
    assert "OFC_TRANSCODE_ENCODER: h264_nvenc" in GPU_OVERRIDE
    assert "OFC_TRANSCODE_ENCODER=libx264" in ENV_EXAMPLE
    assert "OFC_TRANSCODE_ENCODER=libx264" in WSL_ENV_EXAMPLE
    assert "'OFC_TRANSCODE_ENCODER=libx264'" in WINDOWS_CONFIGURATOR
    assert 'os.environ.get("OFC_TRANSCODE_ENCODER", "libx264")' in APP_CONFIG
