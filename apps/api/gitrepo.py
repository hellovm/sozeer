"""版本化：每本书一个 Git 仓库。章节 = 文件，每次生成/修改 = 一个 commit。

天然获得 diff、回滚、备份（git push 到远端即可）。
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from git import Repo, InvalidGitRepositoryError

from apps.api.config import BOOKS_DIR

# ★★ 修复（v1.0-E4.9）：book_id 必须**严格校验**后才能拼进文件路径
#
#   ★ 问题（全端点冒烟扫出）：`repo_path()` 直接 `BOOKS_DIR / book_id`，
#     没有任何校验 → 两个真实风险：
#
#     ① **超长 ID → OSError → 500**
#        `log('x'*300)` → `[WinError 123] 文件名或目录名语法不正确`
#        （Linux 上是 `File name too long`）
#
#     ② **路径遍历（更严重）**
#        `book_id = "../../etc"` → 逃出 BOOKS_DIR，可读写任意路径
#
#   ★★ 判据：**凡是拼进文件路径的"外部输入"都必须白名单校验**
#     （与 SQL 参数绑定同理 —— 只是换了个注入面）
#
#   ★ 校验规则：只允许 [A-Za-z0-9_-]，长度 1..64
#     本项目 book_id 格式是 `bk_<hex>`，白名单完全覆盖。
_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_book_id(book_id: str) -> str:
    """★ 校验并返回安全的 book_id（不合法则抛 ValueError）。

    ★ 为什么抛异常而不是静默替换：
      静默替换会让"恶意 ID"和"正常 ID"指向同一个仓库 ——
      更危险（攻击者可覆盖正常书）。**明确拒绝**才对。
    """
    bid = str(book_id or "").strip()
    if not _ID_OK.match(bid):
        raise ValueError(f"非法的 book_id：{bid[:40]!r}（只允许字母数字下划线连字符，1-64 位）")
    return bid


def repo_path(book_id: str) -> Path:
    """★ 仓库路径（含安全校验，防路径遍历与超长路径）。"""
    bid = safe_book_id(book_id)
    p = (BOOKS_DIR / bid).resolve()
    # ★ 双保险：解析后必须仍在 BOOKS_DIR 内（防符号链接绕过）
    try:
        p.relative_to(Path(BOOKS_DIR).resolve())
    except ValueError:
        raise ValueError(f"非法的 book_id：越出仓库根目录") from None
    return p


def init_repo(book_id: str) -> Repo:
    p = repo_path(book_id)
    p.mkdir(parents=True, exist_ok=True)
    try:
        return Repo(p)
    except InvalidGitRepositoryError:
        r = Repo.init(p)
        with r.config_writer() as cw:
            cw.set_value("user", "name", "novelforge")
            cw.set_value("user", "email", "novelforge@local")
        (p / "正文").mkdir(exist_ok=True)
        (p / "大纲").mkdir(exist_ok=True)
        (p / "人物").mkdir(exist_ok=True)
        return r


def write_file(book_id: str, rel: str, content: str, commit_msg: str = "") -> None:
    r = init_repo(book_id)
    f = repo_path(book_id) / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    if commit_msg:
        r.index.add([str(f.relative_to(repo_path(book_id)))])
        # 首次提交时 HEAD 尚不存在，不能 diff("HEAD")
        if not r.head.is_valid() or r.index.diff("HEAD"):
            r.index.commit(commit_msg)


def log(book_id: str, rel: str | None = None, limit: int = 30) -> list[dict]:
    r = init_repo(book_id)
    if not r.head.is_valid():
        return []
    kwargs = {"max_count": limit}
    if rel:
        kwargs["paths"] = [rel]
    return [
        {
            "hash": c.hexsha[:8],
            "message": c.message.strip(),
            "time": dt.datetime.fromtimestamp(c.committed_datetime.timestamp()).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for c in r.iter_commits(**kwargs)
    ]


def show(book_id: str, rel: str, commit: str) -> str:
    r = init_repo(book_id)
    try:
        return (r.commit(commit).tree / rel).data_stream.read().decode("utf-8")
    except Exception:
        return ""


def rollback(book_id: str, rel: str, commit: str) -> bool:
    """把文件恢复到指定 commit 的内容，并产生一条新 commit（不改写历史）。"""
    content = show(book_id, rel, commit)
    if not content:
        return False
    write_file(book_id, rel, content, commit_msg=f"rollback {rel} -> {commit[:8]}")
    return True
