"""Skill 运行时：SKILL.md（YAML frontmatter + 正文指令）。

与 Claude / WorkBuddy Skills 生态同构 —— 本地调好的 Skill 可直接拷进来用。
用户在配置中心改过的内容存 DB（settings 表），优先级高于磁盘文件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from apps.api.models import Setting

PACKS_DIR = Path(__file__).resolve().parent / "packs"

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Skill:
    id: str
    name: str = ""
    description: str = ""
    version: str = "1"
    model_role: str = "writer"
    variables: list[str] = field(default_factory=list)
    body: str = ""
    source: str = "file"      # file | db


def _parse_fm(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def load(skill_id: str, db=None) -> Skill | None:
    p = PACKS_DIR / f"{skill_id}.md"
    if not p.exists():
        return None
    meta, body = _parse_fm(p.read_text(encoding="utf-8"))
    s = Skill(
        id=meta.get("id", skill_id),
        name=meta.get("name", skill_id),
        description=meta.get("description", ""),
        version=meta.get("version", "1"),
        model_role=meta.get("model_role", "writer"),
        variables=[v.strip() for v in (meta.get("variables", "") or "").split(",") if v.strip()],
        body=body.strip(),
    )
    if db is not None:
        ov = db.get(Setting, f"skill:{skill_id}:body")
        if ov and ov.value.strip():
            s.body = ov.value
            s.source = "db"
    return s


def list_skills(db=None) -> list[Skill]:
    out = []
    for p in sorted(PACKS_DIR.glob("*.md")):
        s = load(p.stem, db)
        if s:
            out.append(s)
    return out


def save_override(skill_id: str, body: str, db) -> None:
    key = f"skill:{skill_id}:body"
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=body)
        db.add(row)
    else:
        row.value = body
    db.commit()


def reset_override(skill_id: str, db) -> None:
    row = db.get(Setting, f"skill:{skill_id}:body")
    if row:
        db.delete(row)
        db.commit()


def render(skill: Skill, **kwargs) -> str:
    """把变量注入正文模板（{{var}}）。"""
    out = skill.body
    for k, v in kwargs.items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return re.sub(r"\{\{[a-z_]+\}\}", "", out)
