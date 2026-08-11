from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import stat
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Protocol
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from psycopg.types.json import Jsonb

from .auth import internal_token_matches, token_matches
from .config import Settings
from .db import connection
from .file_kinds import classify_file as classify_catalog_file, normalize_sha256
from .heartbeat import beat, start_heartbeat
from .safety import UnsafeMediaError, is_video_name, normalized_session_id, safe_relative_path


LOG = logging.getLogger("ofc.drive")
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
PARENT_BATCH = 20
SECOND_LEVEL_CATEGORIES = frozenset({"filmes", "treinamentos"})
UPLOAD_GRANULARITY = 256 * 1024
DEFAULT_UPLOAD_CHUNK = 16 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024
TRANSFER_RESUME_AFTER_SECONDS = 300
GOOGLE_MIME_PREFIX = "application/vnd.google-apps."
WINDOWS_RESERVED_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)
FORBIDDEN_COMPONENT_RE = re.compile(r'[<>"|?*\x00-\x1f]')
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:($|/)")
DRIVE_COMPONENT_BYTES = 180


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...

    def close(self) -> None: ...

    def iter_content(self, chunk_size: int) -> Iterable[bytes]: ...


class HttpSessionLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> ResponseLike: ...


class CredentialsLike(Protocol):
    valid: bool
    refresh_token: str | None
    token: str | None

    def refresh(self, request: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class FileClassification:
    kind: str
    mime_type: str
    is_video: bool
    is_subtitle: bool
    extension: str


@dataclass(frozen=True, slots=True)
class Checksums:
    md5: str
    sha256: str


class InvalidDriveCursor(RuntimeError):
    """O Drive rejeitou um pageToken; o snapshot local deve ser refeito."""


@dataclass(frozen=True, slots=True)
class DriveChangeFeed:
    changes: tuple[dict[str, Any], ...]
    new_start_page_token: str
    has_removals: bool


@dataclass(frozen=True, slots=True)
class DriveScope:
    drive_id: str | None
    corpus: str

    @property
    def cursor_drive_id(self) -> str:
        return self.drive_id or "user"


def file_checksums(path: Path, *, chunk_size: int = DOWNLOAD_CHUNK) -> Checksums:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            md5.update(chunk)
            sha256.update(chunk)
    return Checksums(md5.hexdigest(), sha256.hexdigest())


def _header(response: ResponseLike, name: str) -> str | None:
    expected = name.casefold()
    for key, value in response.headers.items():
        if key.casefold() == expected:
            return str(value)
    return None


def _next_upload_offset(response: ResponseLike) -> int:
    received = _header(response, "Range")
    if not received:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", received.strip())
    if not match:
        raise RuntimeError("Google Drive retornou Range de upload invalido")
    return int(match.group(1)) + 1


def verify_uploaded_metadata(
    metadata: Mapping[str, Any], *, size: int, checksums: Checksums
) -> None:
    if int(metadata.get("size") or -1) != size:
        raise RuntimeError("tamanho do arquivo diverge apos upload no Google Drive")
    remote_sha256 = str(metadata.get("sha256Checksum") or "").casefold()
    remote_md5 = str(metadata.get("md5Checksum") or "").casefold()
    if remote_sha256:
        if remote_sha256 != checksums.sha256:
            raise RuntimeError("SHA-256 diverge apos upload no Google Drive")
        return
    if remote_md5:
        if remote_md5 != checksums.md5:
            raise RuntimeError("MD5 diverge apos upload no Google Drive")
        return
    raise RuntimeError("Google Drive nao retornou checksum para verificar o upload")


def classify_file(name: str, mime_type: str | None) -> FileClassification:
    """Classifica um blob sem transformar a classificacao em autorizacao de playback."""
    canonical = classify_catalog_file(name, mime_type)
    selected_mime = canonical.mime_type
    is_video = is_video_name(name) and (
        selected_mime.startswith("video/")
        or selected_mime in {"application/octet-stream", "application/x-matroska"}
    )
    return FileClassification(
        kind=canonical.file_kind,
        mime_type=selected_mime,
        is_video=is_video,
        is_subtitle=canonical.is_subtitle,
        extension=canonical.extension,
    )


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _bounded_app_properties(values: Mapping[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        selected_key = str(key)
        selected_value = str(value)
        if not selected_key or len(result) >= 30:
            continue
        encoded_key = selected_key.encode("utf-8")
        remaining = 124 - len(encoded_key)
        if remaining <= 0:
            continue
        while len(selected_value.encode("utf-8")) > remaining:
            selected_value = selected_value[:-1]
        result[selected_key] = selected_value
    return result


def _trim_utf8(value: str, limit: int) -> str:
    selected = value
    while selected and len(selected.encode("utf-8")) > limit:
        selected = selected[:-1]
    return selected.rstrip(" .")


def _identity_suffix(component: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    extension = Path(component).suffix
    if len(extension.encode("utf-8")) > 24:
        extension = ""
    stem = component[: -len(extension)] if extension else component
    suffix = f"--{digest}{extension}"
    room = DRIVE_COMPONENT_BYTES - len(suffix.encode("utf-8"))
    selected = _trim_utf8(stem, max(1, room)) or "arquivo"
    return f"{selected}{suffix}"


def safe_drive_component(value: str) -> str:
    """Converte um nome remoto em componente apenas para catalogacao local."""
    original = unicodedata.normalize("NFKC", str(value))
    translated = original.translate(
        str.maketrans({"/": "_", "\\": "_", ":": " -", "\x00": "_"})
    )
    translated = FORBIDDEN_COMPONENT_RE.sub("_", translated)
    translated = "".join(
        char for char in translated if not unicodedata.category(char).startswith("C")
    ).strip(" .")
    if translated in {"", ".", ".."}:
        translated = "sem-nome"
    if WINDOWS_RESERVED_RE.fullmatch(translated):
        translated = f"_{translated}"
    if len(translated.encode("utf-8")) > DRIVE_COMPONENT_BYTES:
        translated = _identity_suffix(translated, original)
    return translated


def unique_drive_components(
    entries: Iterable[tuple[Any, str, str]],
) -> dict[Any, str]:
    """Normaliza nomes e torna colisoes portaveis independentes da ordem."""
    prepared = [
        (key, safe_drive_component(raw), identity) for key, raw, identity in entries
    ]
    collisions: dict[str, int] = {}
    for _, component, _ in prepared:
        folded = unicodedata.normalize("NFKC", component).casefold()
        collisions[folded] = collisions.get(folded, 0) + 1
    return {
        key: (
            _identity_suffix(component, identity)
            if collisions[unicodedata.normalize("NFKC", component).casefold()] > 1
            else component
        )
        for key, component, identity in prepared
    }


def drive_group(
    folder_parts: tuple[str, ...],
    folder_ids: tuple[str, ...],
    file_id: str,
    file_name: str,
) -> tuple[str, str, str, str]:
    """Retorna identidade, titulo, categoria e hash sintetico do card."""
    depth = 0
    if folder_parts:
        depth = 2 if folder_parts[0].casefold() in SECOND_LEVEL_CATEGORIES else 1
        depth = min(depth, len(folder_parts))
    category = "/".join(folder_parts[:depth]) or "Google Drive"
    if len(folder_parts) > depth:
        group_id = folder_ids[depth]
        identity = f"folder:{group_id}"
        title = folder_parts[depth]
    else:
        identity = f"file:{file_id}"
        title = Path(file_name).stem or file_name
    infohash = hashlib.sha1(f"gdrive:{identity}".encode(), usedforsecurity=False).hexdigest()
    return identity, title, category, infohash


def parse_range(value: str | None, total: int) -> tuple[int, int, bool]:
    if not value:
        return 0, total - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("range invalido")
    first, separator, last = value[6:].partition("-")
    if not separator:
        raise ValueError("range invalido")
    if first:
        start = int(first)
        end = int(last) if last else total - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("range invalido")
        start = max(0, total - suffix)
        end = total - 1
    if start < 0 or end < start or end >= total:
        raise ValueError("range invalido")
    return start, end, True


class DriveClient:
    def __init__(
        self,
        token_path: Path | None = None,
        *,
        credentials: CredentialsLike | None = None,
        session: HttpSessionLike | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if credentials is None:
            if token_path is None or not token_path.is_file():
                raise RuntimeError(f"token Google Drive ausente: {token_path}")
            credentials = Credentials.from_authorized_user_file(token_path)
        self.credentials = credentials
        self.session: HttpSessionLike = session or requests.Session()
        self.sleeper = sleeper
        self.lock = threading.RLock()
        self._refresh_if_needed()

    def _refresh_if_needed(self, *, force: bool = False) -> str:
        with self.lock:
            if force or not self.credentials.valid:
                if not self.credentials.refresh_token:
                    raise RuntimeError("token Google Drive sem refresh token")
                self.credentials.refresh(GoogleAuthRequest())
            if not self.credentials.token:
                raise RuntimeError("Google Drive nao forneceu access token")
            return self.credentials.token

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        base_url: str = DRIVE_API,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        data: bytes | BinaryIO | None = None,
        stream: bool = False,
        attempts: int = 5,
        accepted: frozenset[int] = frozenset(),
    ) -> ResponseLike:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{base_url}/{path_or_url.lstrip('/')}"
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            token = self._refresh_if_needed(force=False)
            supplied = {
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "identity",
                **(headers or {}),
            }
            try:
                kwargs: dict[str, Any] = {
                    "params": params,
                    "headers": supplied,
                    "stream": stream,
                    "timeout": (15, 180),
                }
                if json_body is not None:
                    kwargs["json"] = dict(json_body)
                if data is not None:
                    kwargs["data"] = data
                response = self.session.request(
                    method.upper(),
                    url,
                    **kwargs,
                )
                if response.status_code == 401 and attempt == 0:
                    response.close()
                    self._refresh_if_needed(force=True)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    response.close()
                    if attempt + 1 < attempts:
                        self.sleeper(min(16, 2**attempt))
                        continue
                if response.status_code not in accepted:
                    response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.sleeper(min(16, 2**attempt))
        raise RuntimeError(f"falha {method.upper()} no Google Drive: {last_error}")

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        attempts: int = 5,
        accepted: frozenset[int] = frozenset(),
    ) -> ResponseLike:
        return self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            stream=stream,
            attempts=attempts,
            accepted=accepted,
        )

    def _post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        upload: bool = False,
        attempts: int = 5,
        accepted: frozenset[int] = frozenset(),
    ) -> ResponseLike:
        return self._request(
            "POST",
            path,
            base_url=DRIVE_UPLOAD_API if upload else DRIVE_API,
            params=params,
            headers=headers,
            json_body=json_body,
            attempts=attempts,
            accepted=accepted,
        )

    def _patch(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        attempts: int = 5,
    ) -> ResponseLike:
        return self._request(
            "PATCH", path, params=params, json_body=json_body, attempts=attempts
        )

    @staticmethod
    def _payload(response: ResponseLike) -> dict[str, Any]:
        try:
            payload = response.json()
            return dict(payload or {})
        finally:
            response.close()

    def list_files(
        self,
        *,
        query: str,
        fields: str = "id,name,mimeType,parents,appProperties",
        drive_id: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({fields})",
                "pageSize": 1000,
                "pageToken": page_token,
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if drive_id:
                params.update({"corpora": "drive", "driveId": drive_id})
            response = self._get(
                "files",
                params=params,
            )
            payload = self._payload(response)
            result.extend(dict(item) for item in payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return result

    def discover_root_scope(self, root_folder_id: str) -> DriveScope:
        if not DRIVE_ID_RE.fullmatch(root_folder_id):
            raise UnsafeMediaError("pasta raiz Google Drive invalida")
        payload = self._payload(
            self._get(
                f"files/{root_folder_id}",
                params={
                    "fields": "id,driveId,mimeType",
                    "supportsAllDrives": "true",
                },
            )
        )
        if str(payload.get("id") or "") != root_folder_id:
            raise RuntimeError("Google Drive retornou metadados de outra pasta raiz")
        drive_id = str(payload.get("driveId") or "").strip() or None
        if drive_id and not DRIVE_ID_RE.fullmatch(drive_id):
            raise RuntimeError("Google Drive retornou driveId invalido")
        return DriveScope(drive_id=drive_id, corpus="drive" if drive_id else "user")

    @staticmethod
    def _page_token(value: Any, *, source: str) -> str:
        token = str(value or "").strip()
        if not token or len(token) > 8192 or any(ord(char) < 32 for char in token):
            if source == "cursor":
                raise InvalidDriveCursor("pageToken Google Drive local invalido")
            raise RuntimeError("Google Drive nao retornou startPageToken valido")
        return token

    def get_start_page_token(self, *, drive_id: str | None = None) -> str:
        params = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        payload = self._payload(self._get("changes/startPageToken", params=params))
        return self._page_token(payload.get("startPageToken"), source="server")

    def list_changes(
        self, page_token: str, *, drive_id: str | None = None
    ) -> DriveChangeFeed:
        """Consome todo o change log; ele e apenas sinal para reconciliacao."""
        current = self._page_token(page_token, source="cursor")
        seen_tokens: set[str] = set()
        changes: list[dict[str, Any]] = []
        while True:
            if current in seen_tokens:
                raise RuntimeError("Google Drive repetiu nextPageToken de changes")
            seen_tokens.add(current)
            params = {
                "pageToken": current,
                "pageSize": 1000,
                "spaces": "drive",
                "includeRemoved": "true",
                "includeCorpusRemovals": "true",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,changeType,driveId,time,file(id,trashed,parents))"
                ),
            }
            if drive_id:
                params["driveId"] = drive_id
            response = self._get(
                "changes",
                params=params,
                attempts=3,
                accepted=frozenset({400, 410}),
            )
            if response.status_code in {400, 410}:
                response.close()
                raise InvalidDriveCursor("pageToken Google Drive expirado ou rejeitado")
            payload = self._payload(response)
            raw_changes = payload.get("changes", [])
            if not isinstance(raw_changes, list) or any(
                not isinstance(item, Mapping) for item in raw_changes
            ):
                raise RuntimeError("Google Drive retornou lista de changes invalida")
            changes.extend(dict(item) for item in raw_changes)
            next_token = payload.get("nextPageToken")
            if next_token:
                current = self._page_token(next_token, source="server")
                continue
            new_token = self._page_token(
                payload.get("newStartPageToken"), source="server"
            )
            return DriveChangeFeed(
                changes=tuple(changes),
                new_start_page_token=new_token,
                has_removals=any(
                    bool(item.get("removed")) or (
                        item.get("changeType") == "file" and not item.get("file")
                    )
                    for item in changes
                ),
            )

    def list_children(
        self, parent_ids: list[str], *, drive_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not parent_ids or any(not DRIVE_ID_RE.fullmatch(item) for item in parent_ids):
            raise UnsafeMediaError("pasta Google Drive invalida")
        clauses = " or ".join(f"'{item}' in parents" for item in parent_ids)
        query = f"({clauses}) and trashed=false"
        return self.list_files(
            query=query,
            drive_id=drive_id,
            fields=(
                "id,name,mimeType,size,parents,modifiedTime,version,md5Checksum,"
                "sha1Checksum,sha256Checksum,appProperties,"
                "capabilities(canDownload),videoMediaMetadata"
            ),
        )

    def generate_file_id(self) -> str:
        payload = self._payload(
            self._get("files/generateIds", params={"count": 1, "space": "drive"})
        )
        file_ids = payload.get("ids") or []
        if not file_ids or not DRIVE_ID_RE.fullmatch(str(file_ids[0])):
            raise RuntimeError("Google Drive nao retornou um fileId valido")
        return str(file_ids[0])

    def get_file_metadata(self, file_id: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        if not DRIVE_ID_RE.fullmatch(file_id):
            raise UnsafeMediaError("arquivo Google Drive invalido")
        response = self._get(
            f"files/{file_id}",
            params={
                "fields": (
                    "id,name,mimeType,size,parents,md5Checksum,sha1Checksum,"
                    "sha256Checksum,version,driveId,appProperties,capabilities(canDownload)"
                ),
                "supportsAllDrives": "true",
            },
            attempts=3,
            accepted=frozenset({404}) if missing_ok else frozenset(),
        )
        if response.status_code == 404:
            response.close()
            return None
        return self._payload(response)

    def patch_file(self, file_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        if not DRIVE_ID_RE.fullmatch(file_id):
            raise UnsafeMediaError("arquivo Google Drive invalido")
        return self._payload(
            self._patch(
                f"files/{file_id}",
                params={"supportsAllDrives": "true", "fields": "id,name,parents,appProperties"},
                json_body=metadata,
            )
        )

    def _find_managed_folder(self, parent_id: str, folder_key: str) -> dict[str, Any] | None:
        query = (
            f"'{parent_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false "
            f"and appProperties has {{ key='ofc_folder_key' and value='{folder_key}' }}"
        )
        folders = self.list_files(query=query)
        return min(folders, key=lambda item: str(item.get("id"))) if folders else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        if not DRIVE_ID_RE.fullmatch(parent_id):
            raise UnsafeMediaError("pasta Google Drive invalida")
        original = unicodedata.normalize("NFKC", str(name))
        original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        component = safe_drive_component(original)
        folder_key = hashlib.sha1(
            f"{parent_id}\0{original_hash}".encode(), usedforsecurity=False
        ).hexdigest()
        found = self._find_managed_folder(parent_id, folder_key)
        if found:
            return str(found["id"])
        escaped = _drive_query_literal(component)
        same_name = self.list_files(
            query=(
                f"'{parent_id}' in parents and mimeType='{FOLDER_MIME}' "
                f"and name='{escaped}' and trashed=false"
            )
        )
        compatible = [
            item
            for item in same_name
            if str((item.get("appProperties") or {}).get("ofc_original_hash") or "")
            in {"", original_hash}
        ]
        if compatible:
            folder_id = str(min(compatible, key=lambda item: str(item.get("id")))["id"])
            self.patch_file(
                folder_id,
                {
                    "appProperties": {
                        "ofc_folder_key": folder_key,
                        "ofc_original_hash": original_hash,
                        "ofc_managed": "1",
                    }
                },
            )
            return folder_id
        if same_name:
            component = _identity_suffix(component, original_hash)
        folder_id = self.generate_file_id()
        body = {
            "id": folder_id,
            "name": component,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
            "appProperties": {
                "ofc_folder_key": folder_key,
                "ofc_original_hash": original_hash,
                "ofc_managed": "1",
            },
        }
        try:
            response = self._post(
                "files",
                params={"supportsAllDrives": "true", "fields": "id"},
                json_body=body,
                attempts=2,
                accepted=frozenset({409}),
            )
            if response.status_code != 409:
                created = self._payload(response)
                return str(created.get("id") or folder_id)
            response.close()
        except RuntimeError:
            found = self._find_managed_folder(parent_id, folder_key)
            if found:
                return str(found["id"])
            raise
        found = self._find_managed_folder(parent_id, folder_key)
        if found:
            return str(found["id"])
        existing = self.get_file_metadata(folder_id, missing_ok=True)
        if existing:
            return folder_id
        raise RuntimeError("conflito ao criar pasta no Google Drive")

    def ensure_folder_path(self, root_id: str, relative_path: str | None) -> str:
        if not DRIVE_ID_RE.fullmatch(root_id):
            raise UnsafeMediaError("pasta raiz Google Drive invalida")
        raw = str(relative_path or "")
        if not raw.strip():
            return root_id
        raw_parts, _ = _portable_parts(raw)
        parent_id = root_id
        for component in raw_parts:
            parent_id = self.ensure_folder(parent_id, component)
        return parent_id

    @staticmethod
    def _validate_upload_session_url(session_url: str) -> str:
        parsed = urlparse(session_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.googleapis.com"
            or not parsed.path.startswith("/upload/drive/v3/files")
        ):
            raise UnsafeMediaError("URI de sessao resumable Google Drive invalida")
        return session_url

    def _start_resumable_upload(
        self,
        *,
        file_id: str,
        parent_id: str,
        name: str,
        mime_type: str,
        size: int,
        app_properties: Mapping[str, Any] | None,
    ) -> str:
        response = self._post(
            "files",
            upload=True,
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,size,md5Checksum,sha256Checksum,parents,appProperties",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime_type,
            },
            json_body={
                "id": file_id,
                "name": safe_drive_component(name),
                "mimeType": mime_type,
                "parents": [parent_id],
                "appProperties": _bounded_app_properties(app_properties),
            },
            attempts=3,
        )
        session_url = _header(response, "Location") or ""
        response.close()
        return self._validate_upload_session_url(session_url)

    def _query_upload(self, session_url: str, size: int) -> tuple[str, int]:
        response = self._request(
            "PUT",
            self._validate_upload_session_url(session_url),
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            attempts=3,
            accepted=frozenset({200, 201, 308, 404}),
        )
        try:
            if response.status_code in {200, 201}:
                return "complete", size
            if response.status_code == 404:
                return "expired", 0
            return "active", _next_upload_offset(response)
        finally:
            response.close()

    def upload_resumable(
        self,
        local_path: Path,
        parent_id: str,
        *,
        name: str | None = None,
        mime_type: str | None = None,
        app_properties: Mapping[str, Any] | None = None,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK,
        upload_state: dict[str, Any] | None = None,
        on_progress: Callable[[int, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Faz upload idempotente e retomavel de um blob local verificado."""
        if _is_reparse_point(local_path):
            raise UnsafeMediaError("origem de upload nao pode ser link ou reparse point")
        selected_path = local_path.resolve(strict=True)
        if not selected_path.is_file():
            raise UnsafeMediaError("origem de upload nao e arquivo regular")
        if not DRIVE_ID_RE.fullmatch(parent_id):
            raise UnsafeMediaError("pasta Google Drive invalida")
        if chunk_size <= 0 or chunk_size % UPLOAD_GRANULARITY:
            raise ValueError("chunk de upload deve ser multiplo de 256 KiB")
        size = selected_path.stat().st_size
        if size <= 0:
            raise ValueError("upload resumable exige arquivo nao vazio")
        checksums = file_checksums(selected_path)
        state = upload_state if upload_state is not None else {}
        if state.get("size") not in {None, size}:
            raise RuntimeError("arquivo local mudou desde o inicio do upload")
        if state.get("sha256") not in {None, checksums.sha256}:
            raise RuntimeError("conteudo local mudou desde o inicio do upload")
        state.update({"size": size, "md5": checksums.md5, "sha256": checksums.sha256})
        file_id = str(state.get("file_id") or "")
        if not file_id:
            file_id = self.generate_file_id()
            state["file_id"] = file_id
        elif not DRIVE_ID_RE.fullmatch(file_id):
            raise UnsafeMediaError("fileId resumable invalido")

        if state.get("completed"):
            existing = self.get_file_metadata(file_id, missing_ok=True)
            if existing:
                verify_uploaded_metadata(existing, size=size, checksums=checksums)
                return {
                    **existing,
                    "local_path": str(selected_path),
                    "upload_state": dict(state),
                }
            state.pop("completed", None)
            state.pop("session_url", None)
            state["offset"] = 0

        def notify(offset: int) -> None:
            state["offset"] = offset
            if on_progress:
                on_progress(offset, dict(state))

        notify(int(state.get("offset") or 0))
        selected_mime = mime_type or mimetypes.guess_type(selected_path.name)[0]
        selected_mime = selected_mime or "application/octet-stream"
        completed = False
        for restart in range(3):
            session_url = str(state.get("session_url") or "")
            offset = int(state.get("offset") or 0)
            if session_url:
                status, offset = self._query_upload(session_url, size)
                if status == "complete":
                    completed = True
                    break
                if status == "expired":
                    state.pop("session_url", None)
                    state["offset"] = 0
                    existing = self.get_file_metadata(file_id, missing_ok=True)
                    if existing:
                        verify_uploaded_metadata(existing, size=size, checksums=checksums)
                        completed = True
                        break
                    session_url = ""
                    offset = 0
                notify(offset)
            if not session_url:
                session_url = self._start_resumable_upload(
                    file_id=file_id,
                    parent_id=parent_id,
                    name=name or selected_path.name,
                    mime_type=selected_mime,
                    size=size,
                    app_properties={
                        **dict(app_properties or {}),
                        "ofc_sha256": checksums.sha256,
                    },
                )
                state["session_url"] = session_url
                offset = 0
                notify(offset)
            expired = False
            with selected_path.open("rb") as source:
                source.seek(offset)
                while offset < size:
                    chunk = source.read(min(chunk_size, size - offset))
                    if not chunk:
                        raise RuntimeError("arquivo local terminou antes do tamanho esperado")
                    end = offset + len(chunk) - 1
                    response = self._request(
                        "PUT",
                        session_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{size}",
                            "Content-Type": selected_mime,
                        },
                        data=chunk,
                        attempts=3,
                        accepted=frozenset({200, 201, 308, 404}),
                    )
                    if response.status_code == 404:
                        response.close()
                        state.pop("session_url", None)
                        state["offset"] = 0
                        expired = True
                        break
                    if response.status_code in {200, 201}:
                        response.close()
                        offset = size
                        notify(offset)
                        completed = True
                        break
                    next_offset = _next_upload_offset(response)
                    response.close()
                    if next_offset <= offset or next_offset > size:
                        raise RuntimeError("Google Drive nao avancou o upload resumable")
                    offset = next_offset
                    source.seek(offset)
                    notify(offset)
            if completed:
                break
            if not expired:
                raise RuntimeError("upload resumable terminou sem confirmacao")
            if restart == 2:
                break
        if not completed:
            raise RuntimeError("sessao resumable expirou repetidamente")
        metadata = self.get_file_metadata(file_id)
        if metadata is None:  # pragma: no cover - protegido por missing_ok=False
            raise RuntimeError("arquivo enviado nao foi encontrado no Google Drive")
        verify_uploaded_metadata(metadata, size=size, checksums=checksums)
        state.update({"offset": size, "completed": True})
        notify(size)
        return {**metadata, "local_path": str(selected_path), "upload_state": dict(state)}

    def download_to_local(
        self,
        file_id: str,
        destination: Path,
        *,
        metadata: Mapping[str, Any] | None = None,
        chunk_size: int = DOWNLOAD_CHUNK,
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Retoma em .part, verifica checksum e publica por rename atomico."""
        selected_metadata = dict(metadata or self.get_file_metadata(file_id) or {})
        size = int(selected_metadata.get("size") or 0)
        if size <= 0:
            raise RuntimeError("arquivo Drive sem tamanho de blob")
        remote_checksums = Checksums(
            str(selected_metadata.get("md5Checksum") or "").casefold(),
            str(selected_metadata.get("sha256Checksum") or "").casefold(),
        )
        if not remote_checksums.md5 and not remote_checksums.sha256:
            raise RuntimeError("arquivo Drive sem checksum verificavel")
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(f"{destination.name}.part")
        if _is_reparse_point(destination) or _is_reparse_point(part):
            raise UnsafeMediaError("destino local nao pode ser link ou reparse point")
        if destination.exists():
            existing = file_checksums(destination)
            verify_uploaded_metadata(
                {
                    "size": destination.stat().st_size,
                    "md5Checksum": remote_checksums.md5,
                    "sha256Checksum": remote_checksums.sha256,
                },
                size=size,
                checksums=existing,
            )
            return {**selected_metadata, "local_path": str(destination), "resumed": False}
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            raise RuntimeError("arquivo parcial local e maior que o arquivo Drive")
        if on_progress:
            on_progress(offset)
        if offset < size:
            upstream = self.open_media(file_id, f"bytes={offset}-{size - 1}" if offset else None)
            if offset and upstream.status_code != 206:
                upstream.close()
                raise RuntimeError("Google Drive ignorou a retomada por Range")
            mode = "ab" if offset else "wb"
            try:
                with part.open(mode) as target:
                    for chunk in upstream.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        remaining = size - offset
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        target.write(chunk)
                        offset += len(chunk)
                        if on_progress:
                            on_progress(offset)
                        if offset >= size:
                            break
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                upstream.close()
        if offset != size:
            raise RuntimeError("download Drive terminou antes do tamanho esperado")
        checksums = file_checksums(part)
        verify_uploaded_metadata(
            {
                "size": size,
                "md5Checksum": remote_checksums.md5,
                "sha256Checksum": remote_checksums.sha256,
            },
            size=size,
            checksums=checksums,
        )
        part.replace(destination)
        return {**selected_metadata, "local_path": str(destination), "resumed": True}

    def open_media(self, file_id: str, range_header: str | None) -> ResponseLike:
        if not DRIVE_ID_RE.fullmatch(file_id):
            raise UnsafeMediaError("arquivo Google Drive invalido")
        headers = {"Range": range_header} if range_header else {}
        return self._get(
            f"files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers,
            stream=True,
            attempts=3,
        )


class DriveCatalog:
    def __init__(
        self,
        settings: Settings,
        client: DriveClient,
        *,
        connection_factory: Callable[[Settings], AbstractContextManager[Any]] = connection,
    ) -> None:
        self.settings = settings
        self.client = client
        self.connection_factory = connection_factory
        self.cursor_key = f"root:{settings.gdrive_root_id}"
        self.drive_scope: DriveScope | None = None

    def _cursor_drive_id(self) -> str:
        return self.drive_scope.cursor_drive_id if self.drive_scope else "default"

    def _resolve_scope(self, cursor: Mapping[str, Any]) -> DriveScope:
        stored = str(cursor.get("drive_id") or "").strip()
        if stored == "user":
            return DriveScope(drive_id=None, corpus="user")
        if stored not in {"", "default"} and DRIVE_ID_RE.fullmatch(stored):
            return DriveScope(drive_id=stored, corpus="drive")
        return self.client.discover_root_scope(self.settings.gdrive_root_id)

    def _load_cursor(self) -> dict[str, Any] | None:
        with self.connection_factory(self.settings) as database:
            row = database.execute(
                """
                SELECT drive_id,page_token,pending_page_token,last_polled_at,
                       last_success_at,last_error
                FROM ops.drive_cursors WHERE cursor_key=%s
                """,
                (self.cursor_key,),
            ).fetchone()
        return dict(row) if row else None

    def _stage_cursor(self, page_token: str) -> None:
        with self.connection_factory(self.settings) as database:
            database.execute(
                """
                INSERT INTO ops.drive_cursors(
                  cursor_key,drive_id,root_folder_id,pending_page_token,
                  last_polled_at,last_error)
                VALUES(%s,%s,%s,%s,now(),NULL)
                ON CONFLICT(cursor_key) DO UPDATE SET
                  drive_id=excluded.drive_id,root_folder_id=excluded.root_folder_id,
                  pending_page_token=excluded.pending_page_token,
                  last_polled_at=now(),last_error=NULL,updated_at=now()
                """,
                (
                    self.cursor_key,
                    self._cursor_drive_id(),
                    self.settings.gdrive_root_id,
                    page_token,
                ),
            )
            database.commit()

    def _clear_cursor(self, reason: str) -> None:
        with self.connection_factory(self.settings) as database:
            database.execute(
                """
                INSERT INTO ops.drive_cursors(
                  cursor_key,drive_id,root_folder_id,page_token,pending_page_token,
                  last_polled_at,last_error)
                VALUES(%s,%s,%s,NULL,NULL,now(),%s)
                ON CONFLICT(cursor_key) DO UPDATE SET
                  drive_id=excluded.drive_id,root_folder_id=excluded.root_folder_id,
                  page_token=NULL,
                  pending_page_token=NULL,last_polled_at=now(),last_error=excluded.last_error,
                  updated_at=now()
                """,
                (
                    self.cursor_key,
                    self._cursor_drive_id(),
                    self.settings.gdrive_root_id,
                    reason[:2000],
                ),
            )
            database.commit()

    def _record_cursor_error(self, message: str) -> None:
        with self.connection_factory(self.settings) as database:
            database.execute(
                """
                INSERT INTO ops.drive_cursors(
                  cursor_key,drive_id,root_folder_id,last_polled_at,last_error)
                VALUES(%s,%s,%s,now(),%s)
                ON CONFLICT(cursor_key) DO UPDATE SET
                  drive_id=excluded.drive_id,last_polled_at=now(),
                  last_error=excluded.last_error,updated_at=now()
                """,
                (
                    self.cursor_key,
                    self._cursor_drive_id(),
                    self.settings.gdrive_root_id,
                    message[:2000],
                ),
            )
            database.commit()

    def _finalize_cursor(self, database: Any, page_token: str) -> None:
        database.execute(
            """
            INSERT INTO ops.drive_cursors(
              cursor_key,drive_id,root_folder_id,page_token,pending_page_token,
              last_polled_at,last_success_at,last_error)
            VALUES(%s,%s,%s,%s,NULL,now(),now(),NULL)
            ON CONFLICT(cursor_key) DO UPDATE SET
              drive_id=excluded.drive_id,root_folder_id=excluded.root_folder_id,
              page_token=excluded.page_token,
              pending_page_token=NULL,last_polled_at=now(),last_success_at=now(),
              last_error=NULL,updated_at=now()
            """,
            (
                self.cursor_key,
                self._cursor_drive_id(),
                self.settings.gdrive_root_id,
                page_token,
            ),
        )

    def _finish_unchanged(self, run_id: uuid.UUID, page_token: str) -> dict[str, int]:
        with self.connection_factory(self.settings) as database:
            source = database.execute(
                "SELECT row_counts FROM catalog.sources WHERE site='gdrive'"
            ).fetchone()
            previous = dict(source.get("row_counts") or {}) if source else {}
            counts = {
                "folders": int(previous.get("folders") or 0),
                "files": int(previous.get("files") or 0),
                "blobs": int(previous.get("blobs") or 0),
                "videos": int(previous.get("videos") or 0),
                "subtitles": int(previous.get("subtitles") or 0),
                "rejected": int(previous.get("rejected") or 0),
                "changes": 0,
                "reconciled": 0,
                "skipped": 1,
            }
            self._finalize_cursor(database, page_token)
            database.execute(
                """
                INSERT INTO catalog.sources(
                  site,kind,source_path,last_synced_at,row_counts,last_error)
                VALUES('gdrive','google-drive','#AVideos',now(),%s,NULL)
                ON CONFLICT(site) DO UPDATE SET
                  last_synced_at=now(),last_error=NULL
                """,
                (Jsonb(counts),),
            )
            database.execute(
                """
                UPDATE ops.ingestion_runs SET status='done',rows_read=0,rows_written=0,
                  counts=%s,finished_at=now() WHERE id=%s
                """,
                (Jsonb(counts), run_id),
            )
            database.commit()
        return counts

    def scan(self) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        pending: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
            [(self.settings.gdrive_root_id, (), ())]
        )
        seen_folders = {self.settings.gdrive_root_id}
        groups: dict[str, dict[str, Any]] = {}
        counts = {
            "folders": 0,
            "files": 0,
            "blobs": 0,
            "videos": 0,
            "subtitles": 0,
            "rejected": 0,
        }
        while pending:
            batch = [pending.popleft() for _ in range(min(PARENT_BATCH, len(pending)))]
            parents = {item[0]: item for item in batch}
            drive_id = self.drive_scope.drive_id if self.drive_scope else None
            children = (
                self.client.list_children(list(parents), drive_id=drive_id)
                if drive_id
                else self.client.list_children(list(parents))
            )
            components: dict[tuple[str, int], str] = {}
            for parent_id in parents:
                entries = []
                for position, child in enumerate(children):
                    if parent_id not in child.get("parents", []):
                        continue
                    child_id = str(child.get("id") or "")
                    entries.append(
                        (
                            (parent_id, position),
                            str(child.get("name") or "sem-nome"),
                            child_id or f"{parent_id}:{position}",
                        )
                    )
                components.update(unique_drive_components(entries))
            for position, item in enumerate(children):
                parent_id = next(
                    (value for value in item.get("parents", []) if value in parents), None
                )
                if parent_id is None:
                    continue
                _, parent_parts, parent_folder_ids = parents[parent_id]
                component = components[(parent_id, position)]
                item_id = str(item.get("id") or "")
                if not DRIVE_ID_RE.fullmatch(item_id):
                    counts["rejected"] += 1
                    continue
                if item.get("mimeType") == FOLDER_MIME:
                    if item_id not in seen_folders:
                        seen_folders.add(item_id)
                        counts["folders"] += 1
                        pending.append(
                            (
                                item_id,
                                (*parent_parts, component),
                                (*parent_folder_ids, item_id),
                            )
                        )
                    continue
                counts["files"] += 1
                size = int(item.get("size") or 0)
                can_download = bool(item.get("capabilities", {}).get("canDownload"))
                mime_type = str(item.get("mimeType") or "application/octet-stream")
                if size <= 0 or not can_download or mime_type.startswith(GOOGLE_MIME_PREFIX):
                    counts["rejected"] += 1
                    continue
                classification = classify_file(component, mime_type)
                identity, title, category, infohash = drive_group(
                    parent_parts, parent_folder_ids, item_id, component
                )
                relative_path = safe_relative_path("/".join((*parent_parts, component)))
                group_id = identity.partition(":")[2]
                group = groups.setdefault(
                    infohash,
                    {
                        "infohash": infohash,
                        "identity": identity,
                        "group_id": group_id,
                        "title": title,
                        "category": category,
                        "files": [],
                    },
                )
                group["files"].append(
                    {
                        "drive_file_id": item_id,
                        "folder_id": parent_id,
                        "name": component,
                        "path": relative_path,
                        "extension": classification.extension,
                        "size": size,
                        "mime_type": classification.mime_type,
                        "file_kind": classification.kind,
                        "is_video": classification.is_video,
                        "is_subtitle": classification.is_subtitle,
                        "md5_checksum": item.get("md5Checksum"),
                        "sha256_checksum": item.get("sha256Checksum"),
                        "modified_time": item.get("modifiedTime"),
                        "can_download": can_download,
                        "source_record": {
                            "drive_version": item.get("version"),
                            "sha1_checksum": item.get("sha1Checksum"),
                            "sha256_checksum": item.get("sha256Checksum"),
                            "video_media_metadata": item.get("videoMediaMetadata") or {},
                        },
                    }
                )
                counts["blobs"] += 1
                counts["videos"] += int(classification.is_video)
                counts["subtitles"] += int(classification.is_subtitle)
        return groups, counts

    def sync_once(self) -> dict[str, int]:
        run_id = uuid.uuid4()
        with self.connection_factory(self.settings) as database:
            database.execute(
                "INSERT INTO ops.ingestion_runs(id,source,status) VALUES(%s,'gdrive','running')",
                (run_id,),
            )
            database.commit()
        try:
            cursor = self._load_cursor() or {}
            self.drive_scope = self._resolve_scope(cursor)
            committed_token = str(cursor.get("page_token") or "").strip()
            candidate_token: str | None = None
            change_count = 0
            cursor_reset = False
            has_removals = False
            if committed_token:
                try:
                    feed = self.client.list_changes(
                        committed_token, drive_id=self.drive_scope.drive_id
                    )
                except InvalidDriveCursor as exc:
                    cursor_reset = True
                    self._clear_cursor(str(exc))
                else:
                    candidate_token = feed.new_start_page_token
                    change_count = len(feed.changes)
                    has_removals = feed.has_removals
                    if not feed.changes:
                        return self._finish_unchanged(run_id, candidate_token)
            if candidate_token is None:
                # Capturar antes do scan garante que mudancas concorrentes
                # continuem visiveis no proximo poll.
                candidate_token = self.client.get_start_page_token(
                    drive_id=self.drive_scope.drive_id
                )
            # pending_page_token pode sobreviver a uma queda, mas page_token
            # so avanca na mesma transacao que publica a reconciliacao.
            self._stage_cursor(candidate_token)
            groups, counts = self.scan()
            counts = {
                **counts,
                "changes": change_count,
                "removals": int(has_removals),
                "cursor_reset": int(cursor_reset),
                "reconciled": 1,
                "skipped": 0,
            }
            with self.connection_factory(self.settings) as database:
                database.execute("UPDATE catalog.drive_files SET active=FALSE")
                for group in groups.values():
                    files = group["files"]
                    total_size = sum(item["size"] for item in files)
                    source_url = (
                        f"https://drive.google.com/drive/folders/{group['group_id']}"
                        if group["identity"].startswith("folder:")
                        else f"https://drive.google.com/file/d/{group['group_id']}/view"
                    )
                    database.execute(
                        """
                        INSERT INTO catalog.torrents(
                          site,infohash,source_url,metainfo_relpath,display_name,title,
                          category,total_size,file_count,torrent_version,downloaded_at,
                          active,source_record)
                        VALUES('gdrive',%s,%s,%s,%s,%s,%s,%s,%s,'google-drive',
                          (SELECT max(value->>'modified_time')::timestamptz
                           FROM jsonb_array_elements(%s::jsonb) value),TRUE,%s)
                        ON CONFLICT(site,infohash) DO UPDATE SET
                          source_url=excluded.source_url,
                          metainfo_relpath=excluded.metainfo_relpath,
                          display_name=excluded.display_name,title=excluded.title,
                          category=excluded.category,total_size=excluded.total_size,
                          file_count=excluded.file_count,torrent_version='google-drive',
                          downloaded_at=excluded.downloaded_at,active=TRUE,
                          source_record=excluded.source_record,updated_at=now()
                        """,
                        (
                            group["infohash"],
                            source_url,
                            f"gdrive/{group['group_id']}",
                            group["title"],
                            group["title"],
                            group["category"],
                            total_size,
                            len(files),
                            Jsonb(
                                [
                                    {"modified_time": item.get("modified_time")}
                                    for item in files
                                    if item.get("modified_time")
                                ]
                            ),
                            Jsonb(
                                {
                                    "provider": "google-drive",
                                    "identity": group["identity"],
                                    "group_id": group["group_id"],
                                }
                            ),
                        ),
                    )
                    torrent_id = database.execute(
                        "SELECT id FROM catalog.torrents WHERE site='gdrive' AND infohash=%s",
                        (group["infohash"],),
                    ).fetchone()["id"]
                    for index, item in enumerate(sorted(files, key=lambda row: row["path"])):
                        torrent_file_id = database.execute(
                            """
                            INSERT INTO catalog.torrent_files(
                              torrent_id,file_index,path,extension,size,is_video,
                              file_kind,mime_type,is_subtitle,sha256)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(torrent_id,path) DO UPDATE SET
                              file_index=excluded.file_index,extension=excluded.extension,
                              size=excluded.size,is_video=excluded.is_video,
                              file_kind=excluded.file_kind,mime_type=excluded.mime_type,
                              is_subtitle=excluded.is_subtitle,sha256=excluded.sha256,
                              updated_at=now()
                            RETURNING id
                            """,
                            (
                                torrent_id,
                                index,
                                item["path"],
                                item["extension"],
                                item["size"],
                                item["is_video"],
                                item["file_kind"],
                                item["mime_type"],
                                item["is_subtitle"],
                                item["sha256_checksum"],
                            ),
                        ).fetchone()["id"]
                        database.execute(
                            """
                            INSERT INTO catalog.drive_files(
                              drive_file_id,torrent_file_id,folder_id,relative_path,
                              mime_type,md5_checksum,modified_time,can_download,
                              active,source_record)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
                            ON CONFLICT(drive_file_id) DO UPDATE SET
                              torrent_file_id=excluded.torrent_file_id,
                              folder_id=excluded.folder_id,
                              relative_path=excluded.relative_path,
                              mime_type=excluded.mime_type,
                              md5_checksum=excluded.md5_checksum,
                              modified_time=excluded.modified_time,
                              can_download=excluded.can_download,
                              active=TRUE,source_record=excluded.source_record,
                              updated_at=now()
                            """,
                            (
                                item["drive_file_id"],
                                torrent_file_id,
                                item["folder_id"],
                                item["path"],
                                item["mime_type"],
                                item["md5_checksum"],
                                item["modified_time"],
                                item["can_download"],
                                Jsonb(item["source_record"]),
                            ),
                        )
                database.execute(
                    """
                    UPDATE catalog.torrents t SET active=EXISTS(
                      SELECT 1 FROM catalog.torrent_files f
                      JOIN catalog.drive_files d ON d.torrent_file_id=f.id
                      WHERE f.torrent_id=t.id AND d.active AND d.can_download),updated_at=now()
                    WHERE t.site='gdrive'
                    """
                )
                database.execute(
                    """
                    INSERT INTO catalog.sources(
                      site,kind,source_path,last_snapshot_at,last_synced_at,row_counts,last_error)
                    VALUES('gdrive','google-drive','#AVideos',now(),now(),%s,NULL)
                    ON CONFLICT(site) DO UPDATE SET kind='google-drive',source_path='#AVideos',
                      last_snapshot_at=now(),last_synced_at=now(),
                      row_counts=excluded.row_counts,last_error=NULL
                    """,
                    (Jsonb(counts),),
                )
                database.execute(
                    """
                    UPDATE ops.ingestion_runs SET status='done',rows_read=%s,
                      rows_written=%s,counts=%s,finished_at=now() WHERE id=%s
                    """,
                    (counts["files"], counts["blobs"], Jsonb(counts), run_id),
                )
                self._finalize_cursor(database, candidate_token)
                database.commit()
            return counts
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            try:
                with self.connection_factory(self.settings) as database:
                    database.execute(
                        "UPDATE ops.ingestion_runs SET status='failed',error=%s,finished_at=now() WHERE id=%s",
                        (message, run_id),
                    )
                    database.execute(
                        """
                        INSERT INTO catalog.sources(site,kind,source_path,last_error)
                        VALUES('gdrive','google-drive','#AVideos',%s)
                        ON CONFLICT(site) DO UPDATE SET last_error=excluded.last_error
                        """,
                        (message,),
                    )
                    database.commit()
            except Exception:
                LOG.exception("falha ao registrar ingestion_run do Google Drive")
            try:
                self._record_cursor_error(message)
            except Exception:
                LOG.exception("falha ao registrar erro do cursor Google Drive")
            raise


class TransferStore(Protocol):
    def claim_upload(self) -> dict[str, Any] | None: ...

    def claim_download(self) -> dict[str, Any] | None: ...

    def update(self, job_id: Any, **fields: Any) -> None: ...

    def persist_file_sha256(self, file_id: Any, sha256: str) -> bool: ...


class PostgresTransferStore:
    """Adaptador pequeno para manter o worker testavel sem PostgreSQL real."""

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[Settings], AbstractContextManager[Any]] = connection,
        resume_after_seconds: int = TRANSFER_RESUME_AFTER_SECONDS,
    ) -> None:
        self.settings = settings
        self.connection_factory = connection_factory
        self.resume_after_seconds = max(30, int(resume_after_seconds))

    def _claim(
        self,
        *,
        target: str,
        fresh_state: str,
        claimed_state: str,
        resumable_states: tuple[str, ...],
    ) -> dict[str, Any] | None:
        with self.connection_factory(self.settings) as database:
            row = database.execute(
                """
                WITH candidate AS (
                  SELECT id FROM runtime.transfer_jobs
                  WHERE target=%s AND (
                    state=%s OR (
                      state=ANY(%s::text[])
                      AND updated_at <= now()-make_interval(secs=>%s)
                    )
                  )
                    AND (%s <> 'local' OR source_site='gdrive')
                  ORDER BY CASE WHEN state=%s THEN 0 ELSE 1 END,updated_at,id
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE runtime.transfer_jobs job
                SET state=CASE WHEN job.state=%s THEN %s ELSE job.state END,error=NULL
                FROM candidate
                WHERE job.id=candidate.id
                RETURNING job.*
                """,
                (
                    target,
                    fresh_state,
                    list(resumable_states),
                    self.resume_after_seconds,
                    target,
                    fresh_state,
                    fresh_state,
                    claimed_state,
                ),
            ).fetchone()
            database.commit()
        return dict(row) if row else None

    def claim_upload(self) -> dict[str, Any] | None:
        return self._claim(
            target="gdrive",
            fresh_state="downloaded",
            claimed_state="classifying",
            resumable_states=("classifying", "uploading", "verifying"),
        )

    def claim_download(self) -> dict[str, Any] | None:
        return self._claim(
            target="local",
            fresh_state="queued",
            claimed_state="validating",
            resumable_states=(
                "validating",
                "downloading",
                "downloaded",
                "classifying",
                "verifying",
            ),
        )

    def update(self, job_id: Any, **fields: Any) -> None:
        allowed = {
            "state",
            "bytes_total",
            "bytes_done",
            "local_files",
            "drive_files",
            "upload_state",
            "error",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in selected.items():
            assignments.append(f"{key}=%s")
            values.append(
                Jsonb(value)
                if key in {"local_files", "drive_files", "upload_state"}
                else value
            )
        values.append(job_id)
        with self.connection_factory(self.settings) as database:
            database.execute(
                f"UPDATE runtime.transfer_jobs SET {','.join(assignments)} WHERE id=%s",
                tuple(values),
            )
            database.commit()

    def persist_file_sha256(self, file_id: Any, sha256: str) -> bool:
        try:
            selected_id = int(file_id)
        except (TypeError, ValueError):
            return False
        if selected_id <= 0:
            return False
        digest = normalize_sha256(sha256)
        if digest is None:  # pragma: no cover - sha256 obrigatorio na assinatura
            return False
        with self.connection_factory(self.settings) as database:
            row = database.execute(
                """
                UPDATE catalog.torrent_files SET sha256=%s,updated_at=now()
                WHERE id=%s AND (sha256 IS NULL OR sha256=%s)
                RETURNING id
                """,
                (digest, selected_id, digest),
            ).fetchone()
            if row is None:
                existing = database.execute(
                    "SELECT sha256 FROM catalog.torrent_files WHERE id=%s",
                    (selected_id,),
                ).fetchone()
                if existing and str(existing.get("sha256") or "").strip() != digest:
                    raise RuntimeError("SHA-256 local diverge do inventario canonico")
            database.commit()
        return row is not None


def _manifest_items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) if isinstance(item, Mapping) else {"path": str(item)} for item in value]
    if isinstance(value, Mapping):
        if isinstance(value.get("files"), list):
            return _manifest_items(value["files"])
        if any(key in value for key in ("path", "local_path", "drive_file_id", "id")):
            return [dict(value)]
        return [
            {**dict(item), "manifest_key": str(key)}
            if isinstance(item, Mapping)
            else {"path": str(item), "manifest_key": str(key)}
            for key, item in value.items()
        ]
    raise UnsafeMediaError("manifesto de transferencia invalido")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeMediaError(f"nao foi possivel validar reparse point: {path}") from exc


def _assert_no_reparse_points(path: Path, *, stop_at: Path | None = None) -> None:
    selected = path.absolute()
    stop = stop_at.absolute() if stop_at else None
    while True:
        if stop is not None and selected == stop:
            return
        if _is_reparse_point(selected):
            raise UnsafeMediaError(f"caminho contem link ou reparse point: {selected}")
        parent = selected.parent
        if parent == selected:
            return
        selected = parent


def _portable_parts(value: str) -> tuple[list[str], list[str]]:
    raw = unicodedata.normalize("NFKC", str(value)).replace("\\", "/")
    if (
        not raw
        or raw.startswith(("/", "//"))
        or WINDOWS_DRIVE_PATH_RE.match(raw)
        or "\x00" in raw
    ):
        raise UnsafeMediaError("caminho relativo portavel invalido")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeMediaError("navegacao relativa invalida")
    return parts, [safe_drive_component(component) for component in parts]


def portable_relative_path(value: str) -> str:
    _, parts = _portable_parts(value)
    return "/".join(parts)


def unique_relative_paths(
    entries: Iterable[tuple[Any, str, str]],
) -> dict[Any, str]:
    prepared: list[dict[str, Any]] = []
    for key, raw, identity in entries:
        raw_parts, parts = _portable_parts(raw)
        prepared.append(
            {"key": key, "raw_parts": raw_parts, "parts": parts, "identity": identity}
        )
    max_depth = max((len(item["parts"]) - 1 for item in prepared), default=0)
    for depth in range(max_depth):
        groups: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = {}
        for item in prepared:
            if depth >= len(item["parts"]) - 1:
                continue
            parent = tuple(
                unicodedata.normalize("NFKC", value).casefold()
                for value in item["parts"][:depth]
            )
            component = unicodedata.normalize(
                "NFKC", item["parts"][depth]
            ).casefold()
            groups.setdefault((parent, component), []).append(item)
        for colliding in groups.values():
            raw_values = {
                unicodedata.normalize("NFKC", item["raw_parts"][depth])
                for item in colliding
            }
            if len(raw_values) <= 1:
                continue
            for item in colliding:
                prefix = "/".join(item["raw_parts"][: depth + 1])
                item["parts"][depth] = _identity_suffix(
                    item["parts"][depth], prefix
                )
    collisions: dict[str, int] = {}
    for item in prepared:
        relative = "/".join(item["parts"])
        folded = unicodedata.normalize("NFKC", relative).casefold()
        collisions[folded] = collisions.get(folded, 0) + 1
    result: dict[Any, str] = {}
    for item in prepared:
        key = item["key"]
        identity = item["identity"]
        relative = "/".join(item["parts"])
        folded = unicodedata.normalize("NFKC", relative).casefold()
        if collisions[folded] > 1:
            item["parts"][-1] = _identity_suffix(item["parts"][-1], identity)
            relative = "/".join(item["parts"])
        result[key] = relative
    return result


def relative_to_destination_group(
    paths: list[str], destination: Path | str
) -> list[str]:
    """Remove apenas a sobreposicao raiz->prefixo, preservando o restante da arvore."""
    destination_parts = [
        unicodedata.normalize("NFKC", value).casefold()
        for value in Path(destination).parts
        if value not in {Path(destination).anchor, "", os.sep}
    ]
    result: list[str] = []
    for path in paths:
        parts = path.split("/")
        folded = [
            unicodedata.normalize("NFKC", value).casefold() for value in parts
        ]
        # Nunca consome o ultimo componente: ele e o nome do arquivo e a
        # arvore resultante precisa continuar sendo um caminho de arquivo.
        maximum = min(len(destination_parts), max(0, len(parts) - 1))
        strip = 0
        for length in range(maximum, 0, -1):
            if destination_parts[-length:] == folded[:length]:
                strip = length
                break
        result.append("/".join(parts[strip:]))
    return result


def _assert_unique_relative_paths(paths: Iterable[str]) -> None:
    seen: set[str] = set()
    for path in paths:
        folded = unicodedata.normalize("NFKC", path).casefold()
        if folded in seen:
            raise UnsafeMediaError("manifesto produz caminhos locais colidentes")
        seen.add(folded)


def _local_file_matches_metadata(
    path: Path, metadata: Mapping[str, Any]
) -> bool:
    if not path.is_file():
        return False
    size = int(metadata.get("size") or 0)
    if size <= 0 or path.stat().st_size != size:
        return False
    try:
        verify_uploaded_metadata(
            metadata, size=size, checksums=file_checksums(path)
        )
    except RuntimeError:
        return False
    return True


def _collision_safe_local_relative(
    destination_root: Path,
    relative: str,
    file_id: str,
    metadata: Mapping[str, Any],
) -> str:
    """Mantem o nome quando livre/identico ou usa um sufixo estavel por fileId."""
    destination = destination_root / Path(relative)
    _assert_no_reparse_points(destination, stop_at=destination_root)
    if not destination.exists() or _local_file_matches_metadata(destination, metadata):
        return relative
    parts = relative.split("/")
    parts[-1] = _identity_suffix(parts[-1], f"gdrive:{file_id}")
    alternate = "/".join(parts)
    alternate_path = destination_root / Path(alternate)
    _assert_no_reparse_points(alternate_path, stop_at=destination_root)
    if not alternate_path.exists() or _local_file_matches_metadata(
        alternate_path, metadata
    ):
        return alternate
    raise RuntimeError("colisao local deterministica com conteudo divergente")


class DriveTransferWorker:
    def __init__(
        self,
        settings: Settings,
        client: DriveClient,
        *,
        store: TransferStore | None = None,
        allowed_local_roots: Iterable[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store or PostgresTransferStore(settings)
        configured_subtitles = getattr(settings, "subtitle_file_root", None)
        default_roots = [settings.media_root, settings.resume_root]
        if configured_subtitles is not None:
            default_roots.append(Path(configured_subtitles))
        roots = allowed_local_roots or tuple(default_roots)
        self.allowed_local_roots = tuple(Path(root).resolve() for root in roots)

    def _source_path(self, item: Mapping[str, Any]) -> Path:
        raw = item.get("local_path") or item.get("absolute_path") or item.get("path")
        if not raw:
            raise UnsafeMediaError("local_files sem caminho")
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = self.allowed_local_roots[0] / safe_relative_path(str(raw))
        lexical = candidate.absolute()
        matching_roots = [
            root for root in self.allowed_local_roots if _inside(root, lexical)
        ]
        if not matching_roots:
            raise UnsafeMediaError("arquivo local fora das raizes autorizadas")
        for root in matching_roots:
            _assert_no_reparse_points(lexical, stop_at=root)
        selected = candidate.resolve(strict=True)
        if not selected.is_file() or not any(
            _inside(root, selected) for root in self.allowed_local_roots
        ):
            raise UnsafeMediaError("arquivo local fora das raizes autorizadas")
        return selected

    @staticmethod
    def _raw_relative_name(item: Mapping[str, Any], source: Path) -> str:
        raw = item.get("relative_path") or item.get("drive_path") or item.get("name")
        if not raw:
            path_value = item.get("path")
            raw = source.name if not path_value or Path(str(path_value)).is_absolute() else path_value
        raw_parts, _ = _portable_parts(str(raw))
        return "/".join(raw_parts)

    @classmethod
    def _relative_name(cls, item: Mapping[str, Any], source: Path) -> str:
        return portable_relative_path(cls._raw_relative_name(item, source))

    def _destination_root(self, value: Any) -> Path:
        root = self.allowed_local_roots[0]
        raw = str(value or "").strip()
        candidate = Path(raw) if raw else root
        if not candidate.is_absolute():
            _, portable_parts = _portable_parts(raw)
            candidate = root.joinpath(*portable_parts)
        lexical = candidate.absolute()
        if not _inside(root, lexical):
            raise UnsafeMediaError("destination_path local fora da raiz autorizada")
        _assert_no_reparse_points(lexical, stop_at=root)
        selected = lexical.resolve()
        if not _inside(root, selected):
            raise UnsafeMediaError("destination_path local fora da raiz autorizada")
        return selected

    @staticmethod
    def _remote_metadata(item: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
        file_id = str(item.get("drive_file_id") or item.get("file_id") or item.get("id") or "")
        if not DRIVE_ID_RE.fullmatch(file_id):
            raise UnsafeMediaError("drive_files sem fileId valido")
        relative = str(
            item.get("relative_path") or item.get("path") or item.get("name") or file_id
        ).replace("\\", "/")
        relative = portable_relative_path(relative)
        metadata = {
            "id": file_id,
            "name": item.get("name") or Path(relative).name,
            "size": item.get("size"),
            "mimeType": item.get("mimeType") or item.get("mime_type"),
            "md5Checksum": item.get("md5Checksum") or item.get("md5_checksum"),
            "sha256Checksum": item.get("sha256Checksum") or item.get("sha256_checksum"),
        }
        return file_id, metadata, relative

    def _verify_drive_files(
        self,
        drive_files: list[dict[str, Any]],
        file_states: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        if not drive_files:
            raise RuntimeError("job em verifying nao possui drive_files")
        verified: list[dict[str, Any]] = []
        total = 0
        seen: set[str] = set()
        for record in drive_files:
            raw_relative = str(
                record.get("relative_path")
                or record.get("path")
                or record.get("name")
                or ""
            ).replace("\\", "/")
            file_id, recorded, relative = self._remote_metadata(record)
            state_key = raw_relative if raw_relative in file_states else relative
            if state_key in seen:
                raise RuntimeError("drive_files possui caminho relativo duplicado")
            seen.add(state_key)
            state = file_states.get(state_key)
            if not isinstance(state, Mapping) or not state.get("completed"):
                raise RuntimeError("upload_state incompleto durante verificacao")
            size = int(state.get("size") or recorded.get("size") or 0)
            checksums = Checksums(
                str(state.get("md5") or record.get("md5_checksum") or "").casefold(),
                str(
                    state.get("sha256")
                    or record.get("sha256_checksum")
                    or ""
                ).casefold(),
            )
            if size <= 0 or not (checksums.md5 or checksums.sha256):
                raise RuntimeError("upload_state sem tamanho/checksum verificavel")
            remote = self.client.get_file_metadata(file_id)
            if not remote:
                raise RuntimeError("arquivo enviado desapareceu do Google Drive")
            verify_uploaded_metadata(remote, size=size, checksums=checksums)
            total += size
            verified.append(
                {
                    **record,
                    "drive_file_id": file_id,
                    "relative_path": state_key,
                    "size": size,
                    "mime_type": remote.get("mimeType") or record.get("mime_type"),
                    "md5_checksum": remote.get("md5Checksum") or checksums.md5,
                    "sha256_checksum": remote.get("sha256Checksum") or checksums.sha256,
                    "parents": remote.get("parents") or record.get("parents") or [],
                }
            )
        return verified, total

    def _verify_local_files(
        self,
        normalized: list[tuple[str, dict[str, Any], str]],
        destination_root: Path,
    ) -> tuple[list[dict[str, Any]], int]:
        local_files: list[dict[str, Any]] = []
        total = 0
        for file_id, metadata, relative in normalized:
            destination = destination_root / Path(relative)
            _assert_no_reparse_points(destination, stop_at=destination_root)
            if not destination.is_file():
                raise RuntimeError("arquivo local desapareceu antes da verificacao")
            size = int(metadata.get("size") or 0)
            checksums = file_checksums(destination)
            verify_uploaded_metadata(metadata, size=size, checksums=checksums)
            total += size
            local_files.append(
                {
                    "drive_file_id": file_id,
                    "relative_path": relative,
                    "local_path": str(destination),
                    "size": size,
                    "md5_checksum": metadata.get("md5Checksum"),
                    "sha256_checksum": metadata.get("sha256Checksum"),
                }
            )
        return local_files, total

    def process_upload_job(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        job_id = job["id"]
        job_state = str(job.get("state") or "").casefold()
        upload_state = dict(job.get("upload_state") or {})
        file_states = dict(upload_state.get("files") or {})
        drive_files = list(_manifest_items(job.get("drive_files")))
        if job_state == "verifying":
            verified, total = self._verify_drive_files(drive_files, file_states)
            self.store.update(
                job_id,
                state="completed",
                bytes_total=total,
                bytes_done=total,
                drive_files=verified,
                upload_state={**upload_state, "files": file_states, "completed": True},
                error=None,
            )
            return verified
        if job_state not in {"classifying", "uploading"}:
            raise RuntimeError(f"job gdrive nao retomavel no estado {job_state}")
        local_items = [
            *_manifest_items(job.get("local_files")),
            *_manifest_items(job.get("external_files")),
        ]
        if not local_items:
            raise RuntimeError("job gdrive nao possui arquivos locais")
        raw_sources = [(item, self._source_path(item)) for item in local_items]
        source_paths = [str(source).casefold() for _, source in raw_sources]
        if len(source_paths) != len(set(source_paths)):
            raise UnsafeMediaError("manifesto local referencia a mesma origem mais de uma vez")
        legacy_names = {
            index: self._raw_relative_name(item, source)
            for index, (item, source) in enumerate(raw_sources)
        }
        relative_names = unique_relative_paths(
            (
                index,
                self._relative_name(item, source),
                str(
                    item.get("file_id")
                    or item.get("torrent_file_id")
                    or source
                ),
            )
            for index, (item, source) in enumerate(raw_sources)
        )
        destination_path = str(job.get("destination_path") or "")
        relative_values = relative_to_destination_group(
            [relative_names[index] for index in range(len(raw_sources))],
            destination_path,
        )
        relative_names = unique_relative_paths(
            (
                index,
                relative,
                str(
                    raw_sources[index][0].get("file_id")
                    or raw_sources[index][0].get("torrent_file_id")
                    or raw_sources[index][1]
                ),
            )
            for index, relative in enumerate(relative_values)
        )
        # Jobs iniciados por versoes anteriores persistiam a chave antes da
        # sanitizacao. Preserva-la evita criar um segundo arquivo ao retomar.
        for index, legacy in legacy_names.items():
            if legacy in file_states:
                relative_names[index] = legacy
        sources = [
            (item, source, relative_names[index])
            for index, (item, source) in enumerate(raw_sources)
        ]
        _assert_unique_relative_paths(relative for _, _, relative in sources)
        total = sum(path.stat().st_size for _, path, _ in sources)
        base_folder = self.client.ensure_folder_path(
            self.settings.gdrive_root_id, destination_path
        )
        completed_bytes = 0
        for existing in file_states.values():
            if isinstance(existing, Mapping) and existing.get("completed"):
                completed_bytes += int(existing.get("size") or 0)
        self.store.update(
            job_id,
            state="uploading",
            bytes_total=total,
            bytes_done=min(completed_bytes, total),
            upload_state={**upload_state, "files": file_states},
            error=None,
        )
        for item, source, relative in sources:
            state = dict(file_states.get(relative) or {})
            previous_size = int(state.get("size") or source.stat().st_size)
            prior_completed = previous_size if state.get("completed") else 0
            if prior_completed:
                completed_bytes = max(completed_bytes, prior_completed)
            parent_relative = str(Path(relative).parent).replace("\\", "/")
            parent_id = (
                base_folder
                if parent_relative in {"", "."}
                else self.client.ensure_folder_path(base_folder, parent_relative)
            )

            def progress(offset: int, selected_state: dict[str, Any]) -> None:
                file_states[relative] = selected_state
                done = min(total, completed_bytes + offset - prior_completed)
                self.store.update(
                    job_id,
                    bytes_total=total,
                    bytes_done=max(0, done),
                    upload_state={**upload_state, "files": file_states},
                )

            result = self.client.upload_resumable(
                source,
                parent_id,
                name=Path(relative).name,
                mime_type=str(item.get("mime_type") or mimetypes.guess_type(relative)[0] or "application/octet-stream"),
                app_properties={
                    "ofc_job_id": job_id,
                    "ofc_infohash": job.get("infohash") or "",
                    "ofc_source": job.get("source_site") or "",
                    "ofc_torrent_file_id": item.get("file_id")
                    or item.get("torrent_file_id")
                    or item.get("id")
                    or "",
                },
                upload_state=state,
                on_progress=progress,
            )
            final_state = dict(result["upload_state"])
            file_states[relative] = final_state
            torrent_file_id = item.get("file_id") or item.get("torrent_file_id")
            persist_sha256 = getattr(self.store, "persist_file_sha256", None)
            if torrent_file_id is not None and callable(persist_sha256):
                persist_sha256(torrent_file_id, str(final_state["sha256"]))
            completed_bytes += source.stat().st_size - prior_completed
            record = {
                "drive_file_id": result["id"],
                "name": result.get("name") or Path(relative).name,
                "relative_path": relative,
                "size": int(result.get("size") or source.stat().st_size),
                "mime_type": result.get("mimeType"),
                "md5_checksum": result.get("md5Checksum"),
                "sha256_checksum": result.get("sha256Checksum"),
                "parents": result.get("parents") or [parent_id],
            }
            drive_files = [
                entry
                for entry in drive_files
                if str(entry.get("relative_path") or "") != relative
            ]
            drive_files.append(record)
            self.store.update(
                job_id,
                bytes_total=total,
                bytes_done=min(total, completed_bytes),
                drive_files=drive_files,
                upload_state={**upload_state, "files": file_states},
            )
        self.store.update(
            job_id,
            state="verifying",
            bytes_total=total,
            bytes_done=total,
            drive_files=drive_files,
            upload_state={**upload_state, "files": file_states, "completed": True},
            error=None,
        )
        verified, verified_total = self._verify_drive_files(drive_files, file_states)
        if verified_total != total:
            raise RuntimeError("total verificado diverge dos arquivos locais")
        self.store.update(
            job_id,
            state="completed",
            bytes_total=total,
            bytes_done=total,
            drive_files=verified,
            upload_state={**upload_state, "files": file_states, "completed": True},
            error=None,
        )
        return verified

    def process_download_job(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        job_id = job["id"]
        job_state = str(job.get("state") or "").casefold()
        if job_state not in {
            "validating",
            "downloading",
            "downloaded",
            "classifying",
            "verifying",
        }:
            raise RuntimeError(f"job local nao retomavel no estado {job_state}")
        drive_items = _manifest_items(job.get("drive_files"))
        if not drive_items:
            raise RuntimeError("job local nao possui drive_files")
        raw_normalized = [self._remote_metadata(item) for item in drive_items]
        drive_ids = [file_id for file_id, _, _ in raw_normalized]
        if len(drive_ids) != len(set(drive_ids)):
            raise UnsafeMediaError("manifesto Drive referencia o mesmo arquivo mais de uma vez")
        destination_root = self._destination_root(job.get("destination_path"))
        unique_names = unique_relative_paths(
            (index, relative, file_id)
            for index, (file_id, _, relative) in enumerate(raw_normalized)
        )
        relative_names = relative_to_destination_group(
            [unique_names[index] for index in range(len(raw_normalized))],
            destination_root,
        )
        relative_names = list(
            unique_relative_paths(
                (index, relative, raw_normalized[index][0])
                for index, relative in enumerate(relative_names)
            ).values()
        )
        normalized = [
            (file_id, metadata, relative_names[index])
            for index, (file_id, metadata, _) in enumerate(raw_normalized)
        ]
        _assert_unique_relative_paths(relative for _, _, relative in normalized)
        for file_id, metadata, _ in normalized:
            if not metadata.get("size") or not (
                metadata.get("md5Checksum") or metadata.get("sha256Checksum")
            ):
                remote = self.client.get_file_metadata(file_id)
                if remote:
                    metadata.update(remote)
        persisted_relatives: dict[str, str] = {}
        for item in _manifest_items(job.get("local_files")):
            persisted_id = str(item.get("drive_file_id") or "")
            if not persisted_id:
                continue
            if persisted_id in persisted_relatives:
                raise UnsafeMediaError(
                    "manifesto local possui fileId Drive duplicado"
                )
            persisted_raw = item.get("relative_path")
            if not persisted_raw:
                continue
            persisted = portable_relative_path(str(persisted_raw))
            persisted = relative_to_destination_group(
                [persisted], destination_root
            )[0]
            persisted_relatives[persisted_id] = persisted
        collision_safe: list[tuple[str, dict[str, Any], str]] = []
        for file_id, metadata, relative in normalized:
            persisted = persisted_relatives.get(file_id)
            if persisted:
                parts = relative.split("/")
                parts[-1] = _identity_suffix(parts[-1], f"gdrive:{file_id}")
                if persisted not in {relative, "/".join(parts)}:
                    raise UnsafeMediaError(
                        "manifesto local diverge da arvore Drive normalizada"
                    )
                relative = persisted
            relative = _collision_safe_local_relative(
                destination_root, relative, file_id, metadata
            )
            collision_safe.append((file_id, metadata, relative))
        normalized = collision_safe
        _assert_unique_relative_paths(relative for _, _, relative in normalized)
        total = sum(int(metadata.get("size") or 0) for _, metadata, _ in normalized)
        if job_state in {"downloaded", "classifying", "verifying"}:
            local_files, verified_total = self._verify_local_files(
                normalized, destination_root
            )
            if verified_total != total:
                raise RuntimeError("total local verificado diverge do manifesto Drive")
            if job_state == "downloaded":
                self.store.update(job_id, state="classifying", local_files=local_files)
                job_state = "classifying"
            if job_state == "classifying":
                self.store.update(job_id, state="verifying", local_files=local_files)
            self.store.update(
                job_id,
                state="completed",
                bytes_total=total,
                bytes_done=total,
                local_files=local_files,
                error=None,
            )
            return local_files
        local_files: list[dict[str, Any]] = []
        completed = 0
        self.store.update(
            job_id,
            state="downloading",
            bytes_total=total,
            bytes_done=0,
            error=None,
        )
        for file_id, metadata, relative in normalized:
            destination = destination_root / Path(relative)
            _assert_no_reparse_points(destination, stop_at=destination_root)
            resolved_parent = destination.parent.resolve()
            if not _inside(destination_root, resolved_parent):
                raise UnsafeMediaError("caminho Drive escapou de destination_path")

            def progress(offset: int) -> None:
                self.store.update(
                    job_id,
                    bytes_total=total,
                    bytes_done=min(total, completed + offset),
                )

            result = self.client.download_to_local(
                file_id, destination, metadata=metadata, on_progress=progress
            )
            completed += int(metadata.get("size") or 0)
            local_files = [
                entry
                for entry in local_files
                if str(entry.get("relative_path") or "") != relative
            ]
            local_files.append(
                {
                    "drive_file_id": file_id,
                    "relative_path": relative,
                    "local_path": result["local_path"],
                    "size": int(metadata.get("size") or 0),
                    "md5_checksum": metadata.get("md5Checksum"),
                    "sha256_checksum": metadata.get("sha256Checksum"),
                }
            )
            self.store.update(
                job_id,
                bytes_total=total,
                bytes_done=completed,
                local_files=local_files,
            )
        self.store.update(
            job_id,
            state="downloaded",
            bytes_total=total,
            bytes_done=total,
            local_files=local_files,
            error=None,
        )
        local_files, verified_total = self._verify_local_files(
            normalized, destination_root
        )
        if verified_total != total:
            raise RuntimeError("total local verificado diverge do manifesto Drive")
        # O trigger usa o mesmo pipeline para os dois alvos. Em target=local
        # nao ha upload, mas classificacao e verificacao devem ser transicoes
        # persistidas separadamente antes do estado terminal.
        self.store.update(job_id, state="classifying", error=None)
        self.store.update(job_id, state="verifying", error=None)
        self.store.update(
            job_id,
            state="completed",
            bytes_total=total,
            bytes_done=total,
            local_files=local_files,
            error=None,
        )
        return local_files

    def run_once(self, target: str | None = None) -> dict[str, Any] | None:
        if target not in {None, "gdrive", "local"}:
            raise ValueError("target de worker invalido")
        job = self.store.claim_upload() if target != "local" else None
        selected_target = "gdrive"
        if job is None and target != "gdrive":
            job = self.store.claim_download()
            selected_target = "local"
        if job is None:
            return None
        try:
            if selected_target == "gdrive":
                self.process_upload_job(job)
                final_state = "completed"
            else:
                self.process_download_job(job)
                final_state = "completed"
            return {"id": job["id"], "target": selected_target, "state": final_state}
        except Exception as exc:
            self.store.update(
                job["id"], state="failed", error=f"{type(exc).__name__}: {exc}"
            )
            LOG.exception("transferencia Google Drive falhou para job %s", job["id"])
            return {"id": job["id"], "target": selected_target, "state": "failed"}

    def run_forever(
        self, stop_event: threading.Event, *, poll_interval: float = 2.0
    ) -> None:
        while not stop_event.is_set():
            if self.run_once() is None:
                stop_event.wait(poll_interval)


class DriveRuntime:
    def __init__(self, settings: Settings) -> None:
        settings.validate_secrets()
        if not DRIVE_ID_RE.fullmatch(settings.gdrive_root_id):
            raise RuntimeError("OFC_GDRIVE_ROOT_ID invalido")
        self.settings = settings
        self.client = DriveClient(settings.gdrive_token_path)
        self.catalog = DriveCatalog(settings, self.client)
        self.transfer_worker = DriveTransferWorker(settings, self.client)
        self.transfer_stop = threading.Event()
        self.sync_lock = threading.Lock()
        self.last_counts: dict[str, int] = {}
        self.last_error: str | None = None
        self.last_success: float | None = None

    def details(self) -> dict[str, Any]:
        with connection(self.settings) as database:
            sessions = database.execute(
                "SELECT count(*) AS n FROM runtime.playback_sessions WHERE site='gdrive' AND closed_at IS NULL"
            ).fetchone()["n"]
        return {
            "provider": "google-drive",
            "root": "#AVideos",
            "sessions": int(sessions),
            "syncing": self.sync_lock.locked(),
            "last_success": self.last_success,
            "last_error": self.last_error,
            "counts": self.last_counts,
        }

    def sync_once(self) -> dict[str, int]:
        if not self.sync_lock.acquire(blocking=False):
            raise RuntimeError("sincronizacao Google Drive ja esta em andamento")
        return self._sync_acquired()

    def _sync_acquired(self) -> dict[str, int]:
        try:
            counts = self.catalog.sync_once()
            self.last_counts = counts
            self.last_error = None
            self.last_success = time.time()
            beat("gdrive-source", "healthy", self.details())
            LOG.info("Google Drive sincronizado: %s", counts)
            return counts
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            beat("gdrive-source", "degraded", {"error": self.last_error})
            raise
        finally:
            self.sync_lock.release()

    def trigger_sync(self) -> bool:
        """Agenda um sync sem manter a requisicao HTTP aberta."""
        if not self.sync_lock.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self._sync_acquired()
            except Exception:
                LOG.exception("sincronizacao Google Drive disparada falhou")

        thread = threading.Thread(target=run, name="gdrive-sync-trigger", daemon=True)
        try:
            thread.start()
        except Exception:
            self.sync_lock.release()
            raise
        return True

    def start_sync_loop(self) -> threading.Thread:
        def run() -> None:
            while True:
                try:
                    self.sync_once()
                except Exception:
                    LOG.exception("sincronizacao Google Drive falhou")
                time.sleep(self.settings.gdrive_sync_interval)

        thread = threading.Thread(target=run, name="gdrive-sync", daemon=True)
        thread.start()
        return thread

    def start_transfer_loop(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.transfer_worker.run_forever,
            args=(self.transfer_stop,),
            name="gdrive-transfer",
            daemon=True,
        )
        thread.start()
        return thread

    def lookup(self, session_id: str) -> dict[str, Any]:
        with connection(self.settings) as database:
            row = database.execute(
                """
                SELECT s.token_hash,d.drive_file_id,d.mime_type,d.can_download,
                       d.active,f.size,f.path
                FROM runtime.playback_sessions s
                JOIN catalog.torrent_files f ON f.id=s.torrent_file_id
                JOIN catalog.drive_files d ON d.torrent_file_id=f.id
                WHERE s.id=%s AND s.site='gdrive' AND s.closed_at IS NULL
                  AND s.created_at >= now()-make_interval(secs=>%s)
                  AND f.is_video AND d.active AND d.can_download
                """,
                (
                    session_id,
                    int(getattr(self.settings, "playback_ttl_seconds", 43200)),
                ),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def create_session(self, session_id: str) -> dict[str, Any]:
        session_id = normalized_session_id(session_id)
        item = self.lookup(session_id)
        metrics = {
            "provider": "gdrive",
            "state": "remote",
            "download_bps": 0,
            "download_bytes_per_second": 0,
            "verified_buffer_bytes": 0,
            "progress_bytes": 0,
            "file_size": int(item["size"]),
            "progress": 0.0,
            "seeds": 0,
            "peers": 0,
        }
        with connection(self.settings) as database:
            database.execute(
                "UPDATE runtime.playback_sessions SET state='buffering',updated_at=now() WHERE id=%s",
                (session_id,),
            )
            database.execute(
                "UPDATE runtime.download_jobs SET state='ready',metrics=%s,updated_at=now() WHERE session_id=%s",
                (Jsonb(metrics), session_id),
            )
            database.commit()
        return {"session_id": session_id, "file_size": item["size"], "relative_path": item["path"]}

    def metrics(self, session_id: str) -> dict[str, Any]:
        session_id = normalized_session_id(session_id)
        with connection(self.settings) as database:
            row = database.execute(
                "SELECT metrics FROM runtime.download_jobs WHERE session_id=%s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row["metrics"] or {})

    def update_metrics(
        self,
        session_id: str,
        *,
        base_bytes: int,
        delivered: int,
        elapsed: float,
        file_size: int,
        state: str,
    ) -> None:
        rate = int(delivered / max(elapsed, 0.001))
        total = min(file_size, base_bytes + delivered)
        metrics = {
            "provider": "gdrive",
            "state": state,
            "download_bps": rate * 8,
            "download_bytes_per_second": rate,
            "verified_buffer_bytes": total,
            "progress_bytes": total,
            "file_size": file_size,
            "progress": total / file_size if file_size else 0.0,
            "seeds": 0,
            "peers": 0,
        }
        with connection(self.settings) as database:
            database.execute(
                """
                UPDATE runtime.download_jobs SET state=%s,metrics=%s,updated_at=now()
                WHERE session_id=%s
                """,
                (state, Jsonb(metrics), session_id),
            )
            database.execute(
                """
                UPDATE runtime.playback_sessions SET download_rate_bps=%s,
                  verified_buffer_bytes=%s,updated_at=now()
                WHERE id=%s AND closed_at IS NULL
                """,
                (rate * 8, total, session_id),
            )
            database.commit()

    def close_session(self, session_id: str) -> None:
        session_id = normalized_session_id(session_id)
        with connection(self.settings) as database:
            database.execute(
                "UPDATE runtime.download_jobs SET state='closed',updated_at=now() WHERE session_id=%s",
                (session_id,),
            )
            database.commit()


def _internal(settings: Settings) -> bool:
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return internal_token_matches(supplied, settings.internal_token)


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    runtime = DriveRuntime(settings)
    runtime.start_sync_loop()
    runtime.start_transfer_loop()
    start_heartbeat("gdrive-source", runtime.details)
    app = Flask(__name__)
    app.config["runtime"] = runtime

    @app.get("/health")
    def health() -> Response:
        return jsonify({"status": "degraded" if runtime.last_error else "ok", **runtime.details()})

    @app.post("/internal/sync")
    def sync() -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        if not runtime.trigger_sync():
            return jsonify({"error": "sincronizacao Google Drive ja esta em andamento"}), 409
        return jsonify({"accepted": True, "status": "syncing"}), 202

    @app.post("/internal/sessions")
    def create_session() -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        payload = request.get_json(silent=True) or {}
        try:
            item = runtime.create_session(str(payload.get("session_id", "")))
        except (KeyError, UnsafeMediaError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(item), 201

    @app.get("/internal/sessions/<session_id>")
    def session_status(session_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            return jsonify(runtime.metrics(session_id))
        except (KeyError, UnsafeMediaError):
            return jsonify({"error": "sessao ausente"}), 404

    @app.delete("/internal/sessions/<session_id>")
    def close_session(session_id: str) -> Response:
        if not _internal(settings):
            return jsonify({"error": "nao autorizado"}), 403
        try:
            runtime.close_session(session_id)
        except UnsafeMediaError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify({"closed": True})

    @app.route("/source/<session_id>/<token>", methods=["GET", "HEAD"])
    def source(session_id: str, token: str) -> Response:
        try:
            session_id = normalized_session_id(session_id)
            item = runtime.lookup(session_id)
            if not token_matches(token, settings.session_pepper, str(item["token_hash"])):
                return jsonify({"error": "token invalido"}), 403
            total = int(item["size"])
            start, end, partial = parse_range(request.headers.get("Range"), total)
        except KeyError:
            return jsonify({"error": "sessao ausente"}), 404
        except (UnsafeMediaError, ValueError):
            return jsonify({"error": "range ou sessao invalida"}), 416
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": (
                str(item["mime_type"])
                if str(item["mime_type"]).startswith("video/")
                else "application/octet-stream"
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        if request.method == "HEAD":
            return Response(status=206 if partial else 200, headers=headers)
        requested_range = f"bytes={start}-{end}" if partial else None
        try:
            upstream = runtime.client.open_media(str(item["drive_file_id"]), requested_range)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
        if partial and upstream.status_code != 206:
            upstream.close()
            return jsonify({"error": "Google Drive ignorou o intervalo solicitado"}), 502
        previous = runtime.metrics(session_id)
        base_bytes = int(previous.get("progress_bytes") or 0)

        def body() -> Iterator[bytes]:
            delivered = 0
            started = time.monotonic()
            last_update = started
            try:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    remaining = length - delivered
                    if remaining <= 0:
                        break
                    selected = chunk[:remaining]
                    delivered += len(selected)
                    yield selected
                    now = time.monotonic()
                    if now - last_update >= 1:
                        runtime.update_metrics(
                            session_id,
                            base_bytes=base_bytes,
                            delivered=delivered,
                            elapsed=now - started,
                            file_size=total,
                            state="streaming",
                        )
                        last_update = now
            finally:
                upstream.close()
                try:
                    runtime.update_metrics(
                        session_id,
                        base_bytes=base_bytes,
                        delivered=delivered,
                        elapsed=time.monotonic() - started,
                        file_size=total,
                        state="ready",
                    )
                except Exception:
                    LOG.exception("nao foi possivel atualizar metricas Drive")

        return Response(body(), status=206 if partial else 200, headers=headers)

    return app


def main() -> None:
    create_app().run(host="0.0.0.0", port=7103, threaded=True)


if __name__ == "__main__":
    main()
