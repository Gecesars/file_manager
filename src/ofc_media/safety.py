from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any


VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg", ".ogv"}
)
FORBIDDEN_SUFFIXES = frozenset(
    {".exe", ".msi", ".com", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr", ".dll", ".jar", ".lnk", ".html", ".htm"}
)
INFOHASH_RE = re.compile(r"^[0-9a-f]{40}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


class UnsafeMediaError(ValueError):
    pass


def normalized_infohash(value: str) -> str:
    result = value.casefold().strip()
    if not INFOHASH_RE.fullmatch(result):
        raise UnsafeMediaError("infohash invalido")
    return result


def normalized_session_id(value: str) -> str:
    result = value.casefold().strip()
    if not SESSION_RE.fullmatch(result):
        raise UnsafeMediaError("sessao invalida")
    return result


def safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or "\x00" in normalized
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise UnsafeMediaError("caminho relativo invalido")
    return path.as_posix()


def is_video_name(value: str) -> bool:
    try:
        name = PurePosixPath(safe_relative_path(value)).name
    except UnsafeMediaError:
        return False
    suffixes = [item.casefold() for item in Path(name).suffixes]
    return bool(suffixes) and suffixes[-1] in VIDEO_EXTENSIONS and not any(
        item in FORBIDDEN_SUFFIXES for item in suffixes
    )


def safe_owned_path(root: Path, *parts: str) -> Path:
    base = root.resolve()
    candidate = base.joinpath(*parts).resolve()
    if candidate != base and base not in candidate.parents:
        raise UnsafeMediaError("caminho fora da area autorizada")
    return candidate


def has_video_signature(prefix: bytes) -> bool:
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return True
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return True
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"AVI ":
        return True
    if prefix.startswith((b"OggS", b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return True
    if len(prefix) >= 377 and prefix[0] == prefix[188] == prefix[376] == 0x47:
        return True
    return False


class BencodeReader:
    def __init__(self, payload: bytes, max_depth: int = 32) -> None:
        self.payload = payload
        self.index = 0
        self.max_depth = max_depth

    def parse(self, depth: int = 0) -> Any:
        if depth > self.max_depth or self.index >= len(self.payload):
            raise UnsafeMediaError("bencode invalido")
        marker = self.payload[self.index : self.index + 1]
        if marker == b"i":
            return self._integer()
        if marker == b"l":
            self.index += 1
            values = []
            while self._peek() != b"e":
                values.append(self.parse(depth + 1))
            self.index += 1
            return values
        if marker == b"d":
            self.index += 1
            values: dict[bytes, Any] = {}
            previous: bytes | None = None
            while self._peek() != b"e":
                key = self._bytes()
                if previous is not None and key <= previous:
                    raise UnsafeMediaError("dicionario bencode nao canonico")
                previous = key
                values[key] = self.parse(depth + 1)
            self.index += 1
            return values
        if marker.isdigit():
            return self._bytes()
        raise UnsafeMediaError("marcador bencode invalido")

    def _peek(self) -> bytes:
        if self.index >= len(self.payload):
            raise UnsafeMediaError("bencode truncado")
        return self.payload[self.index : self.index + 1]

    def _integer(self) -> int:
        end = self.payload.find(b"e", self.index + 1)
        if end < 0:
            raise UnsafeMediaError("inteiro bencode truncado")
        raw = self.payload[self.index + 1 : end]
        if not raw or raw == b"-0" or (raw.startswith(b"0") and len(raw) > 1):
            raise UnsafeMediaError("inteiro bencode invalido")
        try:
            value = int(raw)
        except ValueError as exc:
            raise UnsafeMediaError("inteiro bencode invalido") from exc
        self.index = end + 1
        return value

    def _bytes(self) -> bytes:
        colon = self.payload.find(b":", self.index)
        if colon < 0:
            raise UnsafeMediaError("bytes bencode truncados")
        raw_length = self.payload[self.index:colon]
        if not raw_length or (raw_length.startswith(b"0") and len(raw_length) > 1):
            raise UnsafeMediaError("tamanho bencode invalido")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise UnsafeMediaError("tamanho bencode invalido") from exc
        if length < 0 or length > 16 * 1024 * 1024:
            raise UnsafeMediaError("campo bencode acima do limite")
        start = colon + 1
        end = start + length
        if end > len(self.payload):
            raise UnsafeMediaError("bytes bencode truncados")
        self.index = end
        return self.payload[start:end]


def encode_bencode(value: Any) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            encode_bencode(key) + encode_bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise UnsafeMediaError("tipo bencode nao suportado")


def decode_metainfo(payload: bytes, advertised_infohash: str) -> dict[bytes, Any]:
    if not payload or len(payload) > 5 * 1024**2:
        raise UnsafeMediaError("metainfo fora do limite")
    reader = BencodeReader(payload)
    root = reader.parse()
    if reader.index != len(payload) or not isinstance(root, dict):
        raise UnsafeMediaError("metainfo invalido")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise UnsafeMediaError("dicionario info ausente")
    if hashlib.sha1(encode_bencode(info)).hexdigest() != normalized_infohash(advertised_infohash):
        raise UnsafeMediaError("infohash local divergente")
    return root


def metainfo_files(root: dict[bytes, Any]) -> list[tuple[int, str, int]]:
    info = root[b"info"]
    result: list[tuple[int, str, int]] = []
    if b"files" in info:
        for index, item in enumerate(info[b"files"]):
            if not isinstance(item, dict) or not isinstance(item.get(b"path"), list):
                raise UnsafeMediaError("arquivo invalido no metainfo")
            try:
                path = "/".join(part.decode("utf-8", "strict") for part in item[b"path"])
                length = int(item[b"length"])
            except (KeyError, UnicodeDecodeError, TypeError, ValueError) as exc:
                raise UnsafeMediaError("arquivo invalido no metainfo") from exc
            result.append((index, safe_relative_path(path), length))
    else:
        try:
            name = info[b"name"].decode("utf-8", "strict")
            length = int(info[b"length"])
        except (KeyError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise UnsafeMediaError("arquivo unico invalido") from exc
        result.append((0, safe_relative_path(name), length))
    return result
