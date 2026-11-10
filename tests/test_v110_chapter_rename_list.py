# -*- coding: utf-8 -*-
"""v1.1 #305 回归 ——「改章节标题后清单不更新」。

============================ 根因 ============================
后端 #310 的 rename_chapter 工具早就会把新标题写进库，并 emit 一条
``kind=action, type=rename`` 的 SSE 事件。但 **StudioView.onStreamEvent**
只处理 token/delta/text、error、chapter_draft 三类，对 action 事件**整体
忽略**——所以流式过程中 agent 改完章节标题，左栏章节树（chapters）一直
显示旧名，直到整个任务结束（onDone → refreshAfterTask 全量重拉）才更新。

跨页导航（章节列表/大纲页）因 AppLayout 无 <keep-alive> + `:key=route.path`
会重挂重拉，能自愈；**唯一不自愈的就是「写作台流式进行中」这个活体视图**。

============================ 修复 ============================
① 后端 rename 事件带上 ``title`` 字段（新的章节标题）。
② StudioView.onStreamEvent 处理 action 事件：
   · type=rename   → 用事件里的 title 在 chapters 里**原地**更新（不闪骨架屏）；
   · type=character/world/outline/foreshadow → 去抖重拉右栏设定；
   · type=meta     → 重拉顶部书名。

============================ 测试（源码级） ===================
前端无 vitest（web2 只跑 vue-tsc + Python 源码断言），故按项目惯例
读 .vue 源码做**可执行代码 token** 断言（不断言注释，防"注释当实现"误判）。
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"


def _read(*parts) -> str:
    return (WEB.joinpath(*parts)).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """★ 去掉整行注释，只留可执行代码——防止"注释里写了却没实现"被误判通过。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        # 去掉行首/行内整块注释（保留字符串内的 //，本文件里不会出现）
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_studioview_exists():
    assert (WEB / "views" / "books" / "StudioView.vue").is_file()


def test_studioview_handles_action_events():
    """★ #305 核心：onStreamEvent 必须处理 kind=action 事件（旧实现整体忽略）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "kind === 'action'" in code, \
        "★ StudioView.onStreamEvent 仍不处理 action 事件 → 改章节标题清单不更新"
    # action 处理里要区分各 type（至少 rename 被识别）
    assert "type === 'rename'" in code, "未识别 rename 类型的 action 事件"


def test_studioview_rename_updates_chapter_inplace():
    """rename：从事件读 ch_idx + title，在 chapters 里原地更新（不闪骨架屏）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    # 读事件字段
    assert "Number(ev.ch_idx)" in code, "rename 处理未读取事件里的 ch_idx"
    assert "ev.title" in code, "rename 处理未读取事件里的新 title"
    # 原地更新章节树（对命中项写 title，而非整表重拉）
    assert "chapters.value.find(" in code, "未对 chapters 按 idx 定位以原地刷新"
    assert "hit.title =" in code, "★ 命中项未原地写新 title（旧实现靠 onDone 兜底）"
    # 兜底：该章不在列表 / 事件未带 title → 全量重拉
    assert "loadChapters()" in code, "rename 缺少 loadChapters 兜底重拉"


def test_studioview_state_actions_debounced_refresh():
    """设定类 action（人物/世界观/大纲/伏笔）→ 去抖重拉右栏设定。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    for t in ("character", "character_delete", "world", "outline", "foreshadow"):
        assert f"'{t}'" in code, f"action 处理未覆盖设定类 type：{t}"
    assert "scheduleStateRefresh()" in code, "★ 缺少设定栏去抖刷新"


def test_studioview_has_debounce_helper():
    """scheduleStateRefresh 必须真用定时器去抖（合并一次任务里连发的多条 action）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "clearTimeout(stateRefreshTimer)" in code, "去抖未先清旧定时器"
    assert "window.setTimeout" in code, "去抖未用 setTimeout"
    assert "void loadState()" in code, "去抖回调未触发 loadState 重拉设定"


def test_studioview_meta_refreshes_book_title():
    """meta（立项信息）action → 重拉顶部书名。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "loadBook()" in code, "meta 处理未重拉书名"


# ---------------------------------------------------------------- 后端契约
def test_backend_rename_event_carries_title():
    """★ 前后端契约：rename 事件必须带 title 字段（前端原地刷新依赖它）。

    源码级断言 agent.py 的 rename emit 带 title；行为级断言由
    test_v190_stability.py::test_agent_rename_chapter_event_carries_new_title
    提供（跑工具真发事件）。
    """
    api = ROOT / "apps" / "api" / "agent.py"
    code = api.read_text(encoding="utf-8")
    # rename 事件的 emit 里必须带 "title"
    import re
    m = re.search(r'type":\s*"rename"[^}]*', code, re.S)
    assert m, "★ agent.py 未找到 rename 事件 emit"
    assert '"title"' in m.group(0), \
        "rename 事件未带 title 字段（前端无法原地刷新章节树）"
