from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath


MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
ALLOWED_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})
TIMESTAMP_LINE = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3})(?P<settings>.*)$"
)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def track_id(site: str, infohash: str, torrent_path: str, language: str) -> str:
    identity = "\0".join((site, infohash, torrent_path, language))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _portable_parts(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("caminho de legenda invalido")
    return path.parts


def resolve_subtitle_path(
    stored_path: str,
    *,
    mounted_root: Path,
    host_root: str,
) -> Path:
    """Traduz um caminho persistido no host para a montagem read-only.

    O banco legado guarda caminhos Windows absolutos. A comparacao e feita por
    componentes, sem prefixos textuais, para impedir escapes por nomes parecidos.
    """

    stored_parts = _portable_parts(stored_path)
    host_parts = _portable_parts(host_root)
    folded = tuple(part.casefold() for part in stored_parts)
    host_folded = tuple(part.casefold() for part in host_parts)
    start = next(
        (
            index
            for index in range(len(stored_parts) - len(host_parts) + 1)
            if folded[index : index + len(host_parts)] == host_folded
        ),
        None,
    )
    if start is None:
        raise ValueError("legenda fora do cofre configurado")
    relative = stored_parts[start + len(host_parts) :]
    if not relative:
        raise ValueError("arquivo de legenda ausente")
    root = mounted_root.resolve(strict=True)
    unresolved = root.joinpath(*relative)
    current = root
    for component in relative:
        current = current / component
        if _is_link(current):
            raise ValueError("links nao sao aceitos no cofre de legendas")
    candidate = unresolved.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("legenda fora do cofre configurado") from exc
    if not candidate.is_file():
        raise ValueError("legenda indisponivel")
    if candidate.suffix.casefold() not in ALLOWED_SUBTITLE_EXTENSIONS:
        raise ValueError("formato de legenda nao aprovado")
    if not 0 < candidate.stat().st_size <= MAX_SUBTITLE_BYTES:
        raise ValueError("tamanho de legenda invalido")
    return candidate


def decode_subtitle(payload: bytes) -> str:
    if not payload or len(payload) > MAX_SUBTITLE_BYTES or b"\x00" in payload:
        raise ValueError("conteudo de legenda invalido")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("codificacao de legenda invalida")


def to_webvtt(payload: bytes, extension: str) -> str:
    text = decode_subtitle(payload).replace("\r\n", "\n").replace("\r", "\n")
    if extension.casefold() == ".vtt":
        if not text.lstrip("\ufeff\n ").startswith("WEBVTT"):
            raise ValueError("cabecalho WebVTT ausente")
        return text
    if extension.casefold() != ".srt":
        raise ValueError("formato de legenda nao aprovado")

    converted: list[str] = ["WEBVTT", ""]
    found_cue = False
    for line in text.split("\n"):
        match = TIMESTAMP_LINE.match(line.strip())
        if match:
            found_cue = True
            converted.append(
                f"{match['start'].replace(',', '.')} --> "
                f"{match['end'].replace(',', '.')}{match['settings']}"
            )
        else:
            converted.append(line)
    if not found_cue:
        raise ValueError("nenhuma marcacao SubRip encontrada")
    return "\n".join(converted).rstrip() + "\n"
