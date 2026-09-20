"""共享测试夹具：内存 SQLite + 不连真实 Postgres 的 FastAPI TestClient。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


# JSONB 在 SQLite 上降级为 JSON（模型按 PG 定义，测试库无需 Postgres）。
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, compiler, **kw):  # noqa: ARG001
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    # 让 API 依赖注入复用同一个测试会话；不用 with 触发 lifespan，避免触碰真实 PG 引擎。
    app.dependency_overrides[get_db] = lambda: (yield db)
    yield TestClient(app)
    app.dependency_overrides.clear()
