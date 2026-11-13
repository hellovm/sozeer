"""小说封面生成：底图（AI 生成，无文字）+ 排版叠加（PIL 真字体渲染）。

为什么不让图片模型直接画带字的封面：主流扩散模型对中文/长文本渲染极不稳定，
常见"字多了笔画""乱码方块""伪汉字"。本模块把职责切开：
  1. gen_image_*  只生成"无文字的封面底图"（prompt 里明确禁文字）
  2. cover.py     用 PIL + Noto CJK 把书名/作者/类型徽章排版叠加，
                  文字 100% 正确，风格可换字体/竖排/描边/烫金效果
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from apps.api.config import BOOKS_DIR

# 字体候选：Noto Serif CJK（衬线，适合仙侠/古风）→ Noto Sans CJK（兜底）
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    # 常见变体位置（不同发行版/容器字体落点不同，逐一探测，命中即用）
    "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Bold.otf",
]

# Windows 字体（部署在 Windows 或 Wine 环境下 Noto 常缺失，必须覆盖）
_FONT_CANDIDATES_WIN = [
    "C:/Windows/Fonts/msyhbd.ttc",          # 微软雅黑 Bold（TTC 含多字重）
    "C:/Windows/Fonts/msyh.ttc",            # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",          # 黑体
    "C:/Windows/Fonts/simsun.ttc",          # 宋体
]

# macOS 字体（本地开发机）
_FONT_CANDIDATES_MAC = [
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

# 全部候选（按顺序探测：先 Linux Noto，再 Windows，再 macOS）
_FONT_ALL: list[str] = []
try:
    _FONT_ALL = list(_FONT_CANDIDATES)
    import platform as _pl
    if _pl.system() == "Windows":
        _FONT_ALL = _FONT_CANDIDATES_WIN + _FONT_ALL
    elif _pl.system() == "Darwin":
        _FONT_ALL = _FONT_CANDIDATES_MAC + _FONT_ALL
except Exception:
    _FONT_ALL = list(_FONT_CANDIDATES)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """多级字体降级：Noto CJK → Windows msyh/simhei → macOS PingFang →
    Pillow 内置 load_default。★ 绝不 raise —— 封面合成是兜底交付路径，
    找不到任何 CJK 字体时退回内置字体而非让整个封面 500（血泪：早期版本
    只查 3 个 Linux 路径，Docker/Windows 部署一没字体就崩）。"""
    for p in _FONT_ALL:
        if os.path.exists(p):
            try:
                # TTC 集合字体：index 0 通用，部分 Noto 集合 index 2 = SC 简体
                return ImageFont.truetype(p, size, index=0)
            except Exception:
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
    # 最后兜底：Pillow ≥10.1 支持 load_default(size)；老版本只接受无参。
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------- 底图 prompt 组稿
# v0.13.9：旧版 prompt 只拼 genre + 简介前 120 字（还错标成"基调"），图片模型盲画，
# 产出与小说内容无关的底图（系统流修仙被画成 office 场景）。现在由 LLM 读全部
# 设定集（类型/基调/梗概/简介/主角/世界观/用户补充）写具体画面 prompt。

NO_TEXT_SUFFIX = (
    "【严格禁止】：不要出现任何文字、字母、数字、汉字、符号、印章文字、题字、logo。"
    "Do NOT render any text, letters, numbers, glyphs, calligraphy or logos in the image."
)

# LLM 不可用时的题材视觉锚点（确定性兜底）
_GENRE_ANCHORS = [
    (("仙侠", "修仙", "修真", "玄幻", "古风", "武侠", "历史", "宫廷"),
     "中国古风仙侠意境：山水云海、古典楼阁、法器灵光，古装人物"),
    (("科幻", "星际", "末世", "机甲", "赛博"),
     "未来感科幻场景，冷色调科技元素与光效"),
    (("悬疑", "推理", "灵异", "惊悚", "恐怖"),
     "昏暗悬疑氛围，冷暖对比强光影"),
    (("都市", "现代", "言情", "婚恋", "职场"),
     "现代都市景观或精致室内场景，现代装束人物"),
    (("游戏", "电竞", "网游"),
     "虚拟与现实交错的场景，霓虹与数据流光效"),
]


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```$", "", s)
    return s.strip()


def _protagonist(db, book_id: str):
    from sqlalchemy import select
    from apps.api.models import Character
    # 手动录入的人物优先（自动识别的常是配角），再按首现章节排
    return db.scalar(select(Character).where(
        Character.book_id == book_id, Character.is_auto == 0)
        .order_by(Character.first_chapter)) \
        or db.scalar(select(Character).where(
            Character.book_id == book_id).order_by(Character.first_chapter))


def _cover_anchor_for(genre_txt: str, db=None) -> str:
    """题材文本 → 封面视觉锚点。

    v0.15 题材解耦：优先走题材包（genres.cover_anchors_for），
    未命中/不可用时回退内置 _GENRE_ANCHORS（**行为与改前一致**）。
    """
    s = genre_txt or ""
    try:
        from apps.api import genres
        anchors = genres.cover_anchors_for(s, [], db)
        if anchors:
            return anchors[0]
    except Exception:
        pass
    for keys, v in _GENRE_ANCHORS:
        if any(k in s for k in keys):
            return v
    return ""


def _fallback_base_prompt(book, meta: dict, protag) -> str:
    """无 LLM 时的确定性底图 prompt：题材锚点 + 主角外貌 + 基调。"""
    genre_txt = f"{book.genre or ''} {' '.join(meta.get('tags') or [])}"
    visual = _cover_anchor_for(genre_txt) or "中国古风仙侠意境：山水云海、古典楼阁、法器灵光，古装人物"
    parts = ["小说封面背景插画，竖版 2:3", visual]
    if protag and (protag.profile or "").strip():
        parts.append(f"主角形象：{(protag.profile or '')[:100]}")
    tone = (meta.get("tone") or "").strip()
    if tone:
        parts.append(f"整体氛围：{tone}")
    parts.append("主体置于画面中下部，上方与下方留出简洁空间供后期排版书名")
    return "，".join(x for x in parts if x) + "。" + NO_TEXT_SUFFIX


async def build_base_prompt(db, book, hint: str = "") -> str:
    """根据书籍设定集组稿底图 prompt。LLM 优先，失败静默走确定性兜底，绝不抛异常。"""
    from sqlalchemy import select
    from apps.api.models import WorldSetting, Setting
    from apps.api import llm
    from apps.api.orchestrator import graph as gen_graph

    try:
        from apps.api.agent import META_KEY
        row = db.get(Setting, f"book:{book.id}:{META_KEY}")
        meta = json.loads(row.value) if row and row.value else {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    protag = _protagonist(db, book.id)
    m = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default")
    if not m:
        return _fallback_base_prompt(book, meta, protag)

    ws = db.scalars(select(WorldSetting).where(
        WorldSetting.book_id == book.id).limit(8)).all()
    worlds_txt = "\n".join(f"- 【{w.category}】{w.key}：{(w.value or '')[:80]}"
                           for w in ws) or "（未设定）"

    lines = [
        f"【书名】{book.title}",
        f"【类型】{book.genre or '（未设定）'}",
    ]
    if meta.get("tone"):
        lines.append(f"【基调】{meta['tone']}")
    if meta.get("logline"):
        lines.append(f"【一句话梗概】{meta['logline']}")
    if meta.get("tags"):
        lines.append(f"【标签】{'、'.join(meta['tags'][:8])}")
    syn = (book.synopsis or "").strip()
    if syn:
        lines.append(f"【简介】{syn[:500]}")
    if meta.get("selling_points"):
        lines.append(f"【核心卖点】{'；'.join(meta['selling_points'][:3])}")
    if protag:
        lines.append(f"【主角】{protag.name}"
                     + (f"：{(protag.profile or '')[:200]}" if protag.profile else ""))
    lines.append(f"【世界观要点】\n{worlds_txt[:700]}")
    if hint.strip():
        lines.append(f"【画面要求（用户指定，优先遵循）】{hint.strip()[:200]}")

    sys_prompt = (
        "你是网文封面美术指导，为「无文字封面底图」写图像生成提示词。\n"
        "根据下方小说信息，构想一个最能代表这本书的具体画面（一个场景、一个瞬间）。\n"
        "硬性要求：\n"
        "1. 只描述画面本身：主体（人物/物件）+ 环境 + 光线 + 色调 + 氛围，60-150字，一段话。\n"
        "2. 人物须贴合其外貌设定；时代背景必须与题材一致、不得混搭（古风题材绝不出现现代物品，"
        "都市题材绝不出现古风元素；『系统』这类抽象概念用画面隐喻表达而不是现代电子屏幕）。\n"
        "3. 竖版构图：主体放在画面中下部，上方与下方留出简洁空间（后期要排书名与宣传语）。\n"
        "4. 禁止出现任何与文字、书法、符号、logo 相关的描述。\n"
        "5. 只输出提示词本身，不要解释、不要 markdown、不要引号。")
    try:
        r = await llm.complete(m, [{"role": "user", "content":
                                    sys_prompt + "\n\n" + "\n".join(lines)}],
                               temperature=0.7, timeout=120)
        txt = _strip_fence(r.text or "")
        if r.error or len(txt) < 20:
            return _fallback_base_prompt(book, meta, protag)
        return txt[:400].rstrip("。，；、 ") + "。" + NO_TEXT_SUFFIX
    except Exception:
        return _fallback_base_prompt(book, meta, protag)


def _fallback_poster_prompt(book, project, meta: dict, protag) -> str:
    """无 LLM 时的确定性短剧海报 prompt：题材锚点 + 画风串 + 竖屏 9:16。"""
    genre_txt = f"{book.genre or ''} {' '.join(meta.get('tags') or [])}"
    visual = _cover_anchor_for(genre_txt) or "中国古风仙侠意境：山水云海、古典楼阁、法器灵光，古装人物"
    parts = ["短剧竖屏海报背景插画，竖版 9:16", visual]
    style = (getattr(project, "style", "") or "").strip()
    if style:
        parts.append(f"画风基调：{style[:80]}")
    if protag and (protag.profile or "").strip():
        parts.append(f"主角形象：{(protag.profile or '')[:100]}")
    parts.append("主体置于画面中下部，上方与下方留出简洁空间供后期排版剧名与宣传语")
    return "，".join(x for x in parts if x) + "。" + NO_TEXT_SUFFIX


async def build_poster_prompt(db, book, project, hint: str = "") -> str:
    """短剧海报底图 prompt（竖屏 9:16）。LLM 优先，失败静默走确定性兜底，绝不抛异常。

    ★ v1.2.0 新增：与 build_base_prompt 同骨架，差异在于——
      · 画幅 9:16（短视频/抖音竖屏，而非书封 2:3）；
      · 引入短剧项目的 style（画风串）作为主视觉基调；
      · 标题位用 project.title（空则回落 book.title）。
    """
    from sqlalchemy import select
    from apps.api.models import WorldSetting, Setting
    from apps.api import llm
    from apps.api.orchestrator import graph as gen_graph

    try:
        from apps.api.agent import META_KEY
        row = db.get(Setting, f"book:{book.id}:{META_KEY}")
        meta = json.loads(row.value) if row and row.value else {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    protag = _protagonist(db, book.id)
    m = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default")
    if not m:
        return _fallback_poster_prompt(book, project, meta, protag)

    ws = db.scalars(select(WorldSetting).where(
        WorldSetting.book_id == book.id).limit(6)).all()
    worlds_txt = "\n".join(f"- 【{w.category}】{w.key}：{(w.value or '')[:80]}"
                           for w in ws) or "（未设定）"

    lines = [
        f"【剧名】{getattr(project, 'title', '') or book.title}",
        f"【类型】{book.genre or '（未设定）'}",
        f"【画风】{(getattr(project, 'style', '') or '').strip()[:120] or '（未设定）'}",
    ]
    if meta.get("logline"):
        lines.append(f"【一句话梗概】{meta['logline']}")
    syn = (book.synopsis or "").strip()
    if syn:
        lines.append(f"【简介】{syn[:400]}")
    if protag:
        lines.append(f"【主角】{protag.name}"
                     + (f"：{(protag.profile or '')[:200]}" if protag.profile else ""))
    lines.append(f"【世界观要点】\n{worlds_txt[:500]}")
    if hint.strip():
        lines.append(f"【画面要求（用户指定，优先遵循）】{hint.strip()[:200]}")

    sys_prompt = (
        "你是短剧海报美术指导，为「无文字竖屏海报底图」写图像生成提示词。\n"
        "根据下方短剧信息，构想一个最能代表这部剧的海报画面（一个场景、一个瞬间）。\n"
        "硬性要求：\n"
        "1. 只描述画面本身：主体（人物/物件）+ 环境 + 光线 + 色调 + 氛围，60-150字，一段话。\n"
        "2. 严格遵循【画风】基调（若已设定）；人物外貌贴合设定，时代/题材一致不混搭。\n"
        "3. 竖版 9:16 构图：主体放在画面中下部，上方与下方留出简洁空间（后期排剧名与宣传语）。\n"
        "4. 禁止出现任何与文字、书法、符号、logo、UI、字幕 相关的描述。\n"
        "5. 只输出提示词本身，不要解释、不要 markdown、不要引号。")
    try:
        r = await llm.complete(m, [{"role": "user", "content":
                                    sys_prompt + "\n\n" + "\n".join(lines)}],
                               temperature=0.7, timeout=120)
        txt = _strip_fence(r.text or "")
        if r.error or len(txt) < 20:
            return _fallback_poster_prompt(book, project, meta, protag)
        return txt[:400].rstrip("。，；、 ") + "。" + NO_TEXT_SUFFIX
    except Exception:
        return _fallback_poster_prompt(book, project, meta, protag)


async def gen_base_image(db, prompt: str, size: str = "1024x1536",
                         book_id: str = "") -> bytes:
    """调系统配置的 image 模型生成无文字底图。

    ★ v1.2.0：统一走 `media.run_media`（它内部已做三件事，比旧的
      _load_media_model 自造查询更完整）：
        1. pick_model(db, "image") —— 系统"配置中心→角色 image"的模型 + 成本降级链
        2. 全 provider 路由（openai / comfyui / comfyui-workflow 模板键 / 云视频…）
        3. MediaCall 成本台账（成功失败都记，与短剧分镜出图同一口径，不再绕过记账）
      run_media 把产物落盘到书仓库 drama_media/，这里回读 bytes 供 PIL 合成。
      size 支持 2:3(1024x1536) / 9:16(1080x1920)；comfyui/openai 通道透传，
      其余 provider 忽略 size（各自默认）。
    """
    from apps.api import media
    r = await media.run_media(db, book_id, "image", prompt, size=size)
    if not r.get("ok"):
        raise RuntimeError(f"底图生成失败：{r.get('error')}")
    rel = r.get("path") or ""
    if not rel:
        raise RuntimeError("底图生成失败：模型返回空")
    from apps.api import gitrepo
    p = gitrepo.repo_path(book_id) / rel
    if not p.exists():
        raise RuntimeError(f"底图产物缺失：{rel}")
    return p.read_bytes()


# 封面/海报画布尺寸（W,H）。★ v1.2.0 新增 9:16（短剧竖屏海报）：
#   小说封面沿用书脊/电商惯例 2:3（1024x1536）；短剧海报贴短视频/抖音竖屏 9:16。
_CANVAS = {
    "2:3": (1024, 1536),    # 小说封面（默认）
    "9:16": (1080, 1920),   # 短剧竖屏海报
}
# 各画布请求的 AI 底图像素（交给图片模型的原生尺寸，compose 时再 cover-fit 居中裁剪）
_BASE_SIZE = {
    "2:3": "1024x1536",
    "9:16": "1080x1920",
}


def compose_cover(book_title: str, author: str = "", tagline: str = "",
                  base_png: bytes | None = None, style: str = "xianxia",
                  portrait: str = "2:3") -> bytes:
    """封面合成主入口。base_png 为空时生成纯渐变底（可先用占位模式预览排版）。

    style: xianxia(竖排右起·衬线) | modern(横排·无衬线·大字)
    portrait: 2:3(小说封面,默认) | 9:16(短剧竖屏海报)
    """
    W, H = _CANVAS.get(portrait, _CANVAS["2:3"])
    if base_png:
        img = Image.open(io.BytesIO(base_png)).convert("RGB")
        # cover-fit 居中裁剪（v0.13.9）：模型可能返回非 2:3 尺寸，
        # 硬拉伸会变形；按目标比例放大后居中裁剪，主体基本在中部不受损。
        scale = max(W / img.width, H / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
        left = (img.width - W) // 2
        top = (img.height - H) // 2
        img = img.crop((left, top, left + W, top + H))
    else:
        img = Image.new("RGB", (W, H), (24, 28, 44))
        d0 = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            d0.line([(0, y), (W, y)], fill=(int(24 + 30 * t), int(28 + 20 * t), int(44 + 60 * t)))

    # 底部渐暗压字带：保证任何底图上文字都可读
    overlay = Image.new("L", (W, H), 0)
    od = ImageDraw.Draw(overlay)
    band_h = int(H * 0.42)
    for i in range(band_h):
        od.line([(0, H - band_h + i), (W, H - band_h + i)], fill=int(200 * (i / band_h) ** 1.2))
    black = Image.new("RGB", (W, H), (10, 10, 18))
    img = Image.composite(black, img, overlay)

    draw = ImageDraw.Draw(img)

    title_color = (240, 232, 214)   # 暖白
    accent = (214, 178, 106)        # 鎏金

    def draw_text_stroke(pos, text, font, fill, stroke=6, stroke_fill=(20, 16, 10)):
        x, y = pos
        draw.text((x, y), text, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=stroke_fill)

    if style == "xianxia":
        # 竖排书名（右起），每字一行，带鎏金描边
        chars = list(book_title.replace(" ", ""))
        size = min(120, int((H * 0.52) / max(len(chars), 4)))
        f_title = _font(size)
        col_x = W - int(W * 0.14) - size
        y0 = int(H * 0.08)
        step = int(size * 1.14)
        for i, ch in enumerate(chars[:14]):
            bbox = draw.textbbox((0, 0), ch, font=f_title)
            cw = bbox[2] - bbox[0]
            draw_text_stroke((col_x + (size - cw) // 2 - bbox[0], y0 + i * step),
                             ch, f_title, title_color, stroke=7)
        # 印章式作者位（左下竖排小字）
        if author:
            f_auth = _font(int(size * 0.36))
            ax = col_x - int(size * 0.9)
            for i, ch in enumerate(author[:8]):
                draw.text((ax, y0 + int(size * 0.4) + i * int(size * 0.46)),
                          ch, font=f_auth, fill=(225, 210, 190))
        # 底部宣传语（横排）
        if tagline:
            f_tag = _font(34)
            tw = draw.textbbox((0, 0), tagline, font=f_tag)[2]
            draw_text_stroke(((W - tw) // 2, int(H * 0.86)), tagline, f_tag, accent, stroke=4)
    else:
        # modern：横排大字居中
        chars = book_title
        size = 110 if len(chars) <= 6 else (88 if len(chars) <= 9 else 66)
        f_title = _font(size)
        tw = draw.textbbox((0, 0), chars, font=f_title)[2]
        y = int(H * 0.72)
        draw_text_stroke(((W - tw) // 2, y), chars, f_title, title_color, stroke=8)
        if author:
            f_auth = _font(40)
            aw = draw.textbbox((0, 0), author, font=f_auth)[2]
            draw.text(((W - aw) // 2, y + int(size * 1.5)), author, font=f_auth,
                      fill=(220, 210, 200))
        if tagline:
            f_tag = _font(34)
            tw2 = draw.textbbox((0, 0), tagline, font=f_tag)[2]
            draw_text_stroke(((W - tw2) // 2, int(H * 0.13)), tagline, f_tag, accent, stroke=4)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def save_cover(book_id: str, png: bytes, filename: str = "cover.png") -> str:
    """落盘到书籍仓库 drama_media/{filename}，返回相对路径。

    filename 默认为 cover.png（小说封面）；短剧海报传 poster_{project_id}.png。
    ★ 校验 filename：只允许安全后缀白名单，防路径遍历（与 gitrepo.safe_book_id 同理）。
    """
    _OK = re.fullmatch(r"[A-Za-z0-9_.-]+", filename)
    if not _OK or ".." in filename or filename.startswith("/"):
        raise ValueError(f"非法的产物文件名：{filename!r}")
    d = Path(BOOKS_DIR) / book_id / "drama_media"
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(png)
    return f"{book_id}/drama_media/{filename}"


def poster_filename(project_id: str) -> str:
    """短剧项目海报文件名（项目维度，多项目互不覆盖）。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", project_id or "")[:40] or "p"
    return f"poster_{safe}.png"
