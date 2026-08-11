from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import ofc_media.healthcheck as healthcheck


class FakeDatabase:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def execute(self, *_args: Any, **_kwargs: Any) -> FakeDatabase:
        return self

    def fetchone(self) -> dict[str, Any]:
        return self.row


def run_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    age: timedelta,
) -> None:
    row = {
        "status": "healthy",
        "updated_at": datetime.now(UTC) - age,
    }

    @contextmanager
    def fake_connection():
        yield FakeDatabase(row)

    monkeypatch.setattr(healthcheck, "connection", fake_connection)
    monkeypatch.setattr(healthcheck, "close_pool", lambda: None)
    monkeypatch.setattr(healthcheck.sys, "argv", ["healthcheck", service])
    healthcheck.main()


def test_sync_heartbeat_window_uses_configured_interval_plus_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFC_SYNC_INTERVAL", "1200")

    assert healthcheck._heartbeat_max_age("sync") == timedelta(minutes=25)


def test_non_sync_heartbeat_window_remains_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFC_SYNC_INTERVAL", "1200")

    assert healthcheck._heartbeat_max_age("control") == timedelta(minutes=5)


def test_sync_heartbeat_survives_default_ten_minute_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OFC_SYNC_INTERVAL", raising=False)

    run_healthcheck(monkeypatch, "sync", timedelta(minutes=11))


def test_sync_heartbeat_expires_after_interval_and_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OFC_SYNC_INTERVAL", raising=False)

    with pytest.raises(SystemExit) as error:
        run_healthcheck(monkeypatch, "sync", timedelta(minutes=16))

    assert error.value.code == 1


def test_non_sync_heartbeat_still_expires_after_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFC_SYNC_INTERVAL", "1200")

    with pytest.raises(SystemExit) as error:
        run_healthcheck(monkeypatch, "control", timedelta(minutes=6))

    assert error.value.code == 1
