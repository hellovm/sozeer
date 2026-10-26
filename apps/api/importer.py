"""把已有的小说目录导入为状态中枢里的结构化数据。

关键点：做**结构化抽取**（伏笔、人物、境界、卷），不是把 md 整文件塞进上下文。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from sqlalchemy import select

from apps.api.models import (
    Book, Chapter, Character, Foreshadow, StyleProfile, Volume, WorldSetting,
)
from apps.api.orchestrator import gates
from apps.api.orchestrator.gates import DEFAULT_BANNED
from apps.api.validators import is_realm_like, valid_character_name


def new_id(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


BANNED_FROM_OUTLINE = [
    "只见", "突然", "就在这时", "一股", "涌上心头", "他不禁", "她不禁", "我不禁",
    "不得不说", "不得不承认", "却说", "且看", "但见", "只见他",
]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def parse_volumes(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"^#{2,4}\s*卷([一二三四五六七八九十]|[0-9]+)[:：]\s*(.+?)（第?\s*(\d+)\s*[–\-~]\s*(\d+)\s*章",
                         text, re.M):
        cn = m.group(1)
        idx = "一二三四五六七八九十".find(cn) + 1 if cn in "一二三四五六七八九十" else int(cn)
        start, end = int(m.group(3)), int(m.group(4))
        # 抓取该段到下一个 ### 之前作为 outline
        seg = text[m.end():]
        nxt = re.search(r"^#{2,4}\s", seg, re.M)
        seg = seg[:nxt.start()] if nxt else seg[:1200]
        out.append({"idx": idx, "name": m.group(2).strip(), "start_ch": start,
                    "end_ch": end, "outline": seg.strip()[:1500]})
    return out


_SKIP_KW = ("力量", "势力", "资源", "节奏", "卷", "后续", "章节", "写作", "大纲", "世界观")


def parse_characters(text: str) -> list[dict]:
    """只在「人物小传」小节内按 ### 切块抽取，避免把创意小节当成人物。"""
    seg = _section(text, "人物")
    if not seg:
        return []
    heads = list(re.finditer(r"^#{3,4}[ \t]+(.+)$", seg, re.M))
    out = []
    for i, m in enumerate(heads):
        raw = m.group(1).strip()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(seg)
        body = seg[m.end():end].strip()

        if re.search(r"[：:]", raw):                       # 「主角：沈闲」
            head, tail = re.split(r"[：:]", raw, 1)
            role = head.strip("* ").strip()
            name = re.split(r"[（(（]", tail.strip())[0].strip("* ").strip()
        else:                                              # 「楚清霜（真爱线伏笔）」
            rm = re.match(r"^([^（(（\n]{1,16})\s*[（(（]([^）)）]*)", raw)
            name, role = (rm.group(1).strip(), rm.group(2).strip()) if rm else (raw.strip("*").strip(), "")

        if any(k in name for k in _SKIP_KW) or len(name) > 16:
            # 「后续重要角色」这类汇总块：抽取里面的行级人物
            for mm in re.finditer(r"^[-*]\s*\*\*([^*]+)\*\*[：:]\s*([^（(（\n，,。]{1,12})", body, re.M):
                nm = mm.group(2).strip()
                if nm and not any(k in nm for k in _SKIP_KW) and valid_character_name(nm):
                    out.append({"name": nm, "role": mm.group(1).strip(), "profile": ""})
            continue
        if not valid_character_name(name):
            # v0.7.1：编号式标题（"1. 退婚流的反转"）是卖点/大纲条目，不是人物
            continue
        out.append({"name": name, "role": role[:40], "profile": body[:900]})

    seen, uniq = set(), []
    for c in out:
        if c["name"] and c["name"] not in seen:
            seen.add(c["name"])
            uniq.append(c)
    return uniq


def _section(text: str, title_kw: str) -> str:
    """取某个标题下的整段内容，止于「同级或更高级」的下一个标题。

    注意：f-string 里正则量词必须写成 {{2,4}}，否则被当成 set 字面量。
    """
    m = re.search(rf"^(#{{2,4}})\s*[^\n]*{title_kw}[^\n]*$", text, re.M)
    if not m:
        return ""
    level = len(m.group(1))
    start = m.end()
    nxt = re.search(rf"^#{{2,{level}}}\s", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end].strip()


def parse_realm(text: str) -> list[tuple[str, str]]:
    """只从境界相关小节抽取。

    v0.7.1 修复：原来 `seg = _section(text, "力量境界") or text` 在总纲没有
    该小节时会**全文兜底**，把"1. **沈家破落真相**（第 1 章）：……— 待回收"
    这类伏笔行整体错分进「境界」分类（本地库 247 条污染即源于此）。
    现在只认明确的小节标题，且逐条过 is_realm_like 清洁度校验。
    """
    seg = ""
    for kw in ("力量境界", "境界体系", "境界设定", "境界划分", "境界"):
        seg = _section(text, kw)
        if seg:
            break
    out = []
    for line in seg.splitlines():
        m = re.match(r"^\s*\d+\.\s*\*{0,2}([^*(（]+)\*{0,2}[（(]([^）)]*)[）)]\s*[—\-–]*\s*(.*)$", line)
        if m:
            out.append((m.group(1).strip(), (m.group(2) + " " + m.group(3)).strip()))
        else:
            # 兼容无括号写法：3. 化神境——神识外放
            m2 = re.match(r"^\s*\d+\.\s*\*{0,2}([^*(（\n]{1,12})\*{0,2}\s*[—\-–]+\s*(.*)$", line)
            if m2:
                out.append((m2.group(1).strip(), m2.group(2).strip()))
    return [(k, v) for k, v in out if is_realm_like(k, v)]


def parse_world_blocks(text: str) -> list[tuple[str, str, str]]:
    """（category, key, value）—— 境界 / 势力 / 资源货币。全部过清洁度校验。"""
    out = []
    for k, v in parse_realm(text):
        out.append(("境界", k, v))
    for cat, kw in (("势力", "势力地图"), ("资源", "资源")):
        seg = _section(text, kw)
        for line in seg.splitlines():
            m = re.match(r"^\s*[-*]\s*\*{0,2}([^*(（:：]+)\*{0,2}\s*[（(]?([^）)]*)[）)]?\s*[:：]?\s*(.*)$", line)
            if m:
                key = m.group(1).strip().strip("*").rstrip("—-")
                val = (m.group(2) + " " + m.group(3)).strip().lstrip("：: ").strip()
                if key and val and is_realm_like(key, val):
                    out.append((cat, key, val[:300]))
    return [(c, k.rstrip("—-").strip(), v) for c, k, v in out]


def parse_rules(text: str) -> str:
    m = re.search(r"##\s*八[、.．]?\s*去除AI味写作准则.*?\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"(##\s*[^\n]*写作准则[^\n]*\n)(.*?)(?=\n##\s|\Z)", text, re.S)
    return m.group(2).strip() if m else ""


def parse_foreshadows(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*（第\s*(\d+)\s*章[^）]*）[:：](.+)$", text, re.M):
        content = m.group(2).strip()
        rest = m.group(4).strip()
        if "已回收" in rest:
            st = "done"
        elif "回收中" in rest:
            st = "doing"
        else:
            st = "open"
        body = re.sub(r"^[—\-–]*\s*(待回收|回收中|已回收).*$", "", rest).strip(" —-")
        out.append({"content": f"{content}：{body}" if body else content,
                    "planted_ch": int(m.group(3)), "status": st})
    return out


def import_book(db, src_dir: str, workspace_id: str = "default") -> dict:
    src = Path(src_dir)
    if not src.exists():
        raise FileNotFoundError(f"目录不存在：{src_dir}")

    # 只把「总纲」当总纲：伏笔追踪单独解析，否则人物/世界观抽取会串味
    outline_files = []
    if (src / "大纲").exists():
        outline_files = [f for f in sorted((src / "大纲").glob("*.md"))
                         if "伏笔" not in f.name and "进度" not in f.name]
    outline = "\n".join(_read(f) for f in outline_files)
    if not outline:
        outline = _read(src / "00-创作总纲.md")

    m = re.search(r"#\s*《(.+?)》", outline)
    title = m.group(1) if m else src.name
    genre = ""
    mg = re.search(r"类型[:：]\s*(.+)", outline)
    if mg:
        genre = mg.group(1).strip()

    book = Book(id=new_id("bk"), workspace_id=workspace_id, title=title, genre=genre,
                platform="generic", synopsis=_extract_synopsis(outline))
    db.add(book)
    db.flush()

    # 卷
    for v in parse_volumes(outline):
        db.add(Volume(id=new_id("vol"), book_id=book.id, idx=v["idx"], name=v["name"],
                      outline=v["outline"], start_ch=v["start_ch"], end_ch=v["end_ch"]))

    # 人物
    for c in parse_characters(outline):
        db.add(Character(id=new_id("chr"), book_id=book.id, name=c["name"],
                         role=c["role"], profile=c["profile"]))

    # 世界观（境界 / 势力 / 资源）
    for cat, k, v in parse_world_blocks(outline):
        db.add(WorldSetting(id=new_id("ws"), book_id=book.id, category=cat, key=k, value=v))

    # 风格 DNA
    pov = "第一人称"
    mp = re.search(r"叙事视角[:：]\s*(.+)", outline)
    if mp:
        pov = mp.group(1).split("。")[0].strip()
    banned = BANNED_FROM_OUTLINE or DEFAULT_BANNED
    mb = re.search(r"禁用套路词库.*?[:：]\s*\n((?:\s*-\s*.+\n)+)", outline)
    if mb:
        words = re.findall(r"[一-龥]{2,8}", mb.group(1))
        if words:
            banned = sorted(set(words))
    db.add(StyleProfile(id=new_id("sty"), book_id=book.id, pov=pov,
                        tone=_extract_tone(outline), banned_words=json.dumps(banned, ensure_ascii=False),
                        max_para_chars=120, rules=parse_rules(outline), sample=""))

    # 伏笔
    fs_text = ""
    f = src / "大纲" / "伏笔追踪.md"
    if f.exists():
        fs_text = _read(f)
    for x in parse_foreshadows(fs_text):
        db.add(Foreshadow(id=new_id("fs"), book_id=book.id, content=x["content"],
                          planted_ch=x["planted_ch"], status=x["status"]))

    # 正文
    ch_dir = src / "正文"
    chapters = []
    if ch_dir.exists():
        for p in sorted(ch_dir.glob("*.md")):
            m = re.match(r"第\s*(\d+)\s*章", p.stem)
            if not m:
                continue
            idx = int(m.group(1))
            raw = _read(p)
            lines = raw.splitlines()
            title = ""
            body = raw
            if lines and lines[0].lstrip().startswith("#"):
                h = lines[0].lstrip("# ").strip()
                t = re.match(r"第\s*\d+\s*章\s*(.*)", h)
                title = t.group(1).strip() if t else h
                body = "\n".join(lines[1:]).strip()
            chapters.append((idx, title, body))

    for idx, t, body in chapters:
        db.add(Chapter(id=new_id("ch"), book_id=book.id, idx=idx, title=t, content=body,
                       words=gates.count_words(body), status="final", summary=("").strip()))

    # 上传信息（简介/作者）
    up = src / "上传信息.md"
    if up.exists():
        t = _read(up)
        ms = re.search(r"```\n(.*?)\n```", t, re.S)
        if ms and not book.synopsis:
            book.synopsis = ms.group(1).strip()[:1200]
        ma = re.search(r"笔名\s*\|\s*(.+)", t)
        if ma:
            book.author = ma.group(1).strip()

    db.commit()

    # 落 Git
    from apps.api import gitrepo
    gitrepo.init_repo(book.id)
    for idx, t, body in chapters:
        gitrepo.write_file(book.id, f"正文/第{idx:03d}章.md",
                           f"# 第{idx:03d}章 {t}\n\n{body}\n",
                           commit_msg=f"导入：第{idx:03d}章 {t}")
    if outline:
        gitrepo.write_file(book.id, "大纲/00-创作总纲.md", outline, commit_msg="导入：创作总纲")
    if fs_text:
        gitrepo.write_file(book.id, "大纲/伏笔追踪.md", fs_text, commit_msg="导入：伏笔追踪")

    return {
        "book_id": book.id,
        "title": book.title,
        "chapters": len(chapters),
        "volumes": len(db.scalars(select(Volume).where(Volume.book_id == book.id)).all()),
        "characters": len(db.scalars(select(Character).where(Character.book_id == book.id)).all()),
        "foreshadows": len(db.scalars(select(Foreshadow).where(Foreshadow.book_id == book.id)).all()),
        "world_settings": len(db.scalars(select(WorldSetting).where(WorldSetting.book_id == book.id)).all()),
    }


def _extract_synopsis(outline: str) -> str:
    m = re.search(r"一句话梗概[:：]\s*(.+)", outline)
    if m:
        return m.group(1).strip()[:600]
    return ""


def _extract_tone(outline: str) -> str:
    m = re.search(r"基调[:：]\s*(.+)", outline)
    return m.group(1).strip()[:200] if m else ""
