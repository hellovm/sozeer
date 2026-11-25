# -*- coding: utf-8 -*-
"""★ git 章节文件「单 H1 标题」不变量回归（#364 patch_chapter 双标题 bug 修复）。

背景（血泪·系统性）：Chapter.content 形态因写入路径而异——
  · 代笔/精修采纳的章节 **含** `# 第NNN章` 标题行（agent.py/graph.py 都拼了前缀）；
  · 编辑器保存的章节 **不含** 标题行（main.py 用 splitlines()[1:] 剥掉）。
所以代码里到处用 `gates.strip_title`（幂等：含标题就剥、不含原样）做兜底。

但**所有 git 章节写点**若把「已含标题行」的正文再套一层 `# 标题` 前缀，
git 文件就会出现 **两个 H1**，且与 DB 正文不一致；回滚到该 commit 还会把
双标题正文写回 DB。`rename_chapter` 更糟（新标题前缀 + 旧标题正文 → 双标题且不一致）。

★ 根因定位（#364 f3a69a6 引入 patch_chapter 时暴露的确定性双标题）：
  修复后 4 处 git 写点统一「正文套 strip_title / 直接写归一化 new_content」。

本文件钉死「git 章节文件恰好一个 H1」不变量，覆盖 两种正文形态（含/不含标题行）
+ 旧双标题写法的防回退源码断言（防再次退化）。
"""
from __future__ import annotations

import os
import re
import tempfile

# ★ import 前隔离数据目录（gitrepo 依赖 config.BOOKS_DIR），避免污染真实数据。
os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_v141_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-v141")

import pytest  # noqa: E402

from apps.api import gitrepo  # noqa: E402
from apps.api.orchestrator import gates  # noqa: E402


def _h1_count(text: str) -> int:
    """统计「行首 # 且形如 第NNN章」的 H1 标题行数（本不变量的度量）。"""
    n = 0
    for ln in (text or "").splitlines():
        s = ln.lstrip()
        if s.startswith("#") and re.match(r"^#\s*第\s*\d+\s*章", s):
            n += 1
    return n


@pytest.fixture()
def books_dir(tmp_path, monkeypatch):
    """把 gitrepo 的 BOOKS_DIR 重定向到临时目录（真实写盘、完全隔离）。"""
    p = tmp_path / "books"
    p.mkdir()
    monkeypatch.setattr(gitrepo, "BOOKS_DIR", p)
    return p


def _read_latest(bid: str, rel: str) -> str:
    """读某章节 git 文件的**磁盘当前内容**（write_file 直接落盘，不依赖是否 commit）。

    ★ 为什么读文件而非 gitrepo.show：write_file 仅在传了 commit_msg 时才 commit，
      测试里做纯内容不变量校验时不必 commit，直接读落盘文件最稳。
    """
    p = gitrepo.repo_path(bid) / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ============================================================ 性质：strip_title 幂等
def test_strip_title_idempotent_both_shapes():
    """核心性质：strip_title 幂等——含标题就剥、不含原样（两种正文形态都安全）。"""
    assert gates.strip_title("# 第001章 雪崩\n\n山风呼啸") == "山风呼啸"
    assert gates.strip_title("山风呼啸") == "山风呼啸"
    assert gates.strip_title("# 第003章 空\n") == ""


# ============================================================ 行为：真实写盘单 H1
def test_git_write_single_h1_body_has_title(books_dir):
    """代笔形态：正文已含标题行 → 修复后 git 文件仍恰好 1 个 H1。"""
    bid = "bk_test_single_h1"
    body = "# 第001章 雪崩\n\n山风呼啸，雪崩压塌了木屋。"
    # 与 accept_draft / 编辑器保存 修复后的写法一致：正文套 strip_title
    gitrepo.write_file(bid, "正文/第001章.md",
                       f"# 第001章 雪崩\n\n{gates.strip_title(body)}\n")
    written = _read_latest(bid, "正文/第001章.md")
    assert _h1_count(written) == 1, f"git 章节文件出现双 H1：\n{written[:160]}"


def test_git_write_single_h1_body_no_title(books_dir):
    """编辑器形态：正文不含标题行 → 前缀拼 1 个 H1，strip_title 不动它。"""
    bid = "bk_test_single_h1b"
    body = "山风呼啸，雪崩压塌了木屋。"
    gitrepo.write_file(bid, "正文/第001章.md",
                       f"# 第001章 雪崩\n\n{gates.strip_title(body)}\n")
    written = _read_latest(bid, "正文/第001章.md")
    assert _h1_count(written) == 1


def test_patch_chapter_writes_normalized_content(books_dir):
    """patch_chapter：归一化后直接写 new_content（已含唯一标题行，不再叠前缀）。"""
    bid = "bk_test_single_h1c"
    new_content = "# 第001章 雪崩\n\n修订后的正文。"
    gitrepo.write_file(bid, "正文/第001章.md", new_content + "\n")
    written = _read_latest(bid, "正文/第001章.md")
    assert _h1_count(written) == 1


def test_legacy_prefix_actually_double_h1(books_dir):
    """对照组（假阴性防护）：旧写法「裸正文 + 前缀」确实产生双 H1 ——
    证明本文件的 _h1_count 检测器真的能抓到该不变量，测试没白做。"""
    bid = "bk_ctrl_double"
    body = "# 第001章 雪崩\n\n正文"
    gitrepo.write_file(bid, "正文/第001章.md", f"# 第001章 雪崩\n\n{body}\n")  # 旧 bug 写法
    written = _read_latest(bid, "正文/第001章.md")
    assert _h1_count(written) == 2, "对照组应复现双 H1（否则检测器失效）"


# ============================================================ 防回退：旧双标题写法必须已消除
def test_double_title_writesites_removed():
    """★ 源码守门（防回退）：旧的「裸正文 + 标题前缀」双标题写法必须已删除。
    用精确旧串 `not in` 判定（唯一长串，无子串歧义）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    agent = (root / "apps" / "api" / "agent.py").read_text(encoding="utf-8")
    graph = (root / "apps" / "api" / "orchestrator" / "graph.py").read_text(encoding="utf-8")
    main = (root / "apps" / "api" / "main.py").read_text(encoding="utf-8")

    # patch_chapter：旧版叠 `# {old_title}` 前缀（双 H1）
    assert r'f"# {old_title}\n\n{new_content}\n"' not in agent, \
        "patch_chapter 回退到叠 old_title 前缀（双 H1）"
    # rename_chapter：旧版裸 {c.content} 前缀（双 H1 且旧标题残留）
    assert r'f"# 第{ch_idx:03d}章 {title}\n\n{c.content}\n"' not in agent, \
        "rename_chapter 回退到裸 c.content（双 H1 + 旧标题）"
    # accept_draft（主采纳路径）：旧版裸 {body} 前缀
    assert r'f"# 第{ch_idx:03d}章 {title}\n\n{body}\n"' not in graph, \
        "accept_draft 回退到裸 body（双 H1）"
    # 编辑器保存：旧版裸 {c.content} 前缀
    assert r'f"# 第{idx:03d}章 {c.title}\n\n{c.content}\n"' not in main, \
        "编辑器保存回退到裸 c.content（双 H1）"
