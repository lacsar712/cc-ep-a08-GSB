"""收尾协议检查（preconditions）测试。

验收场景：
- 缺指标时无法完成；缺产物时无法完成；
- 补齐指标与产物后可以通过并真正完成；
- API 层返回 422 与逐项缺口；审计员只能查看检查结果、完成接口 403。
"""

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.checklist import PreconditionError, evaluate_preconditions, preconditions_satisfied
from app.cqrs import attach_artifact, complete_run, record_metric, start_run
from app.database import Base
from app.main import app
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, compiler, **kw):
    return "JSON"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _start(db) -> object:
    return start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds"),
        code_commit_sha="abc1234",
        description=None,
    )


def test_fresh_run_all_three_checks_visible(db):
    run = _start(db)
    checks = evaluate_preconditions(run)
    assert [c["key"] for c in checks] == ["metric", "artifact", "provenance"]
    assert [c["passed"] for c in checks] == [False, False, True]
    assert preconditions_satisfied(run) is False


def test_complete_blocked_without_metric(db):
    run = _start(db)
    run = attach_artifact(
        db,
        run_id=run.id,
        actor="researcher",
        name="model.bin",
        uri="s3://x/model.bin",
        content_sha256=sha("m"),
        media_type=None,
        expected_version=run.version,
    )
    with pytest.raises(PreconditionError) as exc_info:
        complete_run(
            db,
            run_id=run.id,
            actor="researcher",
            result_summary="done",
            expected_version=run.version,
        )
    failed_keys = {c["key"] for c in exc_info.value.checks if not c["passed"]}
    assert failed_keys == {"metric"}
    # 仍在 running，未写入完成事件
    db.expire_all()
    assert db.get(type(run), run.id).status == "running"


def test_complete_blocked_without_artifact(db):
    run = _start(db)
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=run.version,
    )
    with pytest.raises(PreconditionError) as exc_info:
        complete_run(
            db,
            run_id=run.id,
            actor="researcher",
            result_summary="done",
            expected_version=run.version,
        )
    failed_keys = {c["key"] for c in exc_info.value.checks if not c["passed"]}
    assert failed_keys == {"artifact"}


def test_complete_allowed_after_metric_and_artifact(db):
    run = _start(db)
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=run.version,
    )
    run = attach_artifact(
        db,
        run_id=run.id,
        actor="researcher",
        name="model.bin",
        uri="s3://x/model.bin",
        content_sha256=sha("m"),
        media_type=None,
        expected_version=run.version,
    )
    assert preconditions_satisfied(run) is True
    run = complete_run(
        db,
        run_id=run.id,
        actor="researcher",
        result_summary="done",
        expected_version=run.version,
    )
    assert run.status == "completed"


# -------------------- API 层 --------------------


@pytest.fixture()
def client_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides = {}
    from app.database import get_db

    app.dependency_overrides[get_db] = override_get_db
    yield TestSession
    app.dependency_overrides = {}


@pytest.fixture()
def client(client_db):
    return TestClient(app)


def _token(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _seed_running_run(session: Session) -> str:
    run = start_run(
        session,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds-api"),
        code_commit_sha="abc1234",
        description=None,
    )
    return str(run.id)


def test_api_run_out_includes_preconditions(client, client_db):
    with client_db() as session:
        run_id = _seed_running_run(session)

    token = _token(client, "researcher", "lab123456")
    resp = client.get(f"/api/runs/{run_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    checks = resp.json()["preconditions"]
    assert {c["key"] for c in checks} == {"metric", "artifact", "provenance"}
    by_key = {c["key"]: c["passed"] for c in checks}
    assert by_key == {"metric": False, "artifact": False, "provenance": True}


def test_api_complete_422_with_missing_items(client, client_db):
    with client_db() as session:
        run_id = _seed_running_run(session)

    token = _token(client, "researcher", "lab123456")
    resp = client.post(
        f"/api/runs/{run_id}/complete",
        json={"result_summary": "done", "expected_version": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "checks" in detail and "message" in detail
    failed = {c["key"] for c in detail["checks"] if not c["passed"]}
    assert failed == {"metric", "artifact"}

    # run 仍是 running
    get_resp = client.get(f"/api/runs/{run_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.json()["status"] == "running"


def test_api_acceptance_fill_then_complete(client, client_db):
    """验收：缺指标时无法完成，补齐指标与产物后可以通过并真正完成。"""
    with client_db() as session:
        run_id = _seed_running_run(session)

    token = _token(client, "researcher", "lab123456")
    headers = {"Authorization": f"Bearer {token}"}

    # 初始：缺指标与产物 -> 422
    blocked = client.post(
        f"/api/runs/{run_id}/complete",
        json={"result_summary": "done", "expected_version": 1},
        headers=headers,
    )
    assert blocked.status_code == 422

    # 补指标
    m = client.post(
        f"/api/runs/{run_id}/metrics",
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
        headers=headers,
    )
    assert m.status_code == 200

    # 仍缺产物 -> 422
    blocked2 = client.post(
        f"/api/runs/{run_id}/complete",
        json={"result_summary": "done", "expected_version": 2},
        headers=headers,
    )
    assert blocked2.status_code == 422
    failed = {c["key"] for c in blocked2.json()["detail"]["checks"] if not c["passed"]}
    assert failed == {"artifact"}

    # 补产物
    a = client.post(
        f"/api/runs/{run_id}/artifacts",
        json={
            "name": "model.bin",
            "uri": "s3://x/model.bin",
            "content_sha256": sha("m"),
            "media_type": None,
            "expected_version": 2,
        },
        headers=headers,
    )
    assert a.status_code == 200

    # 协议全部通过 -> 真正完成
    ok = client.post(
        f"/api/runs/{run_id}/complete",
        json={"result_summary": "done", "expected_version": 3},
        headers=headers,
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "completed"
    assert all(c["passed"] for c in ok.json()["preconditions"])


def test_api_preconditions_readable_by_auditor_but_complete_forbidden(client, client_db):
    with client_db() as session:
        run_id = _seed_running_run(session)

    auditor = _token(client, "auditor", "audit123456")
    auditor_headers = {"Authorization": f"Bearer {auditor}"}

    # 审计员可只读查看检查结果
    resp = client.get(
        f"/api/runs/{run_id}/preconditions", headers=auditor_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_passed"] is False
    assert len(body["checks"]) == 3

    # 审计员不能代为完成 -> 403
    forbidden = client.post(
        f"/api/runs/{run_id}/complete",
        json={"result_summary": "done", "expected_version": 1},
        headers=auditor_headers,
    )
    assert forbidden.status_code == 403


def test_api_preconditions_endpoint_researcher_visible(client, client_db):
    with client_db() as session:
        run_id = _seed_running_run(session)

    token = _token(client, "researcher", "lab123456")
    resp = client.get(
        f"/api/runs/{run_id}/preconditions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["run_id"] == run_id
