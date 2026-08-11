from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Callable

from psycopg.types.json import Jsonb

from .db import connection


def beat(service: str, status: str = "healthy", details: dict[str, Any] | None = None) -> None:
    instance = os.environ.get("HOSTNAME", socket.gethostname())
    with connection() as database:
        database.execute(
            """
            INSERT INTO ops.service_heartbeats(service,instance_id,status,details,updated_at)
            VALUES(%s,%s,%s,%s,now())
            ON CONFLICT(service) DO UPDATE SET
              instance_id=excluded.instance_id,status=excluded.status,
              details=excluded.details,updated_at=now()
            """,
            (service, instance, status, Jsonb(details or {})),
        )
        database.commit()


def start_heartbeat(
    service: str,
    details_provider: Callable[[], dict[str, Any]] | None = None,
    interval: int = 15,
) -> threading.Thread:
    def run() -> None:
        while True:
            try:
                details = details_provider() if details_provider else {}
                beat(service, "healthy", details)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=run, name=f"heartbeat-{service}", daemon=True)
    thread.start()
    return thread
