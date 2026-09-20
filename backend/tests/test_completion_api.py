import hashlib
import os
import tempfile

import pytest

# Point the app at a throwaway SQLite file before app modules import settings.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base, SessionLocal, engine, get_db
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, compiler, **kw):
    return "JSON"


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.fixture()
def researcher(client):
    return _token(client, "researcher", "lab123456")


@pytest.fixture()
def auditor(client):
    return _token(client, "auditor", "audit123456")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _create_run(client, token):
    r = client.post(
        "/api/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project": "p1",
            "name": "n1",
            "dataset_content_sha256": _sha("ds"),
            "code_commit_sha": "abc1234",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_checks_visible_to_researcher_and_auditor(client, researcher, auditor):
    run = _create_run(client, researcher)
    for token in (researcher, auditor):
        r = client.get(
            f"/api/runs/{run['id']}/completion-checks",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert [c["key"] for c in data["checks"]] == [
            "has_metric",
            "has_artifact",
            "has_provenance_ids",
        ]
        assert data["all_passed"] is False
        assert "已有至少一条指标" in data["gaps"]
        assert "已挂载至少一件产物" in data["gaps"]


def test_complete_rejected_until_metrics_and_artifacts_added(client, researcher):
    run = _create_run(client, researcher)
    headers = {"Authorization": f"Bearer {researcher}"}

    r = client.post(
        f"/api/runs/{run['id']}/complete",
        headers=headers,
        json={"result_summary": "done", "expected_version": run["version"]},
    )
    assert r.status_code == 422
    assert "指标" in r.json()["detail"] and "产物" in r.json()["detail"]

    # add only a metric -> still blocked
    r = client.post(
        f"/api/runs/{run['id']}/metrics",
        headers=headers,
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/runs/{run['id']}/complete",
        headers=headers,
        json={"result_summary": "done", "expected_version": 2},
    )
    assert r.status_code == 422
    assert "产物" in r.json()["detail"]

    # add the artifact -> gate opens, completion really happens
    r = client.post(
        f"/api/runs/{run['id']}/artifacts",
        headers=headers,
        json={
            "name": "model.bin",
            "uri": "s3://lab/model.bin",
            "content_sha256": _sha("model"),
            "expected_version": 2,
        },
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/runs/{run['id']}/complete",
        headers=headers,
        json={"result_summary": "done", "expected_version": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"

    r = client.get(
        f"/api/runs/{run['id']}/completion-checks",
        headers=headers,
    )
    assert r.json()["all_passed"] is True


def test_auditor_cannot_complete_but_can_see_checks(client, researcher, auditor):
    run = _create_run(client, researcher)
    r = client.post(
        f"/api/runs/{run['id']}/complete",
        headers={"Authorization": f"Bearer {auditor}"},
        json={"result_summary": "done", "expected_version": run["version"]},
    )
    assert r.status_code == 403

    r = client.get(
        f"/api/runs/{run['id']}/completion-checks",
        headers={"Authorization": f"Bearer {auditor}"},
    )
    assert r.status_code == 200
