"""端到端验证：并发写同一条进行中 Run → 后到者 409 → 刷新版本 → 带新版本写成功。

同时核对：
- 409 响应为结构化 detail（code/message/current_version），与 422 参数错误形态不同
- 终态再写返回 terminal_state
- 审计员无写入口（403），只读接口可用
"""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import router
from app.database import Base, get_db


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # JSONB not available on SQLite — compile as JSON (same shim as test_state_machine)
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(_type, compiler, **kw):
        return "JSON"

    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


def login(client, username: str, password: str) -> dict:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def start_run(client, headers) -> dict:
    res = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project": "p-conflict",
            "name": "concurrent-run",
            "dataset_content_sha256": sha("dataset"),
            "code_commit_sha": "abc1234",
            "description": None,
            "expected_version": 0,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_concurrent_writers_conflict_then_recover(client):
    headers = login(client, "researcher", "lab123456")
    run = start_run(client, headers)
    run_id = run["id"]
    assert run["version"] == 1

    # 两个“并发”写者各自拿到同一投影快照（version=1）
    snap_a = client.get(f"/api/runs/{run_id}", headers=headers).json()
    snap_b = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert snap_a["version"] == snap_b["version"] == 1

    # 写者 A 先提交：成功，版本推进到 2
    res_a = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "loss", "value": 0.5, "step": 1, "expected_version": snap_a["version"]},
    )
    assert res_a.status_code == 200, res_a.text
    assert res_a.json()["version"] == 2

    # 写者 B 仍带旧版本提交：后到一侧看到 409，且为结构化冲突（区别于参数错误）
    res_b = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": snap_b["version"]},
    )
    assert res_b.status_code == 409, res_b.text
    detail = res_b.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "version_conflict"
    assert detail["current_version"] == 2
    assert detail["message"]

    # 写者 B 自动刷新投影版本号
    refreshed = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert refreshed["version"] == 2

    # 带新版本再提交：成功，两条指标都在
    res_retry = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": refreshed["version"]},
    )
    assert res_retry.status_code == 200, res_retry.text
    body = res_retry.json()
    assert body["version"] == 3
    assert [m["name"] for m in body["metrics_json"]] == ["loss", "acc"]


def test_conflict_shape_differs_from_param_error(client):
    headers = login(client, "researcher", "lab123456")
    run = start_run(client, headers)
    run_id = run["id"]

    # 参数错误（step 为负 / 缺字段）：422，detail 为列表，非冲突结构
    res_bad = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "loss", "value": 0.5, "step": -1, "expected_version": 1},
    )
    assert res_bad.status_code == 422
    assert isinstance(res_bad.json()["detail"], list)

    # 版本冲突：409，detail 为带 code 的对象
    res_conflict = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "loss", "value": 0.5, "step": 1, "expected_version": 99},
    )
    assert res_conflict.status_code == 409
    detail = res_conflict.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "version_conflict"
    assert detail["current_version"] == 1


def test_terminal_state_conflict_code(client):
    headers = login(client, "researcher", "lab123456")
    run = start_run(client, headers)
    run_id = run["id"]

    res_done = client.post(
        f"/api/runs/{run_id}/complete",
        headers=headers,
        json={"result_summary": "done", "expected_version": 1},
    )
    assert res_done.status_code == 200, res_done.text
    version = res_done.json()["version"]

    # 终态后再写：409 terminal_state（即使版本号正确）
    res = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "loss", "value": 0.1, "step": 1, "expected_version": version},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "terminal_state"
    assert res.json()["detail"]["current_version"] == version


def test_auditor_has_no_write_entry(client):
    researcher = login(client, "researcher", "lab123456")
    run = start_run(client, researcher)
    run_id = run["id"]

    auditor = login(client, "auditor", "audit123456")

    # 只读接口可用
    assert client.get(f"/api/runs/{run_id}", headers=auditor).status_code == 200
    assert client.get(f"/api/runs/{run_id}/events", headers=auditor).status_code == 200
    assert client.get(f"/api/runs/{run_id}/lineage", headers=auditor).status_code == 200

    # 所有写入口一律 403
    writes = [
        ("post", "/api/runs", {"project": "p", "name": "n",
                               "dataset_content_sha256": sha("d"), "code_commit_sha": "abc1234",
                               "expected_version": 0}),
        ("post", f"/api/runs/{run_id}/metrics",
         {"name": "m", "value": 1.0, "step": 0, "expected_version": 1}),
        ("post", f"/api/runs/{run_id}/artifacts",
         {"name": "a", "uri": "s3://x", "content_sha256": sha("a"), "expected_version": 1}),
        ("post", f"/api/runs/{run_id}/complete",
         {"result_summary": "s", "expected_version": 1}),
        ("post", f"/api/runs/{run_id}/abort", {"reason": "r", "expected_version": 1}),
    ]
    for method, url, payload in writes:
        res = getattr(client, method)(url, headers=auditor, json=payload)
        assert res.status_code == 403, f"{url} -> {res.status_code}"
