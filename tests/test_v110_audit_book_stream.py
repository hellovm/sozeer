# -*- coding: utf-8 -*-
"""v1.1 #60 回归 ——「全书审计跑全章 + 流式进度」。

============================ 根因 ============================
后端 ``/api/books/{id}/audit-book``（逐章流式 + 熔断 + 落库）早已完整，
但**前端零引用**（建了没人触发）：``setting.ts`` 没有触发方法，
``QualityView`` 没有「全书审计」按钮，更没挂 SSE 看实时进度 —— 用户
根本不知道能跑全书审计，也没法看「进行到第几章 / 是不是断了」。

============================ 修复 ============================
① ``setting.ts`` 补 ``auditBook(bookId)``（POST /audit-book → task_id）。
② ``QualityView``：
   · 「全书审计」触发按钮；
   · 挂 ``useTaskStream``（/tasks/{id}/stream）逐章流式进度 + 完成后自动重拉；
   · 进页若有 running 的 audit_book 任务 → 自动接续（刷新不失忆）。

============================ 测试（源码级） ==================
前端无 vitest，按项目惯例读 .vue / .ts 源码做**可执行代码 token** 断言。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"


def _read(*parts) -> str:
    return (WEB.joinpath(*parts)).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


# ── ① 后端端点必须存在（防「前端接了、后端没了」） ──
def test_backend_audit_book_endpoint_exists():
    src = (ROOT / "apps" / "api" / "main.py").read_text(encoding="utf-8")
    assert '"/api/books/{book_id}/audit-book"' in src, "★ 后端缺 /audit-book 端点"
    # 逐章流式事件（stage/done/action）必须在写进任务
    assert '"kind": "stage"' in src and '"kind": "done"' in src, \
        "全书审计未写流式进度事件（前端无法看实时进度）"


# ── ② setting.ts 必须有触发方法 ──
def test_setting_api_has_audit_book():
    ts = _read("api", "modules", "setting.ts")
    assert "auditBook(" in ts, "★ setting.ts 缺 auditBook 触发方法（建了没人触发）"
    assert "/audit-book" in ts, "auditBook 未指向 /audit-book 端点"
    # 返回 task_id（前端据此挂流）
    assert "task_id" in ts, "auditBook 未声明返回 task_id（无法挂 SSE）"


# ── ③ QualityView 必须接上：按钮 + 流式 + 恢复 ──
def test_qualityview_has_audit_trigger_button():
    code = _code_only(_read("views", "books", "QualityView.vue"))
    # 触发函数被按钮绑定
    assert "startAuditBook" in code, "★ QualityView 缺全书审计触发"
    assert "startAuditBook" in _read("views", "books", "QualityView.vue")


def test_qualityview_calls_audit_book_api():
    code = _code_only(_read("views", "books", "QualityView.vue"))
    assert "settingApi.auditBook(" in code, \
        "★ 未调用 settingApi.auditBook（后端能力没接上前端）"
    # 挂任务流看实时进度
    assert "useTaskStream(" in code, "未挂任务流（看不到逐章进度）"
    assert "taskStreamUrl(" in code, "未用 taskStreamUrl 定位 SSE 流地址"


def test_qualityview_streams_progress_and_refreshes():
    code = _code_only(_read("views", "books", "QualityView.vue"))
    # 逐章进度事件被消费（stage 文案 / action type=audit）
    assert "kind === 'stage'" in code, "未消费 stage 进度事件"
    assert "audit" in code, "未处理 type=audit 的逐章完成事件"
    # 完成后自动重拉质检概览（逐章分已落库）
    assert "onAuditStreamDone" in code, "★ 缺流结束收口（完成不刷新概览）"
    assert "auditStream.error" in code, "流结束未判断 error 态"


def test_qualityview_resumes_running_audit_on_mount():
    """★ 任务持久化哲学：刷新/进页时若有 running 的 audit_book 任务要自动接续。"""
    code = _code_only(_read("views", "books", "QualityView.vue"))
    assert "resumeRunningAudit" in code, "★ 缺进页恢复（刷新即失忆）"
    # 恢复靠 tasks 列表找 running 的 audit_book 任务
    assert "bookApi.tasks(" in code, "恢复未拉任务列表定位 running 任务"
    assert "'audit_book'" in code, "恢复未按 kind=audit_book 过滤任务"
