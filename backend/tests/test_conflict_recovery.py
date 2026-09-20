"""核对：详情页写入遇版本冲突可感知、可恢复。

覆盖扩展点：
1. 记指标 / 挂产物 / 完成 / 中止 返回冲突时，响应须与普通参数错误（422）区分；
2. 自动刷新投影版本号后，研究员可带新版本再提交成功；
3. 审计员仍无写入口（403）。
"""

from __future__ import annotations

import hashlib

import pytest

DATASET_SHA = hashlib.sha256(b"dataset").hexdigest()
ARTIFACT_SHA = hashlib.sha256(b"artifact").hexdigest()

RESEARCHER = ("researcher", "lab123456")
AUDITOR = ("auditor", "audit123456")


def _login(client, creds) -> dict:
    r = client.post("/api/auth/login", json={"username": creds[0], "password": creds[1]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _start_run(client, headers) -> str:
    r = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project": "p1",
            "name": "并发冲突核对",
            "dataset_content_sha256": DATASET_SHA,
            "code_commit_sha": "abc1234",
            "description": None,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _metric_body(expected_version: int, *, step: int = 1) -> dict:
    return {"name": "loss", "value": 0.5, "step": step, "expected_version": expected_version}


# 四类写命令：(用例名, 路径, 基于给定版本构造请求体)
COMMAND_CASES = [
    ("metric", "/metrics", lambda v: _metric_body(v)),
    (
        "artifact",
        "/artifacts",
        lambda v: {
            "name": "checkpoint.pt",
            "uri": "s3://lab-artifacts/c.pt",
            "content_sha256": ARTIFACT_SHA,
            "media_type": None,
            "expected_version": v,
        },
    ),
    ("complete", "/complete", lambda v: {"result_summary": "done", "expected_version": v}),
    ("abort", "/abort", lambda v: {"reason": "OOM", "expected_version": v}),
]


def test_concurrent_writes_late_side_conflicts_then_refresh_and_succeeds(client):
    """核对主场景：两次并发写同一条 running Run。

    先到一侧基于 v1 写成功（v1→v2）；后到一侧仍持 v1，先看到 409 version_conflict；
    刷新投影拿到 v2 后带新版本重试，写成功（v2→v3）。
    """
    headers = _login(client, RESEARCHER)
    run_id = _start_run(client, headers)

    # 先到一侧：基于打开详情页时看到的 v1 提交
    first = client.post(f"/api/runs/{run_id}/metrics", headers=headers, json=_metric_body(1))
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    # 后到一侧：命令基于同样的 v1，先看到冲突（而非被当作普通参数错误）
    late = client.post(
        f"/api/runs/{run_id}/metrics", headers=headers, json=_metric_body(1, step=2)
    )
    assert late.status_code == 409, late.text
    detail = late.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "version_conflict"
    assert detail["expected_version"] == 1
    assert detail["current_version"] == 2
    # 冲突写入必须被拒绝：只有先到的一条指标
    assert len(client.get(f"/api/runs/{run_id}", headers=headers).json()["metrics_json"]) == 1

    # 自动刷新投影版本号
    refreshed = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert refreshed["version"] == 2

    # 带新版本 v2 再提交 → 成功
    retry = client.post(
        f"/api/runs/{run_id}/metrics", headers=headers, json=_metric_body(2, step=2)
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == 3
    assert len(retry.json()["metrics_json"]) == 2


@pytest.mark.parametrize("kind,path,build", COMMAND_CASES, ids=[c[0] for c in COMMAND_CASES])
def test_every_command_returns_structured_conflict_and_is_recoverable(
    client, kind, path, build
):
    """扩展点 1+2：记指标/挂产物/完成/中止 的冲突都返回结构化 409，且刷新后可恢复。"""
    headers = _login(client, RESEARCHER)
    run_id = _start_run(client, headers)

    # 先用一条指标把版本从 v1 推进到 v2，制造过期视图
    bumped = client.post(
        f"/api/runs/{run_id}/metrics", headers=headers, json=_metric_body(1)
    )
    assert bumped.status_code == 200, bumped.text

    # 后到一侧仍持 v1：四类命令都必须是 409 + version_conflict，而非 400/422
    stale = client.post(f"/api/runs/{run_id}{path}", headers=headers, json=build(1))
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "version_conflict"
    assert detail["current_version"] == 2
    assert detail["expected_version"] == 1
    assert "message" in detail and detail["message"]

    # 刷新投影后带新版本 v2 重试 → 成功，版本推进到 v3
    current = client.get(f"/api/runs/{run_id}", headers=headers).json()["version"]
    assert current == 2
    retry = client.post(f"/api/runs/{run_id}{path}", headers=headers, json=build(current))
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == 3


def test_conflict_is_distinct_from_param_validation_error(client):
    """扩展点 1：普通参数错误走 422，状态码与响应形态都不同于 409 冲突。"""
    headers = _login(client, RESEARCHER)
    run_id = _start_run(client, headers)

    bad = client.post(
        f"/api/runs/{run_id}/metrics",
        headers=headers,
        json={"name": "", "value": 0.5, "step": 0, "expected_version": 1},
    )
    assert bad.status_code == 422
    assert bad.status_code != 409


def test_terminal_state_conflict_is_distinct_code(client):
    """Run 在他处被结束后，命令返回 terminal_state，刷新版本也不可恢复。"""
    headers = _login(client, RESEARCHER)
    run_id = _start_run(client, headers)

    done = client.post(
        f"/api/runs/{run_id}/complete",
        headers=headers,
        json={"result_summary": "done", "expected_version": 1},
    )
    assert done.status_code == 200, done.text

    late = client.post(
        f"/api/runs/{run_id}/metrics", headers=headers, json=_metric_body(2, step=1)
    )
    assert late.status_code == 409
    detail = late.json()["detail"]
    assert detail["code"] == "terminal_state"
    assert detail["current_version"] == 2


def test_auditor_has_no_write_entrypoint(client):
    """扩展点 3：审计员对四类写命令一律 403，无任何写入口。"""
    researcher = _login(client, RESEARCHER)
    run_id = _start_run(client, researcher)
    auditor = _login(client, AUDITOR)

    for _kind, path, build in COMMAND_CASES:
        r = client.post(f"/api/runs/{run_id}{path}", headers=auditor, json=build(1))
        assert r.status_code == 403, (path, r.status_code, r.text)
