from __future__ import annotations

import json
import secrets
import shutil
import uuid

import requests

from .auth import token_digest
from .config import Settings
from .db import connection
from .safety import safe_owned_path


def main() -> None:
    settings = Settings.from_env()
    settings.validate_secrets()
    session = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    storage = secrets.token_hex(32)
    output = safe_owned_path(settings.hls_root, "cache", storage)
    profile = safe_owned_path(output, "720p")
    marker = f"ofc-gateway-smoke-{session.hex}"
    inserted = False
    try:
        profile.mkdir(parents=True, exist_ok=False)
        safe_owned_path(output, "master.m3u8").write_text(marker, encoding="utf-8")
        safe_owned_path(output, "blocked.torrent").write_text("never serve", encoding="utf-8")
        with connection(settings) as database:
            selected = database.execute(
                """
                SELECT t.site,trim(t.infohash) AS infohash,f.id
                FROM catalog.torrent_files f JOIN catalog.torrents t ON t.id=f.torrent_id
                WHERE f.is_video ORDER BY f.id LIMIT 1
                """
            ).fetchone()
            if selected is None:
                raise RuntimeError("catalogo sem video para teste")
            database.execute(
                """
                INSERT INTO runtime.playback_sessions(
                  id,site,infohash,torrent_file_id,token_hash,state,selected_profile)
                VALUES(%s,%s,%s,%s,%s,'ready','adaptive')
                """,
                (
                    session,
                    selected["site"],
                    selected["infohash"],
                    selected["id"],
                    token_digest(token, settings.session_pepper),
                ),
            )
            database.execute(
                """
                INSERT INTO runtime.stream_artifacts(
                  session_id,storage_key,profile,kind,relative_path,ready)
                VALUES(%s,%s,'master','hls','master.m3u8',TRUE)
                """,
                (session, storage),
            )
            database.commit()
            inserted = True

        base = f"http://gateway:8080/stream/{session.hex}"
        valid = requests.get(
            f"{base}/{token}/{storage}/master.m3u8", timeout=10
        )
        invalid = requests.get(
            f"{base}/{'A' * 43}/{storage}/master.m3u8", timeout=10
        )
        blocked = requests.get(
            f"{base}/{token}/{storage}/blocked.torrent", timeout=10
        )
        result = {
            "authorized_hls": valid.status_code,
            "invalid_token": invalid.status_code,
            "blocked_torrent": blocked.status_code,
            "marker_match": valid.text == marker,
        }
        if valid.status_code != 200 or valid.text != marker:
            raise RuntimeError(f"HLS autorizado falhou: {result}")
        if invalid.status_code not in {401, 403}:
            raise RuntimeError(f"token invalido nao foi bloqueado: {result}")
        if blocked.status_code != 404:
            raise RuntimeError(f"extensao .torrent foi exposta: {result}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if inserted:
            with connection(settings) as database:
                database.execute("DELETE FROM runtime.playback_sessions WHERE id=%s", (session,))
                database.commit()
        if output.exists():
            status = output.lstat()
            if output.is_symlink() or getattr(status, "st_file_attributes", 0) & 0x400:
                raise RuntimeError("diretorio temporario do smoke virou link")
            shutil.rmtree(output)


if __name__ == "__main__":
    main()
