# -*- coding: utf-8 -*-
"""v0.14.10 回归：刷新后对话格式丢失 + 草稿卡回不来。

两个 P0 bug 实录（用户截图对比）：
  Bug 1 刷新后此前对话的 Markdown 格式与样式全部丢失（** 裸露、气泡变样）。
        根因：角色名三重不一致——后端存 'assistant'，前端判断 role === 'agent'，
        CSS 选择器也是 .msg.agent。刷新回放走 else → textContent 纯文本。
  Bug 2 系统答「已起草第 N 章」，但正文里看不到草稿。
        根因：草稿卡只活在实时流 DOM；renderMessages（历史回放）从不重建；
        待审区埋在"正文"tab，对话区无入口。

修复：
  Fix1 addMsg 归一化角色（normRole），CSS 补 .msg.assistant 别名（纵深防御）。
  Fix2 Message 增 task_id 列 ← 落库时记下产出草稿的任务；GET messages 回填
       draft 字段；renderMessages 据 draft 重建卡片；对话区加常驻提示条。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "apps" / "web" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
MAIN_PY = (ROOT / "apps" / "api" / "main.py").read_text(encoding="utf-8")
MODELS_PY = (ROOT / "apps" / "api" / "models.py").read_text(encoding="utf-8")


# ==================================================== Bug 1：角色归一化
def test_norm_role_defined():
    assert "function normRole(" in APP_JS, "必须存在角色归一化函数"


def test_norm_role_maps_assistant_to_agent():
    """核心断言：'assistant' 与 'agent' 必须归一为同一分支，否则刷新必挂。"""
    i = APP_JS.index("function normRole(")
    seg = APP_JS[i:i + 600]
    assert "'agent'" in seg and "'assistant'" in seg, "两种写法都要认"
    # 两者必须返回同一个值（否则 CSS/渲染仍会分叉）
    m = re.search(r"if \(r === 'agent' \|\| r === 'assistant'\) return '([^']+)'", seg)
    assert m, "必须用 `r === 'agent' || r === 'assistant'` 合并判断"
    assert m.group(1) == "agent", "归一目标应为 'agent'（CSS 单一事实源）"


def test_add_msg_normalizes_role():
    i = APP_JS.index("function addMsg(role, text)")
    seg = APP_JS[i:i + 700]
    assert "normRole(role)" in seg, "addMsg 必须先归一角色"
    assert "className = 'msg ' + r" in seg, "className 必须用归一后的值"
    assert "r === 'agent'" in seg, "渲染分支必须用归一后的值判断"


def test_css_has_assistant_alias():
    """纵深防御：历史脏数据写成 .msg.assistant 也要能渲染成气泡。"""
    assert ".msg.agent, .msg.assistant {" in CSS
    assert ".msg.agent .txt, .msg.assistant .txt" in CSS
    assert ".msg.agent .txt.md, .msg.assistant .txt.md" in CSS


def test_css_md_styles_alias_all():
    """所有 Markdown 子元素样式都必须带 assistant 别名（不留死角）。"""
    for sel in (".md-table", ".md-h ", ".md-code", ".md-ul", ".md-quote", ".md-hr", ".md-gap"):
        # 找到该选择器所在规则，必须同时含 agent 与 assistant
        idx = CSS.find(sel)
        assert idx > 0, f"缺少样式 {sel}"
        line_end = CSS.find("{", idx)
        decl = CSS[CSS.rfind("\n", 0, idx):line_end]
        assert ".msg.agent" in decl and ".msg.assistant" in decl, \
            f"{sel} 的选择器缺少 .msg.assistant 别名：{decl.strip()}"


def test_render_messages_uses_normalized_role():
    """历史回放路径必须能识别两种角色（这是 bug 的爆点）。"""
    i = APP_JS.index("function renderMessages(")
    seg = APP_JS[i:i + 900]
    assert "normRole(m.role)" in seg, "renderMessages 必须归一后再判角色"


# ==================================================== Bug 2：草稿卡回放
def test_message_model_has_task_id():
    assert "task_id" in MODELS_PY.split("class Message")[1].split("class ")[0], \
        "Message 模型必须新增 task_id 列"


def test_migration_adds_messages_task_id():
    assert '"messages": [' in MAIN_PY, "迁移表必须包含 messages 补列"
    i = MAIN_PY.index('"messages": [')
    seg = MAIN_PY[i:i + 200]
    assert "task_id" in seg, "messages 迁移必须补 task_id 列"


def test_save_chat_messages_persists_task_id():
    i = MAIN_PY.index("def _save_chat_messages(")
    seg = MAIN_PY[i:i + 1600]
    assert "task_id=(m or {}).get(\"task_id\")" in seg, \
        "_save_chat_messages 落库必须写入 task_id"


def test_chat_flow_records_draft_task_id():
    """聊天流必须捕获代笔任务 id，并写进 assistant 消息。"""
    i = MAIN_PY.index("async def _run_chat_flow(")
    seg = MAIN_PY[i:i + 3500]
    assert 'draft_tid' in seg, "必须记录本轮草稿任务 id"
    assert 'ev.get("task_kind") == "draft"' in seg, "必须识别代笔任务事件"
    assert '"task_id": draft_tid["id"]' in seg, "落库时必须带上草稿任务 id"


def test_get_messages_returns_draft_payload():
    """回放接口必须回填未消费草稿的完整载荷（卡片重建所需字段）。"""
    i = MAIN_PY.index("def get_messages(")
    seg = MAIN_PY[i:i + 2600]
    assert '"draft": drafts.get(m.task_id)' in seg, "GET messages 必须按 task_id 回填 draft"
    for f in ('"ch_idx"', '"text"', '"gate_score"', '"gate_report"', '"task_id"'):
        assert f in seg, f"draft 载荷缺少字段 {f}"
    # 已消费（采纳/丢弃）的稿子不能再弹卡片
    assert 'r.get("accepted")' in seg and 'r.get("discarded")' in seg, \
        "已采纳/已丢弃的草稿必须过滤掉"


def test_render_messages_rebuilds_draft_card():
    i = APP_JS.index("function renderMessages(")
    seg = APP_JS[i:i + 900]
    assert "renderDraft(bubble, m.draft)" in seg, "历史回放必须重建草稿卡"
    assert "bubble.dataset.task = m.task_id" in seg, "必须回填 dataset.task 供后续操作定位"


# ==================================================== Bug 2b：对话区入口
def test_draft_hint_host_in_html():
    assert 'id="draftHint"' in HTML, "对话区必须有草稿提示条宿主"


def test_draft_hint_renderer_defined():
    assert "function renderDraftHint(" in APP_JS
    i = APP_JS.index("function renderDraftHint(")
    seg = APP_JS[i:i + 1400]
    assert "gotoBibleTab('body')" in seg, "提示条按钮必须能跳到正文 tab"
    assert "scrollIntoView" in seg, "必须滚动到待审区"


def test_pending_drafts_triggers_hint():
    """待审区渲染时同步刷新提示条（单一数据源，不会漏提示）。"""
    i = APP_JS.index("function renderPendingDrafts(")
    seg = APP_JS[i:i + 500]
    assert "renderDraftHint(items)" in seg, "renderPendingDrafts 必须联动提示条"


def test_draft_hint_css_present():
    assert ".draft-hint" in CSS
    i = CSS.index(".draft-hint {")
    seg = CSS[i:i + 500]
    assert ".draft-hint.hidden" in seg or "display: none" in seg, "提示条隐藏态需有效"


# ==================================================== 端到端：真实回放链路
def test_replay_chain_complete():
    """把两个 bug 的修复串成一条链，确认没有断点。"""
    # 1) 落库：chat 流捕获 draft task_id → 写入 Message.task_id
    assert 'draft_tid' in MAIN_PY and '"task_id": draft_tid["id"]' in MAIN_PY
    # 2) 读取：GET messages 按 task_id 回填 draft
    assert '"draft": drafts.get(m.task_id)' in MAIN_PY
    # 3) 渲染：前端归一角色 + 重建卡片
    assert "normRole(m.role)" in APP_JS and "renderDraft(bubble, m.draft)" in APP_JS
    # 4) 兜底：对话区提示条
    assert "renderDraftHint(items)" in APP_JS
