import pytest
from fastapi.testclient import TestClient

from inventory_management_service.main import app


def test_startup_runs_migrations_before_serving(monkeypatch):
    calls = []
    monkeypatch.setattr("inventory_management_service.main.run_migrations", lambda: calls.append("migrated"))
    with TestClient(app) as client:
        assert calls == ["migrated"]
        assert client.get("/docs").status_code == 200


def test_startup_stops_if_migration_fails(monkeypatch):
    def fail():
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr("inventory_management_service.main.run_migrations", fail)
    with pytest.raises(RuntimeError, match="Database unavailable"):
        with TestClient(app):
            pytest.fail("The application must not start after a migration failure")
