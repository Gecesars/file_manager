from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .safety import UnsafeMediaError, safe_owned_path


@dataclass(frozen=True, slots=True)
class Rendition:
    name: str
    height: int
    bitrate: int
    audio_bitrate: int = 128_000


LADDER = (
    Rendition("1080p", 1080, 6_000_000, 160_000),
    Rendition("720p", 720, 3_000_000, 128_000),
    Rendition("480p", 480, 1_200_000, 96_000),
    Rendition("360p", 360, 700_000, 80_000),
)


@dataclass(frozen=True, slots=True)
class MediaPlan:
    strategy: str
    encoder: str
    renditions: tuple[Rendition, ...]
    video_copy: bool
    audio_copy: bool
    source_bitrate: int

    def fingerprint(self) -> str:
        payload = {
            "strategy": self.strategy,
            "encoder": self.encoder,
            "renditions": [asdict(item) for item in self.renditions],
            "video_copy": self.video_copy,
            "audio_copy": self.audio_copy,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class MediaToolchain:
    def __init__(self, encoder_preference: str = "auto") -> None:
        self.ffmpeg = shutil.which("ffmpeg") or ""
        self.ffprobe = shutil.which("ffprobe") or ""
        self.encoder_preference = encoder_preference
        self._capabilities: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def _encoder_works(self, encoder: str) -> tuple[bool, str | None]:
        # 640x360 evita o falso negativo NVENC causado pelo antigo teste 64x64.
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-frames:v",
            "30",
            "-an",
            "-c:v",
            encoder,
        ]
        if encoder == "h264_nvenc":
            command.extend(["-preset", "p4"])
        command.extend(["-f", "null", os.devnull])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        error = result.stderr.strip()[-1000:] or None
        return result.returncode == 0, error

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            if self._capabilities is not None:
                return dict(self._capabilities)
            result: dict[str, Any] = {
                "ffmpeg": self.ffmpeg or None,
                "ffprobe": self.ffprobe or None,
                "encoders": {},
                "selected_encoder": "libx264",
            }
            if self.available:
                requested = (
                    [self.encoder_preference]
                    if self.encoder_preference not in {"", "auto"}
                    else ["h264_nvenc", "h264_qsv", "h264_vaapi"]
                )
                for encoder in requested:
                    works, error = self._encoder_works(encoder)
                    result["encoders"][encoder] = {"available": works, "error": error}
                    if works and result["selected_encoder"] == "libx264":
                        result["selected_encoder"] = encoder
            self._capabilities = result
            return dict(result)

    def probe(self, source: str) -> dict[str, Any]:
        if not self.ffprobe:
            raise RuntimeError("FFprobe indisponivel")
        result = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-of",
                "json",
                source,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 4 * 1024**2:
            raise UnsafeMediaError(result.stderr.strip()[-1000:] or "FFprobe rejeitou o video")
        payload = json.loads(result.stdout)
        if not any(item.get("codec_type") == "video" for item in payload.get("streams", [])):
            raise UnsafeMediaError("nenhuma faixa de video")
        return payload

    @staticmethod
    def _source(probe: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        video = next(item for item in probe.get("streams", []) if item.get("codec_type") == "video")
        audio = next(
            (item for item in probe.get("streams", []) if item.get("codec_type") == "audio"),
            None,
        )
        return video, audio

    def plan(
        self,
        probe: dict[str, Any],
        *,
        mode: str = "auto",
        quality_cap_bps: int = 0,
    ) -> MediaPlan:
        video, audio = self._source(probe)
        height = max(2, int(video.get("height") or 720))
        source_bitrate = int(
            video.get("bit_rate")
            or probe.get("format", {}).get("bit_rate")
            or 6_000_000
        )
        codec = str(video.get("codec_name") or "").casefold()
        pixel = str(video.get("pix_fmt") or "").casefold()
        browser_h264 = codec == "h264" and pixel in {"yuv420p", "yuvj420p", "nv12"}
        audio_codec = str((audio or {}).get("codec_name") or "").casefold()
        if browser_h264 and mode != "adaptive" and (
            not quality_cap_bps or source_bitrate <= quality_cap_bps
        ):
            name = f"{height - height % 2}p"
            rendition = Rendition(name, height - height % 2, source_bitrate)
            return MediaPlan(
                strategy="remux" if audio_codec in {"aac", ""} else "audio_transcode",
                encoder="copy",
                renditions=(rendition,),
                video_copy=True,
                audio_copy=audio_codec in {"aac", ""},
                source_bitrate=source_bitrate,
            )
        candidates = [item for item in LADDER if item.height <= height]
        if not candidates:
            candidates = [Rendition(f"{height - height % 2}p", height - height % 2, 600_000)]
        if quality_cap_bps:
            allowed = [item for item in candidates if item.bitrate <= quality_cap_bps]
            candidates = allowed or [candidates[-1]]
        encoder = str(self.capabilities()["selected_encoder"])
        if mode == "adaptive" and encoder != "libx264":
            selected = tuple(candidates[:3])
        else:
            selected = (candidates[0],)
        return MediaPlan(
            strategy="adaptive_transcode" if len(selected) > 1 else "transcode",
            encoder=encoder,
            renditions=selected,
            video_copy=False,
            audio_copy=False,
            source_bitrate=source_bitrate,
        )

    def command(
        self,
        *,
        source: str,
        output_root: Path,
        probe: dict[str, Any],
        plan: MediaPlan,
    ) -> list[str]:
        output_root.mkdir(parents=True, exist_ok=True)
        has_audio = any(item.get("codec_type") == "audio" for item in probe.get("streams", []))
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-i", source]
        stream_map: list[str] = []
        if plan.video_copy:
            rendition = plan.renditions[0]
            safe_owned_path(output_root, rendition.name).mkdir(parents=True, exist_ok=True)
            command.extend(["-map", "0:v:0", "-c:v", "copy"])
            if has_audio:
                command.extend(["-map", "0:a:0?"])
                command.extend(["-c:a", "copy" if plan.audio_copy else "aac"])
                if not plan.audio_copy:
                    command.extend(["-b:a", str(rendition.audio_bitrate)])
                stream_map.append(f"v:0,a:0,name:{rendition.name}")
            else:
                stream_map.append(f"v:0,name:{rendition.name}")
        else:
            count = len(plan.renditions)
            outputs = "".join(f"[v{index}]" for index in range(count))
            filters = [f"[0:v:0]split={count}{outputs}"]
            for index, rendition in enumerate(plan.renditions):
                safe_owned_path(output_root, rendition.name).mkdir(parents=True, exist_ok=True)
                filters.append(
                    f"[v{index}]scale=-2:{rendition.height}:force_original_aspect_ratio=decrease:force_divisible_by=2[v{index}out]"
                )
            command.extend(["-filter_complex", ";".join(filters)])
            for index, rendition in enumerate(plan.renditions):
                command.extend(
                    [
                        "-map",
                        f"[v{index}out]",
                        f"-c:v:{index}",
                        plan.encoder,
                        f"-preset:v:{index}",
                        "p4" if plan.encoder == "h264_nvenc" else "veryfast",
                        f"-pix_fmt:v:{index}",
                        "yuv420p",
                        f"-b:v:{index}",
                        str(rendition.bitrate),
                        f"-maxrate:v:{index}",
                        str(int(rendition.bitrate * 1.15)),
                        f"-bufsize:v:{index}",
                        str(rendition.bitrate * 2),
                        f"-g:v:{index}",
                        "96",
                        f"-keyint_min:v:{index}",
                        "96",
                        f"-force_key_frames:v:{index}",
                        "expr:gte(t,n_forced*4)",
                    ]
                )
                if has_audio:
                    command.extend(
                        [
                            "-map",
                            "0:a:0?",
                            f"-c:a:{index}",
                            "aac",
                            f"-ac:a:{index}",
                            "2",
                            f"-ar:a:{index}",
                            "48000",
                            f"-b:a:{index}",
                            str(rendition.audio_bitrate),
                        ]
                    )
                    stream_map.append(f"v:{index},a:{index},name:{rendition.name}")
                else:
                    stream_map.append(f"v:{index},name:{rendition.name}")
        command.extend(
            [
                "-f",
                "hls",
                "-hls_time",
                "4",
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "event",
                "-hls_segment_type",
                "mpegts",
                "-hls_flags",
                "independent_segments+temp_file",
                "-master_pl_name",
                "master.m3u8",
                "-var_stream_map",
                " ".join(stream_map),
                "-hls_segment_filename",
                str(output_root / "%v" / "seg_%06d.ts"),
                str(output_root / "%v" / "index.m3u8"),
            ]
        )
        return command

    @staticmethod
    def ready(output_root: Path, plan: MediaPlan) -> bool:
        if not (output_root / "master.m3u8").is_file():
            return False
        return all(
            (output_root / item.name / "index.m3u8").is_file()
            and next((output_root / item.name).glob("seg_*.ts"), None) is not None
            for item in plan.renditions
        )


def safe_storage_key(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise UnsafeMediaError("chave de cache invalida")
    return value
