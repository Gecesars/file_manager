from __future__ import annotations

from ofc_media.config import Settings


def test_runtime_limit_defaults_and_minimums(monkeypatch):
    for name in (
        "OFC_MAX_ACTIVE_TORRENTS",
        "OFC_MAX_TRANSCODE_QUEUE",
        "OFC_FFMPEG_LOG_TAIL_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings.from_env()
    assert defaults.max_active_torrents == 2
    assert defaults.max_transcode_queue == 1
    assert defaults.ffmpeg_log_tail_bytes == 65_536

    monkeypatch.setenv("OFC_MAX_ACTIVE_TORRENTS", "0")
    monkeypatch.setenv("OFC_MAX_TRANSCODE_QUEUE", "-1")
    monkeypatch.setenv("OFC_FFMPEG_LOG_TAIL_BYTES", "1")
    bounded = Settings.from_env()
    assert bounded.max_active_torrents == 1
    assert bounded.max_transcode_queue == 0
    assert bounded.ffmpeg_log_tail_bytes == 4_096
