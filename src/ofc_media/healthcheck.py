from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from .db import close_pool, connection


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
        if updated < datetime.now(UTC) - timedelta(minutes=5):
            raise SystemExit(1)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
