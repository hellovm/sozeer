# -*- coding: utf-8 -*-
"""v1.4.2 #14：MCP Server（确定性/只读/无副作用能力暴露）。

覆盖：
  1. 4 个工具纯函数（run_diagnose/run_gates_tool/run_list_genres/run_style_hints）
     —— 确定性、返回合法 JSON、无 DB 副作用（血泪："写了这行≠能跑"，真 import 验证）。
  2. /api/mcp/info 端点 fail-open —— mcp 未安装时返回 available=false + 安装提示，
     **绝不 500**（与 redis/python-docx 同款"可选依赖优雅降级"）。
  3. 工具清单与 main.py 暴露的 ops 数一致（EXPECTED_OPS=398 已同步）。

★ 设计红线（测试也钉死）：MCP 工具**只读、无副作用**——
  调完 4 个纯函数，DB 里不该多出任何章节/台账行。
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest


# ============================================================ 工具纯函数
def test_run_diagnose_returns_valid_json():
    """diagnose_chapter：纯规则引擎，返回含 findings/stats/count 的 JSON。"""
    from apps.api import mcp_server as m
    out = json.loads(m.run_diagnose("这一幕，他感到十分愤怒，空气中弥漫着紧张。欲知后事如何？"))
    assert "count" in out and "findings" in out and "stats" in out
    # 命中至少一条（AI 味 / 章末钩子 套话）
    assert out["count"] >= 1


def test_run_diagnose_deterministic():
    """确定性：同输入同输出（MCP 工具红线——无随机、无时间戳）。"""
    from apps.api import mcp_server as m
    text = "夜色中，他感到十分不安。"
    assert m.run_diagnose(text) == m.run_diagnose(text)


def test_run_gates_tool_returns_valid_json():
    """check_quality_gates：返回 score(0-100)/issues/stats；字数不卡（chapter_min=0）。"""
    from apps.api import mcp_server as m
    out = json.loads(m.run_gates_tool("这一幕。"))
    assert 0 <= out["score"] <= 100
    assert isinstance(out["issues"], list)
    # issue 字段是 GateIssue 的 rule/level/message（不是猜的 dim/severity）
    if out["issues"]:
        assert set(out["issues"][0].keys()) >= {"rule", "level", "message"}


def test_run_list_genres_returns_catalog():
    """list_genres：返回题材包清单（含 xianxia）。"""
    from apps.api import mcp_server as m
    cats = json.loads(m.run_list_genres())
    assert isinstance(cats, list) and len(cats) >= 1
    keys = [c.get("key") for c in cats]
    assert "xianxia" in keys


def test_run_style_hints_returns_list():
    """get_genre_style_hints：返回字符串列表。"""
    from apps.api import mcp_server as m
    hints = json.loads(m.run_style_hints("xianxia"))
    assert isinstance(hints, list)


# ============================================================ 只读无副作用（红线）
def test_mcp_tools_are_read_only(tmp_path, monkeypatch):
    """调 4 个工具后 DB 不新增章节/台账（MCP 只读红线）。"""
    # 隔离数据目录
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path / "data"))
    from apps.api.db import Base, engine
    from apps.api.models import Chapter, LLMCall, MediaCall
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from apps.api.db import SessionLocal
    from apps.api import mcp_server as m
    # 调一遍 4 个工具
    m.run_diagnose("测试正文")
    m.run_gates_tool("测试正文")
    m.run_list_genres()
    m.run_style_hints("xianxia")

    from sqlalchemy import select as _sel
    db = SessionLocal()
    try:
        # 工具是纯计算，不该落任何 DB 行
        assert db.scalar(_sel(Chapter).limit(1)) is None
        assert db.scalar(_sel(LLMCall).limit(1)) is None
        assert db.scalar(_sel(MediaCall).limit(1)) is None
    finally:
        db.close()


# ============================================================ /api/mcp/info fail-open
def test_mcp_info_endpoint_fail_open(monkeypatch):
    """mcp 未安装时 /api/mcp/info 仍返回 200 + available=false + 安装提示，不 500。"""
    from apps.api import mcp_server as m
    from apps.api.routers import system as sys_router
    fake_user = type("U", (), {"id": "u_x"})()
    # 模拟 mcp 没装（fail-open 路径）
    monkeypatch.setattr(m, "_MCP_AVAILABLE", False)
    res = sys_router.mcp_info(fake_user)
    assert res["available"] is False
    assert "install_hint" in res and "pip install" in res["install_hint"]
    assert "diagnose_chapter" in res["tools"]


def test_mcp_info_endpoint_available(monkeypatch):
    """mcp 已安装时 /api/mcp/info 返回 available=true。"""
    from apps.api import mcp_server as m
    from apps.api.routers import system as sys_router
    fake_user = type("U", (), {"id": "u_x"})()
    monkeypatch.setattr(m, "_MCP_AVAILABLE", True)
    res = sys_router.mcp_info(fake_user)
    assert res["available"] is True
    assert "stdio" in res["transports"] and "streamable-http" in res["transports"]


def test_tool_names_match_expected():
    """工具清单与 /api/mcp/info 暴露一致（防"模板建了没人触发"）。"""
    from apps.api import mcp_server as m
    assert m.list_tool_names() == [
        "diagnose_chapter", "check_quality_gates", "list_genres", "get_genre_style_hints",
    ]
