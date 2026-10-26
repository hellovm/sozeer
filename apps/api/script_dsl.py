# -*- coding: utf-8 -*-
"""剧本引用语法 DSL（v0.17 A3，评估报告缺口 1）。

背景
----
评估报告列出的**真正硬缺口之一**：

  "剧本引用语法 DSL 缺失：剧本层无法用 `@[角色/衍生]{台词}` 直接把说话人
   绑定资产与音色，改编时需要额外的名称匹配逻辑"

现状：`utterances` 是结构化 JSON（`{kind, speaker, text}`），说话人靠**裸字符串**
匹配 `DramaAsset.name`。问题：
  * 衍生形态（青年/老年、神兽/黄狗）需要额外解析 `"陈长安（青年）"` 这种括号写法
  * 剧本原文（人类可读稿）与结构化数据是两套东西，改编/校对时对不上
  * 无法在**文本层面**表达"这句是谁说的"，模型改写时容易丢说话人

本模块实现 DSL：
    @[角色名/衍生形态]{台词内容}
    @[陈长安]{山门虽远，此心可往。}
    @[陈长安/青年]{我还没有输。}
    @[大黄/神兽]{(低吼)}
    @[旁白]{三年后。}             ← 旁白（无角色资产）
    @[?]{（脚步声）}                ← 匿名/环境音

设计原则
--------
1. **解析器独立**：纯函数，不依赖 ORM，便于测试与复用。
2. **双向**：`parse()` 文本→结构化；`render()` 结构化→文本。可做 lossless round-trip。
3. **向后兼容**：`to_utterances()` 输出与现有 utterances JSON **同构**，
   下游（TTS/口型/出图）零改动。
4. **绑定资产**：`bind_assets()` 把 speaker 映射到 DramaAsset + voice_profile。
"""
from __future__ import annotations

import re

# @[角色/衍生]{台词}  —— 角色名允许中文/字母/数字/下划线/·；衍生形态可选。
# who 允许为空（`@[]{环境音}` 表示匿名/环境音，等同旁白）。
_DSL_RE = re.compile(
    r"@\[(?P<who>[^\]\[]{0,40})\]\{(?P<text>[^{}]*)\}",
    re.S,
)

# 说话人里的分隔符（角色/形态）：
#   `/` 与 `·` 是 DSL 原生写法；`（）`/`()` 是 to_utterances 的输出格式，
#   也是 drama._scene_ref_images 的解析格式 —— 三者必须都认，
#   否则本模块 render → parse 的 round-trip 会静默失败（v0.17 实测踩坑）。
_FORM_PATTERNS = (
    re.compile(r"^(?P<base>.+?)\s*[（(](?P<form>[^）)]*)[）)]\s*$"),   # 陈长安（青年）
    re.compile(r"^(?P<base>.+?)\s*[/·]\s*(?P<form>.+)$"),              # 陈长安/青年、陈长安·青年
)


def split_speaker(who: str) -> tuple[str, str]:
    """拆「角色名/衍生形态」→ (角色名, 形态)。无分隔符时形态为空。

    兼容三种写法：`角色（形态）`、`角色/形态`、`角色·形态`。
    """
    w = (who or "").strip()
    if not w:
        return "", ""
    for pat in _FORM_PATTERNS:
        m = pat.match(w)
        if m:
            base = (m.group("base") or "").strip()
            form = (m.group("form") or "").strip()
            if base and form:          # 两边都非空才算形态
                return base, form
    return w, ""

# 视为"无角色资产"的保留说话人名（等同旁白）
NARRATOR_NAMES = {"旁白", "画外音", "解说", "narrator", "vo", "voiceover", "unknown", "?"}


def parse_script(text: str) -> list[dict]:
    """把 DSL 文本解析成片段列表。

    返回每个匹配的 dict：
      {"who": 原始说话人串, "speaker": 角色名, "form": 衍生形态,
       "text": 台词, "kind": dialogue|voiceover, "start": 匹配起点, "raw": 原文}
    未匹配的普通文本**不丢弃**，作为 kind="prose" 片段一并返回（保留原文顺序），
    这样 round-trip 后仍是完整剧本。
    """
    out: list[dict] = []
    src = text or ""
    pos = 0
    for m in _DSL_RE.finditer(src):
        if m.start() > pos:                     # DSL 之间的普通文本
            seg = src[pos:m.start()]
            if seg.strip():
                out.append({"who": "", "speaker": "", "form": "", "text": seg,
                            "kind": "prose", "start": pos, "raw": seg})
        who = (m.group("who") or "").strip()
        speaker, form = split_speaker(who)
        kind = "voiceover" if _is_narrator(who) else "dialogue"
        out.append({
            "who": who, "speaker": speaker, "form": form,
            "text": (m.group("text") or "").strip(),
            "kind": kind, "start": m.start(), "raw": m.group(0),
        })
        pos = m.end()
    if pos < len(src):                          # 尾部普通文本
        seg = src[pos:]
        if seg.strip():
            out.append({"who": "", "speaker": "", "form": "", "text": seg,
                        "kind": "prose", "start": pos, "raw": seg})
    return out


def _is_narrator(who: str) -> bool:
    w = (who or "").strip().lower()
    return (not w) or (w in NARRATOR_NAMES)


def to_utterances(text_or_fragments, *, include_prose: bool = False) -> list[dict]:
    """DSL → utterances JSON（与现有 schema 同构：{kind, speaker, text}）。

    include_prose=True 时把非 DSL 的普通文本也转成 voiceover（适合整段改编）。
    speaker 保留「角色名（形态）」写法，便于下游 _scene_ref_images 解析形态。
    """
    frags = (parse_script(text_or_fragments)
             if isinstance(text_or_fragments, str) else list(text_or_fragments or []))
    out: list[dict] = []
    for f in frags:
        if f.get("kind") == "prose":
            if include_prose and (f.get("text") or "").strip():
                out.append({"kind": "voiceover", "speaker": None,
                            "text": f["text"].strip()})
            continue
        sp = f.get("speaker") or ""
        form = f.get("form") or ""
        speaker = f"{sp}（{form}）" if (sp and form) else (sp or None)
        out.append({"kind": f.get("kind") or "dialogue",
                    "speaker": speaker, "text": f.get("text") or ""})
    return out


def render_script(utterances: list[dict]) -> str:
    """utterances JSON → DSL 文本（改编稿/人工校对用）。"""
    lines = []
    for u in (utterances or []):
        if not isinstance(u, dict):
            continue
        tx = (u.get("text") or "").strip()
        if not tx:
            continue
        kind = u.get("kind") or "dialogue"
        sp = (u.get("speaker") or "").strip()
        if kind == "voiceover" or not sp:
            who = sp or "旁白"
        else:
            who = sp
        lines.append(f"@[{who}]{{{tx}}}")
    return "\n".join(lines)


def render_scene(scene_or_dict) -> str:
    """整个分镜（含场景描述）→ 可读 DSL 文本。"""
    desc = ""
    utts = []
    if isinstance(scene_or_dict, dict):
        desc = scene_or_dict.get("scene_description") or ""
        utts = scene_or_dict.get("utterances") or []
    else:
        desc = getattr(scene_or_dict, "scene_description", "") or ""
        import json as _j
        raw = getattr(scene_or_dict, "utterances", "") or ""
        if isinstance(raw, str):
            try:
                utts = _j.loads(raw or "[]")
            except Exception:
                utts = []
        else:
            utts = raw
    parts = []
    if desc.strip():
        parts.append(f"【场景】{desc.strip()}")
    body = render_script(utts)
    if body:
        parts.append(body)
    return "\n".join(parts)


# ----------------------------------------------------------------- 资产绑定
def bind_assets(utterances: list[dict], assets: list, *,
                voice_profile_of=None) -> list[dict]:
    """把 utterances 的 speaker 绑定到 DramaAsset（含衍生形态）+ 音色档案。

    assets: DramaAsset 列表（本体 + 衍生）。按 name 精确匹配本体；
            带形态（"角色（青年）"）时优先匹配衍生行，未命中回退本体。
    voice_profile_of: 可选 callable(asset) -> str，取音色档案（默认读 .voice_profile）

    返回**增强后的 utterances**（新增 asset_id / form_name / voice_profile 字段），
    原字段保持不变 → 下游零改动。
    """
    def _vp(a):
        if voice_profile_of:
            try:
                return voice_profile_of(a) or ""
            except Exception:
                return ""
        return getattr(a, "voice_profile", "") or ""

    bases = [a for a in (assets or []) if not getattr(a, "parent_id", "")]
    forms = [a for a in (assets or []) if getattr(a, "parent_id", "")]

    out = []
    for u in (utterances or []):
        if not isinstance(u, dict):
            continue
        u2 = dict(u)
        sp = (u2.get("speaker") or "").strip()
        u2.setdefault("asset_id", "")
        u2.setdefault("form_name", "")
        u2.setdefault("voice_profile", "")
        if not sp:
            out.append(u2)
            continue
        name, form = split_speaker(sp)
        pick = None
        if form:
            # 衍生形态：先按「本体名 + 形态名」在 parent_id 链上查
            # （与 drama._scene_ref_images 的规则保持一致）
            for b in bases:
                if getattr(b, "name", "") == name:
                    for f in forms:
                        if (getattr(f, "parent_id", "") == getattr(b, "id", "")
                                and getattr(f, "form_name", "") == form):
                            pick = f
                            break
                    break
            # 兜底：衍生行的 name 本身可能就叫「陈长安青年」，直接用形态名匹配
            if pick is None:
                for f in forms:
                    if getattr(f, "form_name", "") == form and (
                            getattr(f, "name", "") == name
                            or getattr(f, "name", "").endswith(form)):
                        pick = f
                        break
        if pick is None:
            for b in bases:
                if getattr(b, "name", "") == name:
                    pick = b
                    break
        if pick is not None:
            u2["asset_id"] = getattr(pick, "id", "") or ""
            u2["form_name"] = getattr(pick, "form_name", "") or ""
            # 音色：衍生形态回退本体（本体才是声纹锚点）
            vp = _vp(pick)
            if not vp and getattr(pick, "parent_id", ""):
                parent = next((b for b in bases
                               if getattr(b, "id", "") == pick.parent_id), None)
                if parent is not None:
                    vp = _vp(parent)
            u2["voice_profile"] = vp
        out.append(u2)
    return out


def normalize_script(raw) -> list[dict]:
    """统一入口：把「DSL 文本」或「旧 utterances JSON」都归一为 utterances。

    * str   → 按 DSL 解析（含普通文本时作为旁白收入）
    * list  → 原样返回（已是结构化，保持向后兼容）
    """
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # 看起来像 JSON 数组 → 尝试解析（老数据兼容）
        if s.startswith("["):
            import json as _j
            try:
                v = _j.loads(s)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return to_utterances(s, include_prose=True)
    if isinstance(raw, list):
        return raw
    return []


__all__ = [
    "parse_script", "split_speaker", "to_utterances", "render_script",
    "render_scene", "bind_assets", "normalize_script",
    "NARRATOR_NAMES",
]
