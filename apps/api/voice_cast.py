"""D19 音色资产化：角色声纹注册 + 音色克隆 TTS + 口型对齐时间轴。

## 为什么需要这一层（D18 第二轮实测结论）

LTX-2.5 的 i2v 原生台词**每个镜头独立抽一次音色**——实测跨镜头 F0 中位从
250Hz 漂到 163Hz（相对差 34.7%），同一角色在镜 A 是清亮女声、镜 B 变成
低沉男声。短剧里这是致命的：观众一秒出戏。

D14-r 已经把「哪些台词该走 TTS」路由清楚（`audio_route.py`），但 TTS 那一侧
此前是**哑的**——`run_media(..., voice="alloy")` 全场只有一个音色参数，
多角色对白全用同一个嗓子念，等于把音色漂移换了个形式重演。

本模块补齐这一环，职责有四：

1. **声纹档案**（`VoiceProfile`）：一个角色 ↔ 一套 TTS 参数（voice/rate/pitch/style）。
   可人工指定并锁定，也可自动分配。
2. **确定性分配**（`assign_voices`）：同一角色在任何镜头、任何时间点都拿到
   **同一个** voice。分配依据 = 角色名 hash（稳定）+ 性别/年龄分层池 + 冲突消解。
   这是「跨镜头一致」的技术保证——不依赖任何模型随机性。
3. **音色推断**（`infer_traits`）：从角色已有的 `voice`（口癖/说话方式）与
   `voice_style` 文本里抽线索（「苍老」「清冷」「少女」「浑厚」…），映射到
   音色池的分层，让自动分配不是纯随机而是「像这个人」。
4. **口型时间轴**（`build_lipsync_timeline`）：TTS 合成后拿到每行的音频时长，
   拼成一条全局时间轴，供 lipsync 后处理按帧对齐（谁在第几秒开口）。

## 音色池

池子是「结构性」的——只要求 provider 支持这些 voice 名。默认给 Edge-TTS /
OpenAI-compatible 网关都通用的中文音色名。用户可在模型中心的 tts 插槽里
换成自己网关支持的音色名（见 `VOICE_POOL` 注释）。

## 与既有代码的关系

- 上游：`audio_route.tts_lines(utterances)` 给出「该 TTS 的台词 + speaker」
- 本模块：speaker → VoiceProfile（查表 or 分配）
- 下游：`media.run_media(..., voice=..., tts_opts=...)` 逐行合成
- 无配置时：`resolve_profile()` 确定性回落到 default 池，**绝不报错**——
  音色是增益位，不能把配音主流程打挂（沿用 D14-r 的容错哲学）。
"""
from __future__ import annotations

import hashlib
import json
import re

# ============================================================ 音色池

# 音色池按 (性别, 年龄段) 分层。voice 名用主流中文 TTS 网关的通用命名。
# 网关不支持时会在合成时报错并回落 default——见 resolve_profile 的容错。
# 若你的网关用别的命名（如 Azure 的 zh-CN-XiaoxiaoNeural），改这里即可。
VOICE_POOL: dict[str, list[str]] = {
    # 男声
    "male_young":    ["yunxi", "yunjian", "yunyang"],
    "male_mature":   ["yunye", "yunxi", "yunfeng"],
    "male_old":      ["yunjian", "yunye", "yunfeng"],
    # 女声
    "female_young":  ["xiaoxiao", "xiaoyi", "xiaohan"],
    "female_mature": ["xiaomo", "xiaorui", "xiaoxiao"],
    "female_old":    ["xiaomo", "xiaorui", "xiaoshuang"],
    # 中性 / 旁白
    "narrator":      ["yunxi", "xiaoxiao", "yunjian"],
    # unknown 是「线索全无」的兜底层。生产实测：一个项目常有 3-5 个靠名字也
    # 判不出性别的角色（如「山羊胡供奉」「楚家管事」），池子太小必然撞声。
    # 这里放足 8 个，并让分配算法用 rate/pitch 微调拉开辨识度。
    "unknown":       ["yunxi", "xiaoxiao", "yunjian", "yunye",
                      "xiaoyi", "yunyang", "xiaomo", "yunfeng"],
}

# 默认（找不到任何线索时的兜底音色）。固定值保证确定性。
DEFAULT_VOICE = "yunxi"

# 语速/音高边界（TTS 网关普遍接受的范围）
RATE_MIN, RATE_MAX = 0.5, 2.0
PITCH_MIN, PITCH_MAX = -12.0, 12.0

# ============================================================ 线索词表

# 从角色描述文本里推断性别。命中即定；两边都命中则算冲突（返回 unknown）。
# 注意：只做「包含」匹配，不做上下文消歧——角色描述通常很短，够用了。
# 词汇面刻意覆盖仙侠/古装常见身份词（「道门长老」「剑客」「弟子」），
# 否则「苍老的道门长老」这类描述会因缺性别词而落 unknown 池（实测踩坑）。
_MALE_HINTS = ("男", "少年", "青年男子", "公子", "少爷", "少年郎", "老者", "老翁",
               "汉子", "男子", "夫君", "师兄", "师弟", "父亲", "爹", "兄",
               "剑客", "剑修", "刀客", "道士", "道人", "宗主", "掌门", "长老",
               "兄台", "爷爷", "公公", "伯父", "叔叔", "将军", "统领", "陛下",
               "太子", "王爷", "父亲", "丈夫", "儿郎", "壮汉", "大汉", "樵夫",
               "药童", "童子", "小厮", "仆从", "护卫", "侍卫", "修士", "散修")
_FEMALE_HINTS = ("女", "少女", "姑娘", "女子", "娘子", "夫人", "小姐", "师姐",
                 "师妹", "母亲", "娘", "姊", "姐", "妹", "丫鬟", "婢女", "侍女",
                 "道姑", "仙姑", "公主", "郡主", "婆", "姥姥", "奶奶", "婆婆",
                 "婆婆", "妇", "妃", "皇后", "贵妃", "嬷嬷", "女修", "女弟子",
                 "女神", "仙姬", "狐女", "妖女", "女童", "小女孩")

# 年龄段线索
_OLD_HINTS = ("苍老", "年迈", "老", "暮", "古稀", "花甲", "耄耋", "长者",
              "白发", "垂暮", "衰", "沧桑", "祖", "婆", "翁", "叟")
_CHILD_HINTS = ("幼", "童", "小孩", "稚", "孩", "小儿", "少年", "少女", "少男")

# 音色质感线索（映射到 rate/pitch 微调 + 池子倾向）
_QUALITY_HINTS = {
    "cold": ("清冷", "冷淡", "冰冷", "淡漠", "疏离", "高冷"),
    "warm": ("温和", "温润", "柔和", "温柔", "和煦", "亲切"),
    "deep": ("浑厚", "低沉", "磁", "沉声", "厚重", "雄浑"),
    "bright": ("清亮", "清脆", "明亮", "银铃", "清越", "爽朗"),
    "hoarse": ("沙哑", "嘶哑", "喑哑", "沧桑", "粗砺"),
    "noble": ("贵气", "雍容", "端庄", "威严", "凛然", "肃穆"),
    "cute": ("天真", "活泼", "俏皮", "娇憨", "软糯"),
}

# 旁白识别（speaker 为空或这些词 → narrator 池）
_NARRATOR_NAMES = ("", "旁白", "画外音", "narrator", "解说", "叙述者", "voiceover")

# ---------------------------------------------------------------- 姓名线索
# 生产环境实测：大量角色资产的 description / voice_style 是空的（只有名字），
# 此时全靠描述推断会全部落 unknown 池 → 多人撞声 + 少女配男声。
# 中文人名本身携带很强的性别/年龄信号，这一层是必要的兜底。
_NAME_FEMALE = ("丫头", "姑娘", "娘", "妹", "姐", "嫂", "姑", "婆", "奶", "婶",
                "妃", "姬", "婉", "娟", "妍", "妮", "珠", "玉簪", "凤", "兰",
                "芳", "霞", "玲", "丽", "倩", "婷", "薇", "蓉", "雪", "月",
                "清霜", "素心", "若兰", "紫", "红", "香", "娇", "媚", "媛",
                "娥", "妾", "氏", "女", "仙", "灵", "静", "怡", "欣", "雅")
_NAME_MALE = ("少主", "少爷", "公子", "老爷", "大爷", "大爷", "管事", "供奉",
              "长老", "宗主", "掌门", "师父", "师兄", "师弟", "汉子", "翁", "公",
              "伯", "叔", "哥", "郎", "壮", "勇", "刚", "强", "峰", "龙", "虎",
              "豹", "彪", "猛", "武", "剑", "刀", "勇", "军", "兵", "卒")
# 年龄线索
_NAME_OLD = ("老", "翁", "婆", "奶", "祖", "太", "叟", "公", "嬷嬷")
_NAME_CHILD = ("丫", "童", "娃", "小儿", "小", "幼")


def infer_traits_from_name(name: str) -> dict | None:
    """从角色名推断性别/年龄（仅在描述类线索缺失时作兜底）。

    返回 None 表示名字没有可用线索（此时交给调用方决定是否落 unknown）。
    """
    n = (name or "").strip()
    if not n:
        return None
    is_f = any(w in n for w in _NAME_FEMALE)
    is_m = any(w in n for w in _NAME_MALE)
    if not is_f and not is_m:
        return None
    gender = "female" if (is_f and not is_m) else ("male" if (is_m and not is_f) else "unknown")
    if gender == "unknown":
        return None
    age = "old" if any(w in n for w in _NAME_OLD) else (
        "child" if any(w in n for w in _NAME_CHILD) else "mature")
    pool = {"female": f"female_{'old' if age == 'old' else 'young' if age == 'child' else 'mature'}",
            "male": f"male_{'old' if age == 'old' else 'young' if age == 'child' else 'mature'}"}[gender]
    if pool not in VOICE_POOL:
        pool = "unknown"
    return {"gender": gender, "age": age, "quality": [], "pool": pool,
            "source": "name"}


# ============================================================ 数据结构

class VoiceProfile:
    """一个角色的声纹档案。

    字段：
      speaker    角色名（主键，跨镜头唯一）
      voice      TTS 音色名（voice 池里的一个）
      rate       语速倍率（1.0 = 原速）
      pitch      音高偏移（半音，0 = 原音）
      style      风格标签（provider 支持时透传，如 "calm"/"angry"）
      traits     推断出的特征 {"gender": ..., "age": ..., "quality": [...] }
      source     "manual"（人工锁定）/ "auto"（自动分配）/ "default"（回落）
      locked     人工锁定后自动分配不得覆盖
    """

    __slots__ = ("speaker", "voice", "rate", "pitch", "style",
                 "traits", "source", "locked")

    def __init__(self, speaker: str = "", voice: str = DEFAULT_VOICE,
                 rate: float = 1.0, pitch: float = 0.0, style: str = "",
                 traits: dict | None = None, source: str = "auto",
                 locked: bool = False):
        self.speaker = speaker or ""
        self.voice = voice or DEFAULT_VOICE
        self.rate = _clamp(rate, RATE_MIN, RATE_MAX)
        self.pitch = _clamp(pitch, PITCH_MIN, PITCH_MAX)
        self.style = style or ""
        self.traits = traits or {}
        self.source = source
        self.locked = bool(locked)

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {"speaker": self.speaker, "voice": self.voice,
                "rate": round(self.rate, 3), "pitch": round(self.pitch, 3),
                "style": self.style, "traits": self.traits,
                "source": self.source, "locked": self.locked}

    @classmethod
    def from_dict(cls, d: dict) -> "VoiceProfile":
        if not isinstance(d, dict):
            return cls()
        return cls(speaker=d.get("speaker", ""), voice=d.get("voice", DEFAULT_VOICE),
                   rate=_num(d.get("rate"), 1.0), pitch=_num(d.get("pitch"), 0.0),
                   style=d.get("style", ""), traits=d.get("traits") or {},
                   source=d.get("source", "auto"), locked=bool(d.get("locked")))

    def tts_kwargs(self) -> dict:
        """给 media.run_media 的 TTS 参数（provider 不认的字段应自行忽略）。"""
        out = {"voice": self.voice, "speed": round(self.rate, 3)}
        if self.pitch:
            out["pitch"] = round(self.pitch, 3)
        if self.style:
            out["style"] = self.style
        return out

    def __repr__(self):  # pragma: no cover
        return (f"<VoiceProfile {self.speaker!r} voice={self.voice!r} "
                f"rate={self.rate} pitch={self.pitch} src={self.source}>")


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo if lo > 0 else 0.0
    return max(lo, min(hi, v))


def _num(v, dflt):
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


# ============================================================ 特征推断

def infer_traits(*texts: str) -> dict:
    """从角色描述/口癖文本里推断声纹特征。

    返回 {"gender": "male"|"female"|"unknown", "age": "young"|"mature"|"old"|"child",
          "quality": [...], "pool": "male_young"|... }

    纯确定性：同样的输入永远得到同样的输出（这是跨镜头一致的前提）。
    """
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return {"gender": "unknown", "age": "mature", "quality": [],
                "pool": "unknown"}

    low = blob.lower()
    is_male = any(h in blob for h in _MALE_HINTS)
    is_female = any(h in blob for h in _FEMALE_HINTS)
    if is_male and not is_female:
        gender = "male"
    elif is_female and not is_male:
        gender = "female"
    else:
        # 都命中（描述里写了「少年」又写了「少女」）或都没命中 → 交给音色池的
        # unknown 层，分配算法仍会保证不撞声。
        gender = "unknown"

    is_old = any(h in blob for h in _OLD_HINTS)
    is_child = any(h in blob for h in _CHILD_HINTS)
    if is_child and not is_old:
        age = "child"
    elif is_old:
        age = "old"
    else:
        age = "mature"

    quality = [tag for tag, words in _QUALITY_HINTS.items()
               if any(w in blob for w in words)]

    # 池子选择：child 归到对应的 young 层（音色池没细分童声）
    if gender == "unknown":
        pool = "unknown"
    elif age == "old":
        pool = f"{gender}_old"
    elif age == "young" or age == "child":
        pool = f"{gender}_young"
    else:
        pool = f"{gender}_mature"
    if pool not in VOICE_POOL:
        pool = "unknown"

    _ = low  # 保留：未来可做英文线索
    return {"gender": gender, "age": age, "quality": quality, "pool": pool}


def traits_to_tts_tweaks(traits: dict) -> tuple[float, float, str]:
    """特征 → (rate, pitch, style) 微调。

    只做轻微微调（±0.15 语速、±3 半音），目的是让同池的两个角色听起来
    更可区分，而不是做夸张变声——夸张变声会破坏真人质感。
    """
    rate, pitch, style = 1.0, 0.0, ""
    age = (traits or {}).get("age")
    quality = (traits or {}).get("quality") or []
    if age == "old":
        rate, pitch = 0.9, -2.5
    elif age == "child":
        rate, pitch = 1.12, 3.0
    elif age == "young":
        rate, pitch = 1.03, 1.0

    if "deep" in quality:
        pitch -= 2.0
    if "bright" in quality:
        pitch += 1.5
    if "hoarse" in quality:
        pitch -= 1.0
        rate = round(rate * 0.96, 3)
    if "cold" in quality:
        rate = round(rate * 0.97, 3)
        style = "calm"
    elif "warm" in quality:
        style = "gentle"
    elif "noble" in quality:
        style = "serious"
    elif "cute" in quality:
        rate = round(rate * 1.05, 3)

    return round(rate, 3), round(pitch, 3), style


# ============================================================ 确定性分配

def _stable_index(speaker: str, salt: str, n: int) -> int:
    """角色名 + 盐 → 稳定下标。用 md5 而不是内置 hash()——内置 hash 带
    进程随机种子（PYTHONHASHSEED），跨进程重启会变，那样跨镜头的「一致」
    就只在同一次运行内成立，重启后又换声。这是必须避开的坑。
    """
    if n <= 0:
        return 0
    h = hashlib.md5(f"{salt}|{speaker}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % n


def resolve_profile(speaker: str, *texts: str,
                    explicit: dict | None = None,
                    taken: dict | None = None) -> VoiceProfile:
    """给一个角色解析出声纹档案。

    优先级：
      1. explicit（人工指定/已存库的档案）——直接采用，locked 时不可覆盖
      2. 自动：特征推断 → 池内确定性选 + 冲突消解

    taken: {voice_name: speaker} 已被占用的音色，用于避撞。
    """
    # ---- 人工/已存档案 ----
    if explicit and explicit.get("voice"):
        p = VoiceProfile.from_dict({**explicit, "speaker": speaker})
        # 补全 traits（档案里没存过时现算，保证展示信息完整）
        if not p.traits:
            p.traits = infer_traits(*texts)
        if p.source in ("", None):
            p.source = "manual" if p.locked else "auto"
        return p

    # ---- 自动分配 ----
    traits = infer_traits(*texts)
    # 描述类线索缺失（生产环境常见：资产只有名字）→ 用姓名兜底
    if traits.get("pool") == "unknown":
        nt = infer_traits_from_name(speaker)
        if nt:
            traits = nt
    pool_name = traits["pool"]
    pool = VOICE_POOL.get(pool_name) or VOICE_POOL["unknown"]
    rate, pitch, style = traits_to_tts_tweaks(traits)

    taken = taken or {}
    voice = None
    # 第一轮：池内按确定性 hash 起步，线性探测第一个没被占的
    start = _stable_index(speaker, pool_name, len(pool))
    for i in range(len(pool)):
        cand = pool[(start + i) % len(pool)]
        if cand not in taken or taken.get(cand) == speaker:
            voice = cand
            break
    # 第二轮：本池全被占 → 放宽到全局池（宁可两角色同池不同音高，也不报错）
    if voice is None:
        for cand in VOICE_POOL["unknown"] + VOICE_POOL["narrator"] + [DEFAULT_VOICE]:
            if cand not in taken or taken.get(cand) == speaker:
                voice = cand
                break
    if voice is None:
        # 第三轮：真的全占满了（角色数 > 池子容量）。复用音色，但用 pitch
        # 偏移把两人分开——同音色不同音高在对话里仍可辨识，远好过报错。
        reused = VOICE_POOL["unknown"] + VOICE_POOL["narrator"]
        voice = reused[_stable_index(speaker, "reuse", len(reused))]
        pitch = round(pitch + (6.0 if _stable_index(speaker, "dir", 2) else -6.0), 3)

    return VoiceProfile(speaker=speaker, voice=voice, rate=rate, pitch=pitch,
                        style=style, traits=traits, source="auto", locked=False)


def assign_voices(speakers: list[str], *,
                  hints: dict | None = None,
                  existing: dict | None = None) -> dict[str, VoiceProfile]:
    """给一组角色分配声纹，保证互不撞声且跨调用稳定。

    参数：
      speakers  角色名列表（顺序无关——结果与顺序无关，这是「稳定」的一部分）
      hints     {角色名: 描述文本}，用于特征推断
      existing  {角色名: 已存档案 dict}，人工锁定的会被原样保留

    返回 {角色名: VoiceProfile}

    关键性质（都有测试覆盖）：
      * 同一角色重复调用恒得同一 voice
      * 不同角色尽量不同 voice（池够大时保证不同）
      * 已被 locked 的档案不会被覆盖
      * 传入顺序不影响结果（内部先排序）
    """
    hints = hints or {}
    existing = existing or {}
    out: dict[str, VoiceProfile] = {}
    taken: dict[str, str] = {}

    # 先处理已有锁定档案（它们优先占位）
    order = sorted(set(s or "" for s in speakers))
    for sp in order:
        ex = existing.get(sp)
        if ex and ex.get("voice") and (ex.get("locked") or ex.get("source") == "manual"):
            prof = resolve_profile(sp, hints.get(sp, ""), explicit=ex, taken=taken)
            out[sp] = prof
            taken.setdefault(prof.voice, sp)

    # 再自动分配其余的
    for sp in order:
        if sp in out:
            continue
        prof = resolve_profile(sp, hints.get(sp, ""),
                               explicit=existing.get(sp), taken=taken)
        out[sp] = prof
        taken.setdefault(prof.voice, sp)

    return out


# ============================================================ 与 utterances 对接

def speakers_in(utterances: list[dict]) -> list[str]:
    """从 utterances 里抽出所有 speaker（含旁白的空 speaker，归为 narrator 池）。"""
    out, seen = [], set()
    for u in (utterances or []):
        sp = (u.get("speaker") or "").strip()
        # 空 speaker 且是对白 → 用「未知角色」占位；旁白走 narrator
        key = sp
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def collect_hints(utterances: list[dict]) -> dict:
    """从 utterances 里给每个 speaker 收集线索文本（把它说过的台词当语气线索）。

    台词本身能反映性格（「哼，区区鼠辈」 vs「师兄，我怕」），拿它做
    quality 线索是免费且有效的。

    ⚠️ 台词**不能**用来判性别——台词里常出现称呼别人的词（牧云谣对陈长安
    说「师兄」，若把「师兄」当她的性别线索就会判成 male 或 male+female 冲突）。
    因此本函数的产出只作 quality 补充，见 cast_for_project 里的分层使用。
    """
    hints: dict[str, list[str]] = {}
    for u in (utterances or []):
        sp = (u.get("speaker") or "").strip()
        txt = (u.get("text") or "").strip()
        if txt:
            hints.setdefault(sp, []).append(txt)
    return {k: " ".join(v)[:300] for k, v in hints.items()}


def infer_traits_from_desc(*descs: str) -> dict:
    """只从「角色描述类」文本推断特征（性别/年龄），忽略台词。

    与 infer_traits 的区别：本函数用于**权威**线索源（资产 description /
    voice_style / 人设 profile），台词不参与，避免「师兄」「师妹」这类
    称呼词污染性别判断（实测踩坑：牧云谣的台词含「师兄」→ 性别判成冲突）。
    """
    return infer_traits(*descs)


def quality_hints_from_lines(speaker: str, utterances: list[dict]) -> list[str]:
    """从该角色说过的台词里提 quality 线索（只补语气，不碰性别/年龄）。"""
    blob = " ".join((u.get("text") or "")
                    for u in (utterances or [])
                    if (u.get("speaker") or "").strip() == speaker)[:300]
    if not blob:
        return []
    return [tag for tag, words in _QUALITY_HINTS.items()
            if any(w in blob for w in words)]


def profile_for_speaker(speaker: str, cast: dict,
                        *texts: str) -> VoiceProfile:
    """查声纹；没在 cast 里就现解析一个（回落，不报错）。

    cast 既接受 {名: VoiceProfile}，也接受 {名: dict}（load_cast_from_db /
    cast_for_project 的产出经 JSON 化后是 dict）——两种形态都统一成
    VoiceProfile 返回，调用方不必关心来源。

    旁白（空 speaker / narrator 类）固定走 narrator 池且不受冲突消解影响——
    旁白只有一个声音，不参与角色撞声。
    """
    sp = (speaker or "").strip()
    hit = cast.get(sp) if cast else None
    if hit is not None:
        if isinstance(hit, VoiceProfile):
            return hit
        if isinstance(hit, dict):
            prof = VoiceProfile.from_dict({**hit, "speaker": hit.get("speaker") or sp})
            if not prof.traits:
                prof.traits = infer_traits(*texts)
            return prof
    if sp.lower() in _NARRATOR_NAMES:
        return VoiceProfile(speaker=sp or "旁白", voice=VOICE_POOL["narrator"][0],
                            traits={"gender": "unknown", "age": "mature",
                                    "quality": ["narrator"], "pool": "narrator"},
                            source="auto")
    return resolve_profile(sp or "未知", *texts)


# ============================================================ 口型时间轴

def estimate_duration(text: str, rate: float = 1.0) -> float:
    """估算一句中文台词的朗读时长（秒）。

    公式：中文按 4.2 字/秒（TTS 常见实速），标点停顿按 0.18s 计，
    再除以语速倍率。真实时长应以 TTS 产物为准（见 build_lipsync_timeline
    接受 durations 参数）；这里只做无音频时的兜底估算，用于排期与预览。
    """
    t = (text or "").strip()
    if not t:
        return 0.0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", t))
    punct = len(re.findall(r"[，。！？、；：…—,\.!\?;:]", t))
    base = chinese / 4.2 + punct * 0.18
    return round(base / max(rate, 0.1), 2)


def build_lipsync_timeline(segments: list[dict], *,
                          gap: float = 0.25) -> dict:
    """把逐行 TTS 结果拼成全局时间轴，供 lipsync 按帧对齐。

    参数 segments: [{"speaker": "陈长安", "text": "...", "duration": 2.3,
                     "audio_path": "drama_media/audio_x.mp3",
                     "voice": "yunxi"}, ...]（duration 缺省则估算）

    返回 {
      "total": 8.4,
      "tracks": [{"index":0, "speaker":"陈长安", "start":0.0, "end":2.3,
                  "text":"...", "voice":"yunxi", "audio_path":"..."}, ...],
      "by_speaker": {"陈长安": [0, 2], ...}   # 该角色在 tracks 里的下标
    }
    """
    tracks, by_speaker = [], {}
    t = 0.0
    for i, seg in enumerate(segments or []):
        sp = (seg.get("speaker") or "").strip()
        dur = seg.get("duration")
        dur = float(dur) if dur else estimate_duration(seg.get("text", ""),
                                                        seg.get("rate", 1.0))
        start, end = round(t, 3), round(t + dur, 3)
        tracks.append({"index": i, "speaker": sp, "start": start, "end": end,
                       "text": seg.get("text", ""),
                       "voice": seg.get("voice", ""),
                       "audio_path": seg.get("audio_path", "")})
        by_speaker.setdefault(sp, []).append(i)
        t = end + gap
    total = round(tracks[-1]["end"], 3) if tracks else 0.0
    return {"total": total, "tracks": tracks, "by_speaker": by_speaker}


# ============================================================ 项目级装配

def cast_for_project(character_rows: list[dict],
                     utterances: list[dict] | None = None) -> dict[str, dict]:
    """项目级音色装配：把角色资产 + 分镜台词汇总成完整声纹表。

    参数：
      character_rows [{"name": "陈长安", "description": "...", "voice_style": "...",
                       "voice": "...", "profile": "...", "voice_profile": {...}}]
      utterances     全项目所有分镜的台词

    返回 {角色名: VoiceProfile.to_dict()}

    ⚠️ 三分层线索模型（重要，实测踩坑后定稿）：
      * **性别/年龄**只取自「角色描述类」字段（description / voice_style /
        profile / 小说侧 voice 口癖）——这些是角色的固有属性。
      * **台词不参与性别判断**：台词里常有称呼别人的词（牧云谣对陈长安说
        「师兄」，若当她的线索会判成 male 或性别冲突，自动分配就会把少女
        配成男声——实测就是这个 bug）。
      * **台词只补 quality**（语气/质感），因为它确实反映说话风格。
    """
    utts = utterances or []
    speakers, existing, desc_of = [], {}, {}

    for r in (character_rows or []):
        name = (r.get("name") or "").strip()
        if not name:
            continue
        speakers.append(name)
        # 权威线索：描述优先，其次人设/口癖。绝不含台词。
        desc_of[name] = " ".join(x for x in (r.get("description", ""),
                                             r.get("voice_style", ""),
                                             r.get("profile", ""),
                                             r.get("voice", "")) if x)[:600]
        if r.get("voice_profile"):
            existing[name] = r["voice_profile"]

    # 补漏：分镜里出现但资产表没有的角色（自动识别的人物）
    for sp in speakers_in(utts):
        if sp and sp not in speakers:
            speakers.append(sp)

    cast = assign_voices(speakers, hints=desc_of, existing=existing)

    # quality 补充：把台词里体现的语气特征并进 traits（不动 gender/age/pool）
    for sp, prof in cast.items():
        extra = quality_hints_from_lines(sp, utts)
        if extra:
            q = list(dict.fromkeys((prof.traits.get("quality") or []) + extra))
            prof.traits = {**prof.traits, "quality": q}

    return {k: v.to_dict() for k, v in cast.items()}


def build_scene_tts_jobs(utterances: list[dict], cast: dict,
                         mode: str = "quality") -> list[dict]:
    """把一场戏的 utterances 转成 TTS 合成作业列表（逐行一个 job）。

    依赖 audio_route 的路由（quality 档全 TTS / fast 档对白给 LTX），
    这里只负责把「该 TTS 的行」配上声纹与参数。

    返回 [{"speaker","text","voice","rate","pitch","style","tts_kwargs"}, ...]
    """
    from apps.api import audio_route as _ar
    lines = _ar.tts_lines(utterances or [], mode=mode)
    jobs = []
    for u in lines:
        sp = (u.get("speaker") or "").strip()
        prof = profile_for_speaker(sp, cast, u.get("text", ""))
        jobs.append({
            "speaker": sp,
            "text": (u.get("text") or "").strip(),
            "voice": prof.voice,
            "rate": prof.rate,
            "pitch": prof.pitch,
            "style": prof.style,
            "tts_kwargs": prof.tts_kwargs(),
        })
    return jobs


def load_cast_from_db(db, project, book_id: str = "") -> dict:
    """从库里读角色资产 + 项目全部分镜台词，装配声纹表。

    任何异常都返回 {}（音色是增益位，绝不阻塞配音流程）。
    注意：异常会以 warning 打到 stderr——早期版本静默吞掉，导致「声纹表为空」
    这类问题极难定位（实测踩坑：drama_assets 缺列时接口只是空表，看不出原因）。
    """
    try:
        from sqlalchemy import select
        from apps.api.models import DramaAsset, DramaScene, Character, jl
        rows = []
        assets = db.scalars(select(DramaAsset).where(
            DramaAsset.project_id == project.id,
            DramaAsset.kind == "character")).all()
        # 小说侧角色（口癖字段做线索）
        book_id = book_id or getattr(project, "book_id", "")
        chars = {}
        if book_id:
            for c in db.scalars(select(Character).where(
                    Character.book_id == book_id)).all():
                chars[c.name] = c
        for a in assets:
            base_name = re.sub(r"（[^）]*）$", "", a.name or "")  # 衍生形态归本体
            c = chars.get(base_name) or chars.get(a.name)
            rows.append({
                "name": base_name or a.name,
                "voice_style": a.voice_style or "",
                # description 是性别/年龄线索最富的字段（「青年剑客」「少女，仙门弟子」
                # 「苍老的道门长老」）——早期版本漏传它，导致 gender 全判 unknown、
                # 音色池全落 unknown 层（实测踩坑）。必须带上。
                "description": a.description or "",
                "voice_profile": _load_json(getattr(a, "voice_profile", "")),
                "voice": c.voice if c else "",
                "profile": c.profile if c else "",
            })
        # 所有分镜的台词（做线索 + 补漏）
        utts = []
        for s in db.scalars(select(DramaScene).where(
                DramaScene.project_id == project.id)).all():
            utts.extend(jl(s.utterances, []) or [])
        return cast_for_project(rows, utts)
    except Exception as e:
        import sys as _sys
        print(f"[voice_cast] load_cast_from_db 失败（静默降级为空表）："
              f"{type(e).__name__}: {e}", file=_sys.stderr, flush=True)
        return {}


def _load_json(s):
    if not s:
        return None
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        return None
