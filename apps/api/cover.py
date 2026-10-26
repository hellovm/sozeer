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
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            # TTC 集合字体：index 2 通常是 SC（简体）子字体
            try:
                return ImageFont.truetype(p, size, index=2)
            except Exception:
                return ImageFont.truetype(p, size)
    raise RuntimeError("未找到 CJK 字体，请安装 fonts-noto-cjk")


def _load_media_model(db, role: str):
    from sqlalchemy import select
    from apps.api.models import ModelProfile
    m = db.scalar(select(ModelProfile).where(ModelProfile.role == role,
                                             ModelProfile.enabled == 1)
                  .order_by(ModelProfile.is_default.desc())) \
        or db.scalar(select(ModelProfile).where(ModelProfile.role == role))
    return m


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


async def gen_base_image(db, prompt: str) -> bytes:
    """调 image 模型生成无文字底图（竖版 2:3）。复用 media 的 OpenAI 兼容/ComfyUI 双通道。"""
    from apps.api import media
    m = _load_media_model(db, "image")
    if not m:
        raise RuntimeError("未配置图像生成模型（配置中心 → 角色设为 image）")
    try:
        if m.provider == "comfyui":
            data = await media.gen_image_comfyui(m, prompt)
        else:
            data = await media.gen_image_openai(m, prompt, size="1024x1536")
    except media.MediaError as e:
        raise RuntimeError(f"底图生成失败：{e}")
    if not data:
        raise RuntimeError("底图生成失败：模型返回空")
    return data


def compose_cover(book_title: str, author: str = "", tagline: str = "",
                  base_png: bytes | None = None, style: str = "xianxia") -> bytes:
    """封面合成主入口。base_png 为空时生成纯渐变底（可先用占位模式预览排版）。

    style: xianxia(竖排右起·衬线) | modern(横排·无衬线·大字)
    """
    W, H = 1024, 1536
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


def save_cover(book_id: str, png: bytes) -> str:
    """落盘到书籍仓库 drama_media/cover.png，返回相对路径。"""
    d = Path(BOOKS_DIR) / book_id / "drama_media"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "cover.png"
    p.write_bytes(png)
    return f"{book_id}/drama_media/cover.png"
