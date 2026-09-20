# 科学实验溯源工作台（Experiment Provenance Workbench）

CQRS + Event Sourcing 全栈示例：命令追加 `event_store`，查询走投影表；Vue 前端查看 Run、事件时间线与血缘。

## How to Run

```bash
cd projects/03-experiment-provenance
docker compose up --build
```

> 镜像默认走 `docker.m.daocloud.io`（便于国内拉取）；前端 npm 使用 `npmmirror`。若你可直连 Docker Hub，可将 Dockerfile / compose 中的镜像前缀改回官方名。

首次启动会：

1. 拉起 PostgreSQL
2. 启动 FastAPI 后端并建表
3. `seed` 写入 2 条已完成 Run + 1 条进行中 Run
4. 构建并启动前端（nginx）

停止：

```bash
docker compose down
```

本地后端测试（可选，需 Python 3.11+）：

```bash
cd backend
pip install -r requirements.txt
pytest -q
```

## Services / 端口

| 服务 | 地址 |
|------|------|
| Frontend | http://localhost:3173 |
| Backend API | http://localhost:8173 |
| PostgreSQL | localhost:54373 |

容器内：

- `db`：Postgres `provenance/provenance`，库名 `provenance`
- `backend`：Uvicorn `:8000`
- `seed`：一次性灌数后退出
- `frontend`：nginx `:80`，`/api` 反代到 backend

## 账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| researcher | lab123456 | 可发命令（Start/Metric/Artifact/Complete/Abort） |
| auditor | audit123456 | 只读事件与投影 |

## Verification

1. 打开 http://localhost:3173 ，使用 `researcher` / `lab123456` 登录
2. 在 Run 列表看到 seed 数据（含进行中与已完成）
3. 点击「新建 Run」，填写 project/name、dataset sha、code commit，启动
4. 在详情页记录指标、挂载产物，再 Complete（或 Abort）
5. 打开「事件时间线」确认 version 递增的原始事件
6. 打开「血缘」确认 code_commit、dataset 指纹、artifacts、metrics
7. 健康检查：`GET http://localhost:8173/api/health`
8. 用 `auditor` 登录：可看列表/事件/血缘，命令按钮不可用
9. 并发冲突恢复：两个窗口打开同一条进行中 Run 的详情页，先后提交写命令 —— 后到者看到黄色「版本冲突」提示（区别于红色参数错误），投影版本号已自动刷新，表单保留，直接再次提交即成功

终态或 `expected_version` 不匹配时，API 返回 **409**，且 `detail` 为结构化对象 `{code, message, current_version}`（`code` ∈ `version_conflict` / `terminal_state` / `already_exists`），与参数错误（422，`detail` 为列表）形态不同；详情页凭此区分冲突与参数错误，并自动刷新投影版本号供重试。

## 架构要点

- **命令**：`StartRun` / `RecordMetric` / `AttachArtifact` / `CompleteRun` / `AbortRun`
- **事件**：`RunStarted` / `MetricRecorded` / `ArtifactAttached` / `RunCompleted` / `RunAborted`
- **event_store**：`(aggregate_id, version)` 唯一；冲突 → 409
- **run_projections**：查询侧投影（状态、指标、产物等）
