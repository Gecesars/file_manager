from pathlib import Path

import pytest

from ofc_media.subtitle_tracks import resolve_subtitle_path, to_webvtt, track_id


def test_srt_is_converted_to_webvtt():
    payload = b"1\r\n00:00:01,250 --> 00:00:03,500\r\nOla!\r\n"
    assert to_webvtt(payload, ".srt") == (
        "WEBVTT\n\n1\n00:00:01.250 --> 00:00:03.500\nOla!\n"
    )


def test_invalid_srt_is_rejected():
    with pytest.raises(ValueError):
        to_webvtt(b"apenas texto", ".srt")


def test_host_path_is_mapped_inside_read_only_mount(tmp_path: Path):
    subtitle = tmp_path / "Filmes" / "Exemplo.pt-BR.srt"
    subtitle.parent.mkdir()
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nOi\n", encoding="utf-8")
    result = resolve_subtitle_path(
        r"D:\dev\Torrents\SubtitleVault\subtitles\Filmes\Exemplo.pt-BR.srt",
        mounted_root=tmp_path,
        host_root="D:/dev/Torrents/SubtitleVault/subtitles",
    )
    assert result == subtitle


def test_path_outside_configured_vault_is_rejected(tmp_path: Path):
    (tmp_path / "bad.srt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_subtitle_path(
            "D:/outro/bad.srt",
            mounted_root=tmp_path,
            host_root="D:/dev/Torrents/SubtitleVault/subtitles",
        )


def test_track_identifier_is_stable_and_opaque():
    first = track_id("1337x", "a" * 40, "movie.mkv", "pt-BR")
    assert first == track_id("1337x", "a" * 40, "movie.mkv", "pt-BR")
    assert len(first) == 24
