from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from .db import close_pool, connection


DEFAULT_HEARTBEAT_MAX_AGE = timedelta(minutes=5)
SYNC_HEARTBEAT_GRACE = timedelta(minutes=5)


def _heartbeat_max_age(service: str) -> timedelta:
    if service != "sync":
        return DEFAULT_HEARTBEAT_MAX_AGE

    sync_interval = max(int(os.environ.get("OFC_SYNC_INTERVAL", "600")), 30)
    return timedelta(seconds=sync_interval) + SYNC_HEARTBEAT_GRACE


def main() -> None:
    try:
        service = sys.argv[1] if len(sys.argv) > 1 else ""
        with connection() as database:
            row = database.execute(
                "SELECT status,updated_at FROM ops.service_heartbeats WHERE service=%s",
                (service,),
            ).fetchone()
        if not row or row["status"] != "healthy":
            raise SystemExit(1)
        updated = row["updated_at"]
        if updated < datetime.now(UTC) - _heartbeat_max_age(service):
            raise SystemExit(1)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
