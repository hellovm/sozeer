# -*- coding: utf-8 -*-
"""改稿对比（v0.19 F6）。

为什么做
--------
项目已有章节版本（git 存储，`gitrepo`），但**只能整篇查看**（`chapter_at`），
没有「改了哪里」的可视化对比。作者改稿时最想知道的是：

  * 这一版比上一版**具体改了哪几句**？
  * AI 精修动了我哪些字？会不会把我特意写的东西改掉了？
  * 从草稿到定稿，一共净增/净删多少字？

本模块提供**行级 + 词级**两层 diff，输出结构化结果供前端渲染（左右对照 + 高亮）。

设计
----
* 纯函数（不碰 DB / 网络），便于测试与复用
* 中文友好：按**字符级**做词内 diff（中文没有空格分词，行级+字符级最实用）
* 大文本保护：超长内容截断对比（避免 O(n²) 卡死）
"""
from __future__ import annotations

import difflib
import re

# 单次对比的字符上限（超长正文全量 diff 很慢；截断后对比前 N 字并明确标注）
MAX_DIFF_CHARS = 60000


def _split_lines(text: str) -> list[str]:
    """按行切分（保留空行以便对齐；去掉行尾空白避免假差异）。"""
    return [ln.rstrip() for ln in (text or "").splitlines()]


def _char_diff(a: str, b: str) -> list[dict]:
    """行内字符级 diff（中文友好）。

    返回 [{"op": "equal|insert|delete", "text": ...}, ...]
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append({"op": "equal", "text": a[i1:i2]})
        elif tag == "delete":
            out.append({"op": "delete", "text": a[i1:i2]})
        elif tag == "insert":
            out.append({"op": "insert", "text": b[j1:j2]})
        else:  # replace
            out.append({"op": "delete", "text": a[i1:i2]})
            out.append({"op": "insert", "text": b[j1:j2]})
    return out


def _as_text(v) -> str:
    """把任意入参安全转成字符串。

    ★ 为什么需要：对比工具是**前端渲染链路上的一环**，任何入参都不该让它崩。
    实测发现 `diff_text(123, "x")` 会抛 AttributeError（对 int 调 .splitlines）——
    调用方拿到 None / 数字（如 DB 字段为 NULL、前端传错）时整个页面就白了。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def diff_text(old, new, *, context: int = 3) -> dict:
    """核心对比：返回行级变更 + 每处变更的行内字符 diff。

    context: 未变更区域保留几行上下文（前后各 N 行）。
    返回 {
      "ok", "truncated",
      "stats": {"added","removed","changed","same","chars_added","chars_removed"},
      "lines": [{"type":"same|add|del|change","old_no","new_no",
                 "old","new","inline":[{op,text}...]}...]
    }
    """
    o = _as_text(old)
    n = _as_text(new)
    truncated = False
    if len(o) > MAX_DIFF_CHARS or len(n) > MAX_DIFF_CHARS:
        o = o[:MAX_DIFF_CHARS]
        n = n[:MAX_DIFF_CHARS]
        truncated = True

    ol, nl = _split_lines(o), _split_lines(n)
    sm = difflib.SequenceMatcher(None, ol, nl, autojunk=False)

    lines: list[dict] = []
    stats = {"added": 0, "removed": 0, "changed": 0, "same": 0,
             "chars_added": 0, "chars_removed": 0}

    def _gap(lo: int, hi: int, lno: int) -> None:
        """未变更区域：只保留前后 context 行，中间折叠。"""
        seg = list(range(lo, hi))
        if len(seg) <= context * 2 + 1:
            keep = seg
        else:
            keep = seg[:context] + [-1] + seg[-context:]
        for k in keep:
            if k == -1:
                lines.append({"type": "fold", "count": len(seg) - context * 2})
            else:
                lines.append({"type": "same", "old_no": k + 1, "new_no": k + 1,
                              "old": ol[k], "new": nl[k], "inline": []})
                stats["same"] += 1

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            _gap(i1, i2, i1)
        elif tag == "insert":
            for j in range(j1, j2):
                lines.append({"type": "add", "old_no": None, "new_no": j + 1,
                              "old": "", "new": nl[j],
                              "inline": [{"op": "insert", "text": nl[j]}]})
                stats["added"] += 1
                stats["chars_added"] += len(nl[j])
        elif tag == "delete":
            for i in range(i1, i2):
                lines.append({"type": "del", "old_no": i + 1, "new_no": None,
                              "old": ol[i], "new": "",
                              "inline": [{"op": "delete", "text": ol[i]}]})
                stats["removed"] += 1
                stats["chars_removed"] += len(ol[i])
        else:  # replace：逐行配对做行内 diff
            om = list(range(i1, i2))
            nm = list(range(j1, j2))
            for k in range(max(len(om), len(nm))):
                oi = om[k] if k < len(om) else None
                nj = nm[k] if k < len(nm) else None
                if oi is not None and nj is not None:
                    inline = _char_diff(ol[oi], nl[nj])
                    lines.append({"type": "change", "old_no": oi + 1,
                                  "new_no": nj + 1, "old": ol[oi], "new": nl[nj],
                                  "inline": inline})
                    stats["changed"] += 1
                    stats["chars_added"] += sum(len(x["text"]) for x in inline if x["op"] == "insert")
                    stats["chars_removed"] += sum(len(x["text"]) for x in inline if x["op"] == "delete")
                elif oi is not None:
                    lines.append({"type": "del", "old_no": oi + 1, "new_no": None,
                                  "old": ol[oi], "new": "",
                                  "inline": [{"op": "delete", "text": ol[oi]}]})
                    stats["removed"] += 1
                    stats["chars_removed"] += len(ol[oi])
                else:
                    lines.append({"type": "add", "old_no": None, "new_no": nj + 1,
                                  "old": "", "new": nl[nj],
                                  "inline": [{"op": "insert", "text": nl[nj]}]})
                    stats["added"] += 1
                    stats["chars_added"] += len(nl[nj])

    changes = stats["added"] + stats["removed"] + stats["changed"]
    return {"ok": True, "truncated": truncated, "stats": stats,
            "lines": lines, "changed_lines": changes,
            "similarity": round(sm.ratio(), 4)}


def summarize(old: str, new: str) -> str:
    """一句话摘要（供聊天回复 / 提交说明用）。"""
    r = diff_text(old, new, context=1)
    s = r["stats"]
    if r["changed_lines"] == 0:
        return "两版内容一致，没有改动。"
    parts = []
    if s["added"]:
        parts.append(f"新增 {s['added']} 行")
    if s["removed"]:
        parts.append(f"删除 {s['removed']} 行")
    if s["changed"]:
        parts.append(f"改写 {s['changed']} 行")
    net = s["chars_added"] - s["chars_removed"]
    sign = "+" if net >= 0 else ""
    return (f"{'、'.join(parts)}；净{sign}{net} 字，"
            f"相似度 {r['similarity'] * 100:.1f}%。")


def word_level_changes(old: str, new: str, limit: int = 40) -> list[dict]:
    """只提取「改动片段」清单（前端可做"改动点列表"侧栏）。

    返回 [{"old": 旧片段, "new": 新片段}, ...]，最多 limit 条。
    """
    out = []
    for ln in diff_text(old, new, context=0)["lines"]:
        if ln["type"] == "fold" or ln["type"] == "same":
            continue
        out.append({"old": ln.get("old", ""), "new": ln.get("new", ""),
                    "type": ln["type"]})
        if len(out) >= limit:
            break
    return out


__all__ = ["diff_text", "summarize", "word_level_changes", "MAX_DIFF_CHARS"]
