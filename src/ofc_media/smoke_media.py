from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from .media import MediaToolchain


def _encode(
    media: MediaToolchain,
    root: Path,
    source: Path,
    output: Path,
    mode: str,
) -> dict[str, object]:
    probe = media.probe(str(source))
    plan = media.plan(probe, mode=mode)
    command = media.command(source=str(source), output_root=output, probe=probe, plan=plan)
    started = time.monotonic()
    encoded = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = round(time.monotonic() - started, 3)
    if encoded.returncode or not media.ready(output, plan):
        diagnostics = {
            "command": command,
            "output_exists": output.exists(),
            "directories": {
                item.name: (output / item.name).is_dir() for item in plan.renditions
            },
            "files": [str(path.relative_to(root)) for path in root.rglob("*")],
        }
        raise RuntimeError(
            (encoded.stderr[-3000:] or "HLS sintetico incompleto")
            + "\n"
            + json.dumps(diagnostics, ensure_ascii=False)
        )
    master = (output / "master.m3u8").read_text(encoding="utf-8")
    return {
        "encoder": plan.encoder,
        "strategy": plan.strategy,
        "renditions": [item.name for item in plan.renditions],
        "master_variants": master.count("#EXT-X-STREAM-INF"),
        "segments": {
            item.name: len(list((output / item.name).glob("seg_*.ts")))
            for item in plan.renditions
        },
        "elapsed_seconds": elapsed,
    }


def main() -> None:
    media = MediaToolchain("auto")
    capabilities = media.capabilities()
    if capabilities.get("selected_encoder") != "h264_nvenc":
        raise RuntimeError(f"NVENC indisponivel: {capabilities!r}")

    with tempfile.TemporaryDirectory(prefix="ofc-media-smoke-") as temporary:
        root = Path(temporary)
        source = root / "source.mp4"
        generated = subprocess.run(
            [
                media.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1280x720:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000",
                "-t",
                "8",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if generated.returncode:
            raise RuntimeError(generated.stderr[-3000:])

        ddp_source = root / "source-ddp.mkv"
        converted = subprocess.run(
            [
                media.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-c:v",
                "copy",
                "-c:a",
                "eac3",
                str(ddp_source),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if converted.returncode:
            raise RuntimeError(converted.stderr[-3000:])

        result = {
            "adaptive": _encode(media, root, source, root / "adaptive", "adaptive"),
            "remux": _encode(media, root, source, root / "remux", "auto"),
            "audio_only": _encode(media, root, ddp_source, root / "audio-only", "auto"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
