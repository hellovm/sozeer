# -*- coding: utf-8 -*-
"""题材包（Genre Pack）：把「题材相关的规则与措辞」从内核里解耦出来（v0.15）。

背景
----
v0.14 及以前，系统的题材知识是**硬编码散落**在 7 个文件里的：
度量衡维度、数值扫描正则、境界词表、风格锚点、封面锚点、主编人设提示词……
它们**全部默认仙侠玄幻**。结果是：写都市文时，"灵石/功法/层重阶品"照样
参与记账，境界闸门对"职级"视而不见，封面锚点把职场剧画成古装。

设计原则（与 platform.py 完全一致，便于维护者心领神会）
----------------------------------------------------
1. **配置包**：每个题材是一份 dict（见 GENRES），声明式、可读、可 diff。
2. **未命中返 None（opt-in）**：get_genre() 拿不到就返回 None，调用方
   走**原有兜底逻辑**——这样老书零影响、零回归，新题材逐步接入。
3. **主题材单选 + 标签多选**（用户已确认的产品决策）：
   * `book.theme` 决定**度量衡与实力体系**（必须唯一，两套度量衡冲突会
     导致数值账本记账混乱）；
   * `book.tags` 只影响**提示词措辞**（风格/封面/人设语气），可多选叠加。
   所以本模块提供两个入口：get_genre()（单主题材，取硬规则）与
   style_hints_for()（多标签，取软措辞）。

兼容承诺
--------
- 所有对外函数在拿不到题材包时**必须**返回"不启用"语义（None / 空列表 /
  上游原有的默认值），绝不抛异常、绝不改变老书行为。
- 数值账本、闸门、审计的全部调用点保持**函数名与签名不变**，只换内部实现
  （见各模块的 realm_whitelist_for 等），把改动面从 11 处压到 2 处。
"""
from __future__ import annotations

# ============================================================ 题材包定义
# 字段说明（全部可选，缺省即"该项不启用"）：
#   label          中文名（UI 显示）
#   aliases        别名/关键词，用于把自由文本的 book.genre 匹配到本包（子串匹配）
#   metrics        度量衡维度 {维度: {"units": [...], "cumulative": bool}}
#                  决定数值账本按什么维度记账（金钱/经验/年龄/功法…）
#   scan_patterns  正文数值扫描正则 [(维度, pattern_str)]，pattern 用 {NUM} 占位
#   realm_dim      实力/等级体系在世界观里对应的 category 名（仙侠="境界"，都市="职级"…）
#   realm_stops    该题材下**不是**实力等级的高频"X境/X级"词，防误报
#   style_hint     出图风格锚点（给 drama/cover 用）
#   cover_anchor   封面视觉锚点（LLM 不可用时的确定性兜底）
#   prompt_hint    注入主编人设的题材偏好措辞
#
# 注意：metrics/scan_patterns 是"硬规则"，只有 theme（主题材）生效；
#       style_hint/cover_anchor/prompt_hint 是"软措辞"，tags 可叠加。

GENRES: dict[str, dict] = {

    # ---------------------------------------------------------- 仙侠玄幻（原有默认，抽出）
    "xianxia": {
        "label": "仙侠玄幻",
        "aliases": ["仙侠", "修仙", "修真", "玄幻", "古风", "武侠", "仙", "道",
                    "宗门", "洪荒", "神话", "灵气"],
        "metrics": {
            "金钱": {"units": ["枚", "块", "两", "文", "灵石", "金币", "银两", "万"],
                     "cumulative": True},
            "经验": {"units": ["点", "经验"], "cumulative": True},
            "年龄": {"units": ["岁", "年"], "cumulative": True},
            "功法": {"units": ["层", "重", "阶", "品"], "cumulative": True},
            "级别": {"units": ["级", "阶", "段", "星"], "cumulative": True},
            "声望": {"units": ["点"], "cumulative": True},
            "其他": {"units": [], "cumulative": True},
        },
        "scan_patterns": [
            ("金钱", r"{NUM}\s*(?:枚|块|两|文|灵石|金币|银两)"),
            ("经验", r"{NUM}\s*(?:点经验|经验值|经验)"),
            ("年龄", r"{NUM}\s*岁"),
            ("功法", r"{NUM}\s*(?:层|重)"),
            ("级别", r"{NUM}\s*(?:级|阶|段|星)"),
        ],
        "realm_dim": "境界",
        "realm_stops": [
            "心境", "处境", "困境", "逆境", "绝境", "梦境", "幻境", "秘境", "环境",
            "意境", "语境", "边界", "境外", "之境", "实境", "场景", "接境", "止境",
            "无人之境", "大环境", "小环境",
        ],
        "style_hint": "中国仙侠古风，水墨质感，飘逸衣袂，仙气缭绕",
        "cover_anchor": "中国古风仙侠意境：山水云海、古典楼阁、法器灵光，古装人物",
        "prompt_hint": "力量/境界体系要自洽（如练气→筑基→金丹…），能制造冲突与爽点；"
                       "数值（灵石/功法层数）需前后一致。",
    },

    # ---------------------------------------------------------- 都市（用户优先要求）
    "urban": {
        "label": "都市",
        "aliases": ["都市", "现代", "言情", "婚恋", "职场", "商战", "现实",
                    "重生", "娱乐圈", "校园"],
        "metrics": {
            # 都市文的"钱"是 RMB；等级是身家/职级/权力；没有功法
            "金钱": {"units": ["元", "块", "万", "亿", "毛", "角", "千万", "百万"],
                     "cumulative": True},
            "职级": {"units": ["级", "阶", "品", "等"], "cumulative": True},
            "年龄": {"units": ["岁", "年"], "cumulative": True},
            "声望": {"units": ["点", "分"], "cumulative": True},
            "粉丝": {"units": ["万", "个", "位"], "cumulative": True},
            "其他": {"units": [], "cumulative": True},
        },
        "scan_patterns": [
            ("金钱", r"{NUM}\s*(?:万元|亿元|元|块|千万|百万)"),
            ("职级", r"{NUM}\s*(?:级|阶|品|等)"),
            ("年龄", r"{NUM}\s*岁"),
            ("粉丝", r"{NUM}\s*(?:万粉丝|粉丝)"),
        ],
        # 都市文的实力体系不是"境界"，而是职级/身家/资源
        "realm_dim": "职级",
        "realm_stops": [
            "阶级", "阶层", "高级", "低级", "顶级", "上级", "下级", "等级",
            "级别", "层级", "阶段", "台阶", "阶梯", "品级", "等第",
        ],
        "style_hint": "现代写实风格，电影感光影，画面干净利落",
        "cover_anchor": "现代都市景观或精致室内场景，现代装束人物，城市天际线",
        "prompt_hint": "冲突来自现实规则（职场博弈、商战、人情世故、金钱与尊严）；"
                       "爽点靠信息差与专业碾压，金钱/职级数值需前后一致。",
    },

    # ---------------------------------------------------------- 都市悬疑
    "urban_suspense": {
        "label": "都市悬疑",
        "aliases": ["悬疑", "推理", "刑侦", "灵异", "惊悚", "恐怖", "密室", "犯罪"],
        "metrics": {
            "金钱": {"units": ["元", "万", "块"], "cumulative": True},
            "年龄": {"units": ["岁", "年"], "cumulative": True},
            "案件": {"units": ["起", "宗", "件", "桩"], "cumulative": True},
            "时间": {"units": ["天", "小时", "分钟", "月"], "cumulative": False},
            "其他": {"units": [], "cumulative": True},
        },
        "scan_patterns": [
            ("金钱", r"{NUM}\s*(?:万元|元|万|块)"),
            ("年龄", r"{NUM}\s*岁"),
            ("案件", r"{NUM}\s*(?:起|宗|件|桩)"),
            ("时间", r"{NUM}\s*(?:天|小时|分钟|个月)(?:前|后|内)"),
        ],
        "realm_dim": "",          # 悬疑无实力体系，闸门不启用
        "realm_stops": [],
        "style_hint": "昏暗悬疑氛围，冷暖对比强光影，电影感构图",
        "cover_anchor": "阴暗悬疑氛围，雨夜街道或逼仄空间，冷暖强对比光影",
        "prompt_hint": "核心是谜题设计与信息控制：线索要公平可推（读者能拼出真相），"
                       "反转要符合逻辑。禁用无源巧合，证据链需自洽。",
    },

    # ---------------------------------------------------------- 科幻
    "scifi": {
        "label": "科幻",
        "aliases": ["科幻", "星际", "末世", "机甲", "赛博", "未来", "太空", "废土"],
        "metrics": {
            "金钱": {"units": ["信用点", "星币", "元", "万"], "cumulative": True},
            "能源": {"units": ["度", "单位", "焦"], "cumulative": True},
            "年龄": {"units": ["岁", "年"], "cumulative": True},
            "等级": {"units": ["级", "阶", "段", "星"], "cumulative": True},
            "其他": {"units": [], "cumulative": True},
        },
        "scan_patterns": [
            ("金钱", r"{NUM}\s*(?:信用点|星币|元|万)"),
            ("能源", r"{NUM}\s*(?:度|单位|焦)"),
            ("年龄", r"{NUM}\s*岁"),
            ("等级", r"{NUM}\s*(?:级|阶|段|星)"),
        ],
        "realm_dim": "等级",
        "realm_stops": ["阶级", "阶层", "高级", "低级", "顶级", "阶段", "层级", "等级"],
        "style_hint": "未来感科幻场景，冷色调科技元素与光效，硬核质感",
        "cover_anchor": "未来感科幻场景，冷色调科技元素与光效，星舰或机甲剪影",
        "prompt_hint": "设定需自洽（科技原理可虚构但要内部一致）；冲突来自科技伦理、"
                       "资源争夺或文明博弈，避免软化成纯动作片。",
    },

    # ---------------------------------------------------------- 游戏电竞
    "game": {
        "label": "游戏电竞",
        "aliases": ["游戏", "电竞", "网游", "虚拟", "竞技", "副本", "全息"],
        "metrics": {
            "金钱": {"units": ["金币", "元", "万", "信用点"], "cumulative": True},
            "经验": {"units": ["点", "经验", "EXP"], "cumulative": True},
            "等级": {"units": ["级", "阶", "段", "星"], "cumulative": True},
            "年龄": {"units": ["岁"], "cumulative": True},
            "其他": {"units": [], "cumulative": True},
        },
        "scan_patterns": [
            ("金钱", r"{NUM}\s*(?:金币|元|万|信用点)"),
            ("经验", r"{NUM}\s*(?:点经验|经验值|经验|EXP)"),
            ("等级", r"{NUM}\s*(?:级|阶|段|星)"),
            ("年龄", r"{NUM}\s*岁"),
        ],
        "realm_dim": "等级",
        "realm_stops": ["阶级", "阶层", "高级", "低级", "顶级", "阶段", "层级", "等级"],
        "style_hint": "虚拟与现实交错的场景，霓虹与数据流光效，游戏感强",
        "cover_anchor": "虚拟现实交错的场景，霓虹与数据流光效，游戏角色剪影",
        "prompt_hint": "数值成长要可视化（等级/装备/技能），游戏规则需自洽；"
                       "爽点来自策略深度与实力成长曲线。",
    },
}

# 默认题材（老书未设 theme 时的兜底 = 原有仙侠行为，零回归）
DEFAULT_GENRE = "xianxia"


# ============================================================ DB 覆盖层
# 用户要求「题材包放 DB 表、配置中心可改，无需发版」。实现为**代码为默认、
# DB 为覆盖**：内置 GENRES 保证开箱即用；genre_packs 表非空时可覆盖/新增。
# 关键：DB 不可用（表不存在 / 查询异常）时**静默回退**到内置包，绝不影响写作。

_DB_CACHE: dict | None = None   # {key: merged_dict}；None = 未加载
_DB_LOADED = False


def _load_db_rows(db):
    """读取 genre_packs 全部记录（含停用），返回 {key: row}。失败返回 {}。

    为什么要读停用行？因为 config 中心的「停用」语义必须能**压过**内置包
    —— 否则用户停用 'scifi' 后，内置的 scifi 仍会从 dict(GENRES) 兜底回来，
    表现为"停用按钮没反应"（v0.15 实测踩坑）。
    """
    try:
        from sqlalchemy import select as _sel
        from apps.api.models import GenrePack
        try:
            db.expire_all()          # 丢弃 identity map，强制重新查库
        except Exception:
            pass
        return {r.key: r for r in db.scalars(_sel(GenrePack)).all()}
    except Exception:
        return {}


def _load_db_packs(db) -> dict[str, dict]:
    """从 genre_packs 表读取覆盖层，与内置包合并。失败返回 {}（用内置）。

    ★ 陷阱警告（v0.13.14 同类问题，v0.15 复现）：本服务 SessionLocal(autoflush=False)
    且会话跨请求复用 SQLAlchemy identity map。若同一会话先 get() 过某行，
    之后再 select() 会直接返回**缓存对象**，看不到其他会话提交的新值——
    表现为"配置中心改了题材包，前端刷新还是旧值"。故查询前必须 expire_all()。
    """
    from apps.api.models import jl
    out: dict[str, dict] = {}
    for key, r in _load_db_rows(db).items():
        if not r.enabled:
            continue                              # 停用 → 不参与合并
        # 坏包（config_json 非法 JSON 且非空）→ 跳过，避免把垃圾配置混进题材表。
        # jl() 解析失败返回默认值，无法区分"空配置"与"坏配置"，故先探测。
        raw_cfg = (r.config_json or "").strip()
        if raw_cfg:
            try:
                import json as _json
                parsed = _json.loads(raw_cfg)
            except Exception:
                continue                          # 解析失败 → 视为坏包，跳过
            if not isinstance(parsed, dict):
                continue
        cfg = jl(r.config_json, {}) or {}
        if not isinstance(cfg, dict):
            continue
        # 合并顺序（后者覆盖前者）：内置包 → config_json → 专用列
        # 专用列（label/aliases）必须最后覆盖，否则"改 label 列不生效"
        # （v0.15 实测踩坑：seed 时 label 同时写进了 config_json，导致列改动被旧值盖回）
        base = dict(GENRES.get(key, {}))
        base.update(cfg)
        if r.label:
            base["label"] = r.label            # 专用列优先于 config_json
        col_aliases = jl(r.aliases, [])
        if isinstance(col_aliases, str):
            col_aliases = [col_aliases] if col_aliases else []
        aliases = col_aliases or cfg.get("aliases") or base.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        base["aliases"] = aliases
        base["_source"] = "db"
        out[key] = base
    return out


def _disabled_keys(db) -> set[str]:
    """被显式停用的 key 集合（含内置包）——用于从兜底表里剔除。"""
    from apps.api.models import GenrePack  # noqa: F401
    return {k for k, r in _load_db_rows(db).items() if not r.enabled}


def effective_genres(db=None) -> dict[str, dict]:
    """合并后的题材表（内置 + DB 覆盖）。db 为 None 时只返回内置。

    合并规则：
      ① 以内置包为基底（保证零依赖启动）
      ② DB 覆盖层替换同 key / 追加新 key
      ③ **DB 里 enabled=0 的 key 从结果中剔除**（含内置包）——
         这样配置中心才能真正"停用"一个内置题材
    """
    global _DB_CACHE, _DB_LOADED
    if db is None:
        return dict(GENRES)
    if _DB_LOADED and _DB_CACHE is not None:
        return _DB_CACHE
    merged = dict(GENRES)
    merged.update(_load_db_packs(db))
    for k in _disabled_keys(db):          # ③ 停用项（含内置）一律剔除
        merged.pop(k, None)
    _DB_CACHE = merged
    _DB_LOADED = True
    return merged


def invalidate_cache() -> None:
    """配置中心改完题材包后调用，让下次读取重新加载。"""
    global _DB_CACHE, _DB_LOADED
    _DB_CACHE = None
    _DB_LOADED = False


# ============================================================ 导入 / 导出（v0.16 G12）
# 用途：题材包可备份、可分享（"我调好的都市包发你"）。JSON 结构：
#   {"schema": 1, "exported_at": "...", "packs": [{"key","label","aliases","config","builtin"}]}
EXPORT_SCHEMA = 1


def export_packs(db, keys: list[str] | None = None, include_builtin: bool = True) -> dict:
    """导出题材包为 JSON 友好结构。

    keys=None 导出全部；include_builtin=False 只导自定义包（分享用）。
    """
    from apps.api.models import GenrePack, jl
    import datetime as _dt
    rows = _load_db_rows(db)
    out_packs = []
    # v0.16 修复：必须以「内置包 ∪ DB 包」为全集——只用 GENRES.keys() 会让
    # 自定义题材（只存在于 DB）导不出去，备份/分享直接丢数据。
    all_keys = list(GENRES.keys())
    for k in rows.keys():
        if k not in all_keys:
            all_keys.append(k)
    for k in (keys or all_keys):
        is_builtin = k in GENRES
        if is_builtin and not include_builtin:
            continue
        row = rows.get(k)
        if row is not None:
            cfg = jl(row.config_json, {}) or {}
            aliases = jl(row.aliases, [])
            label = row.label or (GENRES.get(k, {}).get("label", k))
            enabled = int(row.enabled)
        elif k in GENRES:
            g = GENRES[k]
            cfg = {kk: vv for kk, vv in g.items() if kk not in ("label", "aliases")}
            aliases = list(g.get("aliases", []))
            label = g["label"]
            enabled = 1
        else:
            continue
        out_packs.append({
            "key": k, "label": label, "aliases": aliases,
            "config": cfg, "builtin": 1 if is_builtin else 0, "enabled": enabled,
        })
    return {"schema": EXPORT_SCHEMA,
            "exported_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "packs": out_packs}


def import_packs(db, payload: dict, on_conflict: str = "skip") -> dict:
    """导入题材包。

    on_conflict:
      * "skip"   —— 已存在则跳过（默认，最安全）
      * "update" —— 已存在则覆盖
      * "rename" —— 冲突时自动加后缀 _2 新建
    返回 {ok, created, updated, skipped, errors}
    """
    from apps.api.models import GenrePack, jd
    packs = (payload or {}).get("packs")
    if not isinstance(packs, list):
        return {"ok": False, "error": "payload 缺 packs 数组",
                "created": 0, "updated": 0, "skipped": 0, "errors": []}

    created = updated = skipped = 0
    errors: list[str] = []
    for i, p in enumerate(packs):
        try:
            if not isinstance(p, dict):
                errors.append(f"#{i} 不是对象"); continue
            key = str(p.get("key") or "").strip().lower()
            import re as _re
            if not key or not _re.fullmatch(r"[a-z0-9_]+", key):
                errors.append(f"#{i} key 非法：{p.get('key')!r}"); continue
            label = str(p.get("label") or key).strip()
            cfg = p.get("config") or {}
            if not isinstance(cfg, dict):
                errors.append(f"#{i} config 不是对象"); continue
            aliases = p.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [x.strip() for x in aliases.replace("，", ",").split(",") if x.strip()]

            row = db.get(GenrePack, key)
            is_builtin = key in GENRES
            if row is None and is_builtin:
                # 内置包还没落库 → 先补种子，拿到行再按冲突策略处理
                seed_builtin(db)
                row = db.get(GenrePack, key)

            if row is not None:
                if on_conflict == "skip":
                    skipped += 1; continue
                if on_conflict == "rename":
                    newkey = key
                    n = 2
                    while db.get(GenrePack, newkey) or newkey in GENRES:
                        newkey = f"{key}_{n}"; n += 1
                    db.add(GenrePack(key=newkey, label=label, aliases=jd(aliases),
                                     config_json=jd(cfg), builtin=0,
                                     enabled=int(p.get("enabled", 1)), sort_order=200))
                    created += 1; continue
                # update
                row.label = label
                row.aliases = jd(aliases)
                row.config_json = jd(cfg)
                row.enabled = int(p.get("enabled", row.enabled))
                updated += 1
            else:
                db.add(GenrePack(key=key, label=label, aliases=jd(aliases),
                                 config_json=jd(cfg),
                                 builtin=1 if is_builtin else 0,
                                 enabled=int(p.get("enabled", 1)), sort_order=200))
                created += 1
        except Exception as e:
            errors.append(f"#{i} {type(e).__name__}: {e}")
    try:
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"提交失败：{e}",
                "created": created, "updated": updated, "skipped": skipped, "errors": errors}
    invalidate_cache()
    return {"ok": True, "created": created, "updated": updated,
            "skipped": skipped, "errors": errors}


def seed_builtin(db) -> int:
    """把内置题材包写入 genre_packs（幂等：已存在则跳过，不覆盖用户改动）。

    写入拆分原则（避免"两处真相"）：
      * `label` / `aliases` → 写专用列（配置中心直接可改、UI 友好）
      * 其余配置项（metrics/scan_patterns/realm_* 等）→ 写 config_json
    注意：**不要把 label/aliases 同时塞进 config_json** —— 否则改专用列时
    会被 config_json 里的旧值盖回（v0.15 实测踩过这个坑）。
    """
    from apps.api.models import GenrePack, jd
    n = 0
    dedup = {"label", "aliases"}
    for i, (k, v) in enumerate(GENRES.items()):
        if db.get(GenrePack, k):
            continue
        db.add(GenrePack(
            key=k, label=v.get("label", k),
            aliases=jd(v.get("aliases", [])),
            config_json=jd({kk: vv for kk, vv in v.items() if kk not in dedup}),
            builtin=1, enabled=1, sort_order=i * 10,
        ))
        n += 1
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return n


# ============================================================ 查询接口
def get_genre(name: str | None, db=None) -> dict | None:
    """按主题材名取配置包；未命中返回 None（不启用题材适配）。

    匹配顺序：① 精确 key ② 别名子串匹配（book.genre 常是自由文本，
    如 '系统流 + 轻松搞笑 + 修仙 + 反套路'，需子串命中）。
    """
    if not name:
        return None
    table = effective_genres(db)
    key = str(name).strip().lower()
    if key in table:
        return table[key]
    # 别名子串匹配。评分规则（按优先级）：
    #   ① 命中别名越长越优先（"都市悬疑" > "都市"）
    #   ② 平局时，命中别名**总长度**更大者优先 —— 解决 "都市悬疑" 同时命中
    #      urban 的 "都市"(2字) 与 urban_suspense 的 "悬疑"(2字) 的平局，
    #      此时应选"语义覆盖更完整"的一方。
    #   ③ 仍平局时，按 key 字典序 —— 保证结果**确定性**（不依赖 dict 插入顺序，
    #      否则 DB 覆盖层加载顺序变化会导致同一输入命中不同题材）。
    raw = str(name)
    best, best_score = None, (0, 0, "")
    for gk in sorted(table.keys()):          # ③ 确定性遍历
        gv = table[gk]
        hits = [a for a in (gv.get("aliases") or []) if a and a in raw]
        if not hits:
            continue
        score = (max(len(a) for a in hits),           # ① 最长命中
                 sum(len(a) for a in hits),           # ② 覆盖总量
                 # ③ 反转字典序：靠前的 key 优先（sorted 升序 → 用 chr 反转）
                 "".join(chr(0x10FFFF - ord(c)) for c in gk))
        if score > best_score:
            best, best_score = gv, score
    return best


def get_genre_by_key(key: str | None, db=None) -> dict | None:
    """按 key 精确取（不做别名匹配），给配置中心/内部调用用。"""
    if not key:
        return None
    return effective_genres(db).get(str(key).strip().lower())


def metrics_for(theme: str | None, db=None) -> dict:
    """取主题材的度量衡维度定义；未命中回退仙侠原表（零回归）。"""
    g = get_genre(theme, db) or effective_genres(db).get(DEFAULT_GENRE) or GENRES[DEFAULT_GENRE]
    return g.get("metrics") or GENRES[DEFAULT_GENRE]["metrics"]


def scan_patterns_for(theme: str | None, db=None) -> list[tuple[str, str]]:
    """取主题材的数值扫描模式（(维度, 正则模板) 列表，{NUM} 为占位）。"""
    g = get_genre(theme, db) or GENRES[DEFAULT_GENRE]
    pats = g.get("scan_patterns")
    return pats if pats is not None else GENRES[DEFAULT_GENRE]["scan_patterns"]


def realm_dim_for(theme: str | None, db=None) -> str:
    """取主题材的"实力体系维度名"（仙侠=境界，都市=职级…）。

    返回空串表示该题材无实力体系（如悬疑），闸门应跳过。
    未命中题材时回退 '境界'（保持老书行为）。
    """
    g = get_genre(theme, db)
    if g is None:
        return "境界"
    return g.get("realm_dim", "境界")


def realm_stops_for(theme: str | None, db=None) -> list[str]:
    """取主题材的"非实力等级"高频词（防闸门误报）。"""
    g = get_genre(theme, db) or GENRES[DEFAULT_GENRE]
    return g.get("realm_stops") or GENRES[DEFAULT_GENRE]["realm_stops"]


# ============================================================ 软措辞（可多标签叠加）
def style_hints_for(theme: str | None = None, tags: list[str] | None = None, db=None) -> list[str]:
    """按主题材 + 标签收集风格锚点（去重、保序）。

    tags 是"影响提示词"的软标签，多个命中即叠加——例如
    theme='urban' + tags=['悬疑'] → 同时拿到"现代写实"与"昏暗悬疑"。
    """
    out: list[str] = []

    def _push(txt: str):
        if txt and txt not in out:
            out.append(txt)

    g = get_genre(theme, db) if theme else None
    if g:
        _push(g.get("style_hint", ""))
    for t in (tags or []):
        gt = get_genre(t, db)
        if gt:
            _push(gt.get("style_hint", ""))
    return out


def cover_anchors_for(theme: str | None = None, tags: list[str] | None = None, db=None) -> list[str]:
    """按主题材 + 标签收集封面视觉锚点（去重、保序）。"""
    out: list[str] = []

    def _push(txt: str):
        if txt and txt not in out:
            out.append(txt)

    g = get_genre(theme, db) if theme else None
    if g:
        _push(g.get("cover_anchor", ""))
    for t in (tags or []):
        gt = get_genre(t, db)
        if gt:
            _push(gt.get("cover_anchor", ""))
    return out


def prompt_hints_for(theme: str | None = None, tags: list[str] | None = None, db=None) -> list[str]:
    """按主题材 + 标签收集"注入主编人设"的题材偏好措辞。"""
    out: list[str] = []

    def _push(txt: str):
        if txt and txt not in out:
            out.append(txt)

    g = get_genre(theme, db) if theme else None
    if g:
        _push(g.get("prompt_hint", ""))
    for t in (tags or []):
        gt = get_genre(t, db)
        if gt:
            _push(gt.get("prompt_hint", ""))
    return out


# ============================================================ 清单（给 UI / API）
def catalog(db=None, include_disabled: bool = False) -> list[dict]:
    """题材清单：给配置中心下拉、立项 UI 用。

    include_disabled=True 时（配置中心用）额外返回已停用题材，
    便于运营看到并重新启用它们。
    """
    table = effective_genres(db)
    items = []
    disabled: set = set()
    if include_disabled and db is not None:
        disabled = _disabled_keys(db)
        for k in disabled:
            if k in table:
                continue
            row_cfg = None
            try:
                rows = _load_db_rows(db)
                r = rows.get(k)
                if r:
                    from apps.api.models import jl
                    cfg = jl(r.config_json, {}) or {}
                    if isinstance(cfg, dict):
                        row_cfg = dict(GENRES.get(k, {}))
                        row_cfg.update(cfg)
                        row_cfg["label"] = r.label or k
                        row_cfg["aliases"] = jl(r.aliases, []) or []
            except Exception:
                row_cfg = None
            if row_cfg is not None:
                table = dict(table)
                table[k] = row_cfg
    for k, v in table.items():
        items.append({
            "key": k, "label": v.get("label", k),
            "aliases": v.get("aliases", []),
            "has_realm": bool(v.get("realm_dim")),
            "metrics": list((v.get("metrics") or {}).keys()),
            "style_hint": v.get("style_hint", ""),
            "builtin": 1 if k in GENRES else 0,
            "enabled": 0 if k in disabled else 1,
        })
    return items


def normalize_key(name: str | None, db=None) -> str:
    """把任意题材写法归一为规范 key（'都市' → 'urban'）；未命中返回 ''。

    统一入口：建书、PATCH theme、agent 工具都走这里，避免各处归一逻辑不一致。
    """
    if not name:
        return ""
    g = get_genre(name, db)
    if not g:
        return ""
    for k, v in effective_genres(db).items():
        if v is g:
            return k
    return ""


def normalize_keys(names, db=None, limit: int = 8) -> list[str]:
    """批量归一 + 去重 + 丢弃未知项（软标签用）。"""
    out: list[str] = []
    for x in (names or []):
        k = normalize_key(x, db)
        if k and k not in out:
            out.append(k)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "GENRES", "DEFAULT_GENRE", "get_genre", "get_genre_by_key",
    "metrics_for", "scan_patterns_for", "realm_dim_for", "realm_stops_for",
    "style_hints_for", "cover_anchors_for", "prompt_hints_for", "catalog",
    "effective_genres", "invalidate_cache", "seed_builtin",
    "normalize_key", "normalize_keys",
    "export_packs", "import_packs", "EXPORT_SCHEMA",
]
