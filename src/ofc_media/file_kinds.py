from __future__ import annotations

import hashlib
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal


FileKind = Literal[
    "video",
    "audio",
    "subtitle",
    "image",
    "document",
    "archive",
    "software",
    "dataset",
    "other",
]
PresenceConfidence = Literal["exact", "possible", "none"]

FILE_KINDS: tuple[FileKind, ...] = (
    "video",
    "audio",
    "subtitle",
    "image",
    "document",
    "archive",
    "software",
    "dataset",
    "other",
)

_EXTENSIONS: dict[FileKind, frozenset[str]] = {
    "video": frozenset(
        {
            ".3gp",
            ".asf",
            ".avi",
            ".divx",
            ".flv",
            ".m2ts",
            ".m4v",
            ".mkv",
            ".mov",
            ".mp4",
            ".mpeg",
            ".mpg",
            ".mts",
            ".ogv",
            ".rm",
            ".rmvb",
            ".ts",
            ".vob",
            ".webm",
            ".wmv",
        }
    ),
    "audio": frozenset(
        {
            ".aac",
            ".ac3",
            ".aiff",
            ".alac",
            ".ape",
            ".dts",
            ".flac",
            ".m4a",
            ".mka",
            ".mp3",
            ".oga",
            ".ogg",
            ".opus",
            ".wav",
            ".wma",
        }
    ),
    "subtitle": frozenset(
        {
            ".ass",
            ".dfxp",
            ".idx",
            ".lrc",
            ".smi",
            ".srt",
            ".ssa",
            ".sub",
            ".sup",
            ".ttml",
            ".vtt",
        }
    ),
    "image": frozenset(
        {
            ".avif",
            ".bmp",
            ".gif",
            ".heic",
            ".heif",
            ".ico",
            ".jpeg",
            ".jpg",
            ".jxl",
            ".png",
            ".svg",
            ".tif",
            ".tiff",
            ".webp",
        }
    ),
    "document": frozenset(
        {
            ".azw",
            ".azw3",
            ".doc",
            ".docx",
            ".epub",
            ".html",
            ".htm",
            ".md",
            ".mobi",
            ".odp",
            ".ods",
            ".odt",
            ".pdf",
            ".ppt",
            ".pptx",
            ".rtf",
            ".tex",
            ".txt",
            ".xls",
            ".xlsx",
        }
    ),
    "archive": frozenset(
        {
            ".7z",
            ".bz2",
            ".cab",
            ".gz",
            ".img",
            ".iso",
            ".rar",
            ".tar",
            ".tar.bz2",
            ".tar.gz",
            ".tar.xz",
            ".tar.zst",
            ".tbz2",
            ".tgz",
            ".txz",
            ".xz",
            ".zip",
            ".zst",
        }
    ),
    "software": frozenset(
        {
            ".apk",
            ".appimage",
            ".bat",
            ".bin",
            ".cmd",
            ".deb",
            ".dll",
            ".dmg",
            ".dylib",
            ".exe",
            ".ipa",
            ".jar",
            ".msi",
            ".msix",
            ".pkg",
            ".ps1",
            ".rpm",
            ".sh",
            ".so",
            ".whl",
        }
    ),
    "dataset": frozenset(
        {
            ".avro",
            ".csv",
            ".db",
            ".feather",
            ".geojson",
            ".h5",
            ".hdf5",
            ".json",
            ".jsonl",
            ".ndjson",
            ".npy",
            ".npz",
            ".orc",
            ".parquet",
            ".pkl",
            ".sql",
            ".sqlite",
            ".sqlite3",
            ".tsv",
            ".xml",
            ".yaml",
            ".yml",
        }
    ),
    "other": frozenset(),
}

_KIND_BY_EXTENSION = {
    extension: kind for kind, extensions in _EXTENSIONS.items() for extension in extensions
}
_COMPOUND_EXTENSIONS = tuple(
    sorted((value for value in _KIND_BY_EXTENSION if value.count(".") > 1), key=len, reverse=True)
)
_MIME_OVERRIDES = {
    ".ass": "text/x-ssa",
    ".m2ts": "video/mp2t",
    ".mka": "audio/x-matroska",
    ".mkv": "video/x-matroska",
    ".srt": "application/x-subrip",
    ".ssa": "text/x-ssa",
    ".sub": "text/x-subviewer",
    ".sup": "application/pgs",
    ".ts": "video/mp2t",
    ".vtt": "text/vtt",
}
_KIND_BY_MIME_PREFIX: tuple[tuple[str, FileKind], ...] = (
    ("video/", "video"),
    ("audio/", "audio"),
    ("image/", "image"),
    ("text/", "document"),
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:($|/)")
_WINDOWS_RESERVED_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)
_FORBIDDEN_COMPONENT_RE = re.compile(r"[<>:\"|?*\x00-\x1f]")
_NORMALIZED_NAME_RE = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class FileClassification:
    extension: str
    file_kind: FileKind
    mime_type: str
    is_subtitle: bool


def normalized_extension(value: str) -> str:
    """Return a lower-case extension, preserving known compound archives."""

    leaf = unicodedata.normalize("NFKC", str(value)).replace("\\", "/").rsplit("/", 1)[-1]
    lowered = leaf.casefold().strip()
    if not lowered:
        return ""
    if "/" not in lowered and lowered.startswith(".") and lowered.count(".") == 1:
        return lowered
    if "." not in lowered and re.fullmatch(r"[a-z0-9]{1,12}", lowered):
        return f".{lowered}"
    for extension in _COMPOUND_EXTENSIONS:
        if lowered.endswith(extension):
            return extension
    return PurePosixPath(lowered).suffix


def classify_extension(value: str) -> FileKind:
    return _KIND_BY_EXTENSION.get(normalized_extension(value), "other")


def classify_file(path: str, mime_type: str | None = None) -> FileClassification:
    extension = normalized_extension(path)
    kind = _KIND_BY_EXTENSION.get(extension, "other")
    supplied_mime = (mime_type or "").strip().casefold()
    if kind == "other" and supplied_mime and supplied_mime != "application/octet-stream":
        if "subtitle" in supplied_mime or supplied_mime in {
            "application/pgs",
            "application/x-subrip",
        }:
            kind = "subtitle"
        else:
            for prefix, inferred_kind in _KIND_BY_MIME_PREFIX:
                if supplied_mime.startswith(prefix):
                    kind = inferred_kind
                    break
    guessed_mime = _MIME_OVERRIDES.get(extension)
    if guessed_mime is None:
        guessed_mime = mimetypes.guess_type(f"asset{extension}", strict=False)[0]
    selected_mime = (
        supplied_mime
        if supplied_mime and supplied_mime != "application/octet-stream"
        else guessed_mime or supplied_mime or "application/octet-stream"
    )
    return FileClassification(
        extension=extension,
        file_kind=kind,
        mime_type=selected_mime,
        is_subtitle=kind == "subtitle",
    )


def normalize_name(value: str, *, strip_extension: bool = False) -> str:
    """Normalize a file name for conservative, case-insensitive comparisons."""

    leaf = unicodedata.normalize("NFKC", str(value)).replace("\\", "/").rsplit("/", 1)[-1]
    if strip_extension:
        extension = normalized_extension(leaf)
        if extension:
            leaf = leaf[: -len(extension)]
    decomposed = unicodedata.normalize("NFKD", leaf.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_controls = "".join(char for char in without_marks if unicodedata.category(char) != "Cc")
    return " ".join(part for part in _NORMALIZED_NAME_RE.split(without_controls) if part)


def normalize_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("sha256 deve conter exatamente 64 caracteres hexadecimais")
    return normalized


def match_presence(
    *,
    left_name: str,
    left_size: int,
    right_name: str,
    right_size: int,
    left_sha256: str | None = None,
    right_sha256: str | None = None,
) -> PresenceConfidence:
    """Compare two assets without promoting a name-only match to exact.

    Equal SHA-256 plus size is exact. When either digest is unavailable, equal
    normalized names plus size is deliberately labelled ``possible``.
    """

    if left_size < 0 or right_size < 0:
        raise ValueError("tamanho de arquivo nao pode ser negativo")
    if left_size != right_size:
        return "none"
    left_digest = normalize_sha256(left_sha256)
    right_digest = normalize_sha256(right_sha256)
    if left_digest is not None and right_digest is not None:
        return "exact" if left_digest == right_digest else "none"
    left_normalized = normalize_name(left_name)
    right_normalized = normalize_name(right_name)
    if left_normalized and left_normalized == right_normalized:
        return "possible"
    return "none"


def _safe_component(value: str, *, max_length: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(char for char in normalized if not unicodedata.category(char).startswith("C"))
    normalized = _FORBIDDEN_COMPONENT_RE.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized:
        raise ValueError("componente de destino vazio")
    if normalized in {".", ".."}:
        raise ValueError("navegacao relativa nao e permitida no destino")
    if _WINDOWS_RESERVED_RE.fullmatch(normalized):
        normalized = f"_{normalized}"
    if len(normalized) > max_length:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
        normalized = f"{normalized[: max_length - 11].rstrip()}-{digest}"
    return normalized


def safe_destination_path(*parts: str, max_component_length: int = 150) -> str:
    """Build a portable relative POSIX path and reject traversal/absolute input."""

    if not parts:
        raise ValueError("destino deve possuir ao menos um componente")
    if not 32 <= max_component_length <= 255:
        raise ValueError("max_component_length deve estar entre 32 e 255")
    components: list[str] = []
    for raw_part in parts:
        part = unicodedata.normalize("NFKC", str(raw_part)).replace("\\", "/")
        if "\x00" in part:
            raise ValueError("byte NUL nao e permitido no destino")
        if part.startswith("/") or part.startswith("//") or _WINDOWS_DRIVE_RE.match(part):
            raise ValueError("destino deve ser relativo")
        for component in part.split("/"):
            if not component or component == ".":
                continue
            if component == "..":
                raise ValueError("navegacao relativa nao e permitida no destino")
            components.append(_safe_component(component, max_length=max_component_length))
    if not components:
        raise ValueError("destino vazio")
    result = "/".join(components)
    if len(result.encode("utf-8")) > 1024:
        raise ValueError("destino excede 1024 bytes")
    return result


def classified_destination_path(file_kind: FileKind, title: str, relative_path: str) -> str:
    if file_kind not in FILE_KINDS:
        raise ValueError(f"tipo de arquivo invalido: {file_kind}")
    return safe_destination_path(file_kind, title, relative_path)
