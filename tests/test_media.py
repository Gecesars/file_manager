from pathlib import Path
from types import SimpleNamespace

from ofc_media.media import MediaToolchain


def probe(video="h264", audio="aac", pixel="yuv420p", height=1080, bitrate=5_000_000):
    return {
        "format": {"bit_rate": str(bitrate), "duration": "60"},
        "streams": [
            {"codec_type": "video", "codec_name": video, "pix_fmt": pixel, "height": height},
            {"codec_type": "audio", "codec_name": audio},
        ],
    }


def toolchain() -> MediaToolchain:
    media = MediaToolchain()
    media.ffmpeg = "ffmpeg"
    media.ffprobe = "ffprobe"
    media._capabilities = {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "encoders": {"h264_nvenc": {"available": True, "error": None}},
        "selected_encoder": "h264_nvenc",
    }
    return media


def test_h264_aac_is_remuxed_without_video_encode(tmp_path: Path):
    media = toolchain()
    plan = media.plan(probe(), mode="auto")
    command = media.command(source="http://source", output_root=tmp_path, probe=probe(), plan=plan)
    assert plan.strategy == "remux"
    assert ["-c:v", "copy"] == command[command.index("-c:v") : command.index("-c:v") + 2]


def test_h264_ddp_converts_only_audio():
    plan = toolchain().plan(probe(audio="eac3"), mode="auto")
    assert plan.strategy == "audio_transcode"
    assert plan.video_copy is True
    assert plan.audio_copy is False


def test_incompatible_video_uses_nvenc_adaptive_ladder():
    plan = toolchain().plan(probe(video="hevc"), mode="adaptive")
    assert plan.encoder == "h264_nvenc"
    assert [item.name for item in plan.renditions] == ["1080p", "720p", "480p"]


def test_hls_mpegts_names_are_resolved_from_absolute_variant_output(tmp_path: Path):
    media = toolchain()
    source_probe = probe(video="hevc", height=720)
    plan = media.plan(source_probe, mode="adaptive")
    command = media.command(
        source="http://source", output_root=tmp_path, probe=source_probe, plan=plan
    )
    index = command.index("-master_pl_name")
    assert command[index + 1] == "master.m3u8"
    segment_type = command.index("-hls_segment_type")
    assert command[segment_type + 1] == "mpegts"
    segment_name = command.index("-hls_segment_filename")
    assert command[segment_name + 1] == str(tmp_path / "%v" / "seg_%06d.ts")
    audio_channels = command.index("-ac:a:0")
    assert command[audio_channels + 1] == "2"
    audio_rate = command.index("-ar:a:0")
    assert command[audio_rate + 1] == "48000"
    playlist = command[-1]
    assert playlist == str(tmp_path / "%v" / "index.m3u8")


def test_encoder_probe_uses_supported_dimensions(monkeypatch):
    media = MediaToolchain()
    media.ffmpeg = "ffmpeg"
    captured = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", run)
    works, error = media._encoder_works("h264_nvenc")
    assert works and error is None
    assert "testsrc2=size=640x360:rate=30" in captured["command"]
