"""去 AI 腔引擎（v0.22.4 C4）。

★★★ 为什么这是核心竞争力：

  AI 写的小说，读者三章就能看出来。典型特征：
    · 排比句、"不是…而是…"、"仿佛…又似…" 密集
    · 每个动作都配一个情绪解释（如：他愤怒地握紧拳头）
    · 形容词堆叠（"深邃的眼眸"、"清冷的气质"）
    · 句长均匀（AI 偏好中等长度，人写会长短交错）
    · "的"字泛滥（书面腔）

  InkFlow 的做法：40+ 类规则库 + LLM 双层改写。
  本模块是**规则层**（确定性、零成本、可量化），
  LLM 改写层由 orchestrator 调用（见 rewrite_suggestions）。

================================ 设计 ================================

★ **规则分级**（严重度）：
    error  —— 明确 AI 腔，必须改（如"命运的齿轮开始转动"）
    warn   —— 疑似，建议改（如排比过密）
    info   —— 观察项（如句长均匀）

★ **每条规则带"改写建议"**：
  只报问题不给方案 = 用户还是不知道怎么改。

★ **命中率可量化**（验收要求）：
  `analyze()` 返回每类命中的次数与密度，
  可对比"改写前后"的改善幅度 → A/B 可验证。

================================ 与既有模块的关系 ================================

  gates.check_ai_flavor()   —— 已有的启发式打分（句长变异/的字密度）
  quality.sk_ai_tells()     —— 已有的词表检查
  本模块                    —— **整合 + 大幅扩充**，并给出分级与改写建议

★ 不重复造轮子：本模块**复用** gates 的启发式统计，在其上叠加规则库。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger("novelforge")


# ============================================================================
#  规则库（40+ 类）
# ============================================================================

@dataclass
class Rule:
    """一条去 AI 腔规则。"""
    key: str                    # 规则标识
    label: str                  # 中文名
    pattern: str                # 正则
    level: str                  # error | warn | info
    advice: str                 # 改写建议
    max_per_1k: float = 3.0     # 每千字允许次数（超过才报）


# ★ 十大类，共 46 条规则（advice 用单引号定界，内部可自由用中文引号）
RULES: list[Rule] = [
    # ---------------- 1. 陈词滥调（error：明确 AI 腔）----------------
    Rule('cliche_gear', '命运齿轮', r'命运的齿轮|冥冥之中|命中注定|宿命般', 'error',
         '删掉这类“旁白式”总结句，用具体事件让读者自己感受命运感'),
    Rule('cliche_time', '时间套话', r'时间仿佛(静止|凝固)|时光飞逝|岁月如梭|光阴似箭',
         'warn', '改为具体的时间标记（如“日头西斜”“过了三炷香”）'),
    Rule('cliche_air', '空气描写', r'空气中(弥漫|充斥)着|气氛(陡然|瞬间)?(凝固|凝重)',
         'warn', '用感官细节替代抽象氛围（气味 / 温度 / 声音）'),
    Rule('cliche_heart', '心潮澎湃', r'心潮(澎湃|起伏)|五味杂陈|百感交集|说不出的(感觉|滋味)',
         'warn', '直接写生理反应或动作（如“喉咙发紧”“转过身去”）'),
    Rule('cliche_eyes', '眼神套话', r'(目光|眼神|眸光)(一闪|闪烁|深邃|复杂)|眼底闪过一丝',
         'error', '眼神描写是重灾区。改为具体动作（如“他盯着地上那道裂缝”）'),
    Rule('cliche_smile', '笑容套话', r'嘴角(微微)?(勾起|上扬)|露出一抹(淡|微|冷)笑|淡淡一笑',
         'error', '四种笑法换着用是 AI 特征。写他为什么笑（如“他笑的时候眼睛没动”）'),
    Rule('cliche_aura', '气场套话', r'气场(强大|凛冽|十足)|压迫感(十足|扑面而来)|不怒自威',
         'warn', '改为旁人的具体反应（如“他进来后，说话声停了”）'),
    Rule('cliche_face', '面不改色', r'面不改色|波澜不惊|毫无波澜|不动声色',
         'warn', '这类“没反应”的描写信息量为零，删掉或改为微动作'),

    # ---------------- 2. 排比与对仗（warn：密度问题）----------------
    Rule('par_bujin', '不仅…而且', r'不仅.{0,8}而且', 'warn',
         'AI 爱用关联词。拆成两句，或用具体递进'),
    Rule('par_jiyou', '既…又…', r'既.{0,8}又.{0,8}(还|也)?', 'warn',
         '同上。中文写作少用关联词连接'),
    Rule('par_bushi', '不是…而是…', r'不是.{0,12}而是', 'warn',
         '这是最典型的 AI 句式。改陈述句'),
    Rule('par_fangfo', '仿佛…又似…', r'仿佛.{0,12}又(似|像)', 'warn',
         '删掉一个比喻，或改成直接描写'),
    Rule('par_rutong', '如同…一般', r'如同.{0,12}一般|犹如.{0,12}一般', 'warn',
         '“一般”冗余。改“像…那样”或直接比喻'),

    # ---------------- 3. 形容词堆叠（warn）----------------
    Rule('adj_deep', '深邃 / 清冷', r'(深邃|清冷|幽深|冷冽|凛冽)(的|地)?(眼眸|目光|气质|气息)',
         'warn', '形容词 + 名词的固定搭配是 AI 特征。用动作或对话体现气质'),
    Rule('adj_yi', '一抹 / 一丝', r'[一一]抹(微笑|笑意|冷意|异色)|[一一]丝(不易察觉|若有若无)',
         'warn', '“一抹 / 一丝”滥用。删掉或换更具体的量词'),
    Rule('adj_stack', '三重形容词', r'[\u4e00-\u9fff]{2}的[\u4e00-\u9fff]{2}的[\u4e00-\u9fff]{2}的',
         'warn', '连续三个“…的”是 AI 堆叠。最多留一个'),

    # ---------------- 4. 对话标签（error：直接影响阅读）----------------
    Rule('dlg_adverb', '副词 + 说道',
         r'[\u4e00-\u9fff]{1,3}地(说道|说|问道|答|笑道|冷冷地|沉声)', 'error',
         '对话标签不要带副词。用动作或上下文表现情绪'),
    Rule('dlg_explain', '解释性标签',
         r'(愤怒|高兴|悲伤|惊讶|无奈|苦涩)(地)?(说道|说|问道|回答)', 'error',
         '情绪不该写在标签里。让台词本身传达情绪'),
    Rule('dlg_asked', '反问道 / 质问道', r'(反问|质问|追问|沉声)道', 'warn',
         '这类标签过于刻意。直接“他说”或省略'),
    Rule('dlg_ellipsis', '省略号连用', r'…{3,}|。{3,}|！{3,}', 'warn',
         '标点连用是网络腔。用文字表达停顿'),

    # ---------------- 5. 叙述视角（warn：AI 爱越界）----------------
    Rule('pov_feel', '心理直述（他感到）', r'(他|她|我)(感到|觉得|意识到|察觉到)(一阵|一股)',
         'warn', '心理描写别用“感到”。直接写意识内容或身体反应'),
    Rule('pov_know', '全知判断', r'(显然|无疑|必然|注定|毫无疑问)', 'warn',
         '上帝视角的判断削弱代入感。删掉'),
    Rule('pov_explain', '作者解释', r'(这(意味着|说明|表示)|换句话说|也就是说)',
         'warn', '作者跳出来解释了。让读者自己推理'),

    # ---------------- 6. 节奏与句式（info：观察项）----------------
    Rule('pacing_then', '于是 / 然后 / 接着', r'(于是|然后|接着|随后|紧接着)', 'info',
         '连接词过密显得流水账。用事件自身逻辑衔接'),
    Rule('pacing_suddenly', '突然 / 忽然', r'(突然|忽然|猛然|霍然)', 'info',
         '“突然”用太多会钝化。留给真正的意外'),
    Rule('pacing_very', '十分 / 非常 / 极其', r'(十分|非常|极其|格外|异常)(的)?', 'info',
         '程度副词是弱表达。用具体的量或对比'),
    Rule('pacing_every', '每 / 都（全称）', r'每一次|所有的(人|事)|全都|无一例外', 'info',
         '全称判断要慎用，容易虚假'),
    Rule('pacing_gradually', '渐渐 / 缓缓', r'(渐渐|缓缓|慢慢|徐徐)', 'info',
         '慢动作描写过密会拖节奏'),

    # ---------------- 7. 情绪写法（error/warn）----------------
    Rule('emo_tell', '直陈情绪', r'(他|她|我)(很|非常|十分|特别)(愤怒|高兴|难过|害怕|紧张|兴奋)',
         'error', '直接说情绪是 AI 最典型特征。写身体反应（手心出汗 / 呼吸变快）'),
    Rule('emo_burst', '情绪爆发套话', r'(怒火|怒意|怒气)(中烧|滔天|冲天)|(心中|心底)一(阵|股)(暖流|寒意)',
         'error', '情绪爆发写滥了。用行为或对白表达'),
    Rule('emo_tears', '泪水描写', r'(泪水|眼泪)(夺眶而出|模糊了(视线|双眼))|泣不成声',
         'warn', '改为克制写法（他别过脸去 / 拿袖子擦了擦）'),
    Rule('emo_shiver', '身体反应套话', r'(浑身|全身)(一|猛地)(颤|震|僵)|如遭雷击|五雷轰顶',
         'warn', '这些反应被用滥了。找更贴身的细节'),

    # ---------------- 8. 场景与动作（warn）----------------
    Rule('act_swallow', '吞咽 / 颔首', r'(咽了(咽|口)口水|颔首|点了点头|微微颔首)',
         'info', '动作套话。换成更有信息量的动作'),
    Rule('act_turn', '转身 / 抬头', r'(缓缓|慢慢|猛地)(转身|抬头|低头|起身)',
         'info', '动作修饰词过多。直接“他转身”'),
    Rule('act_grip', '握紧拳头', r'(握紧|攥紧|捏紧)(了)?(拳头|双拳)|指节(泛白|发白)',
         'warn', '这是最被滥用的愤怒表达。换别的'),
    Rule('act_step', '后退一步', r'(后退|倒退)(了)?(一|半)步|身形(一|猛)顿|脚步(一|猛)(顿|滞)',
         'info', '同类动作重复率高，注意变化'),

    # ---------------- 9. 环境描写（warn/info）----------------
    Rule('env_moon', '月光/阳光套话', r'(皎洁|清冷|惨白)的月光|(温暖|金色)的阳光(洒|照)',
         'info', '自然景象描写套路化。写具体感受'),
    Rule('env_wind', '风套话', r'(一阵|呼啸的)(寒|冷|狂风)|风吹过|微风拂过',
         'info', '“风”是 AI 最爱的环境元素。慎用'),
    Rule('env_silence', '寂静描写', r'(寂静|沉默|安静)(得|的)(可怕|诡异|压抑)|针落可闻',
         'warn', '“寂静得可怕”是标准套话。写具体声音（钟摆声 / 远处犬吠）'),
    Rule('env_smell', '气味套话', r'(淡淡|浓郁|若有若无)的(香气|味道|气息)',
         'info', '气味描写要有来由（是什么东西散发的）'),

    # ---------------- 10. 结构与过渡（warn）----------------
    Rule('struct_summary', '段落总结句', r'(这(一|场)(幕|战|局)|就此|从此)(,)?',
         'warn', '章节内不要频繁"总结"。交给下一章'),
    Rule('struct_trans', '过渡句套话', r'(与此(同时|同时)|另一方面|不(知|觉)过了多久)',
         'warn', '过渡句是 AI 标记。用场景切换替代'),
    Rule('struct_recap', '重复前情', r'(回想起|想起(了)?(刚才|之前)|记得(那时|当初))',
         'info', '回顾前情要节制，读者记得'),
    Rule('struct_meta', '元叙述', r'(故事(要|得)从|说到这(里|儿)|言归正传)',
         'error', '作者跳出来讲故事了。直接进入情节'),
]

# 按 pattern 预编译（性能：一章几千字，逐条 compile 会慢 10 倍）
_COMPILED: list[tuple[Rule, re.Pattern]] = []


def _compiled_rules() -> list[tuple[Rule, re.Pattern]]:
    global _COMPILED
    if not _COMPILED:
        _COMPILED = [(r, re.compile(r.pattern)) for r in RULES]
    return _COMPILED


# ============================================================================
#  分析
# ============================================================================

SENT_SPLIT = re.compile(r"[。！？…]+")


@dataclass
class Hit:
    rule: Rule
    count: int
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"key": self.rule.key, "label": self.rule.label,
                "level": self.rule.level, "count": self.count,
                "advice": self.rule.advice, "samples": self.samples[:3]}


@dataclass
class Report:
    score: int = 100
    words: int = 0
    hits: list[Hit] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        by_level: dict[str, int] = {}
        for h in self.hits:
            by_level[h.rule.level] = by_level.get(h.rule.level, 0) + 1
        return {
            "score": self.score, "words": self.words,
            "hit_kinds": len(self.hits),
            "hit_total": sum(h.count for h in self.hits),
            "by_level": by_level,
            "hits": [h.to_dict() for h in self.hits],
            "stats": self.stats,
            "verdict": ("优秀" if self.score >= 85 else
                        "良好" if self.score >= 70 else
                        "一般" if self.score >= 55 else "需改写"),
        }


def analyze(text: str, *, chapter_words: int = 0) -> Report:
    """★ 去 AI 腔分析（核心入口）。

    ★ 密度归一化：每千字命中次数。原文越长，绝对命中数越多，
      所以必须按密度判定 —— 否则长章节永远"问题更多"。
    """
    t = text or ""
    words = chapter_words or len(t)
    rep = Report(words=words)
    if not t.strip():
        return rep

    per_1k = 1000.0 / max(1, words)
    for rule, rx in _compiled_rules():
        ms = list(rx.finditer(t))
        if not ms:
            continue
        density = len(ms) * per_1k
        # ★ 阈值：error 级宽松些（出现就该报），warn/info 看密度
        limit = rule.max_per_1k if rule.level != "error" else 1.0
        if density < limit:
            continue
        samples = []
        for m in ms[:3]:
            lo, hi = max(0, m.start() - 12), min(len(t), m.end() + 12)
            samples.append(t[lo:hi].replace("\n", " "))
        rep.hits.append(Hit(rule, len(ms), samples))

    # ★ 启发式统计（句长变异 / 的字密度）—— 复用 gates 的算法
    sents = [s.strip() for s in SENT_SPLIT.split(t) if len(s.strip()) > 1]
    n = max(1, len(sents))
    lens = [len(s) for s in sents]
    avg = sum(lens) / n if lens else 0
    var = (sum((l - avg) ** 2 for l in lens) / n) if lens else 0
    cv = (var ** 0.5) / max(1.0, avg) if avg else 0
    de_ratio = t.count("的") / max(1, len(t))
    rep.stats = {
        "sentences": n, "avg_sent_len": round(avg, 1),
        "sent_len_cv": round(cv, 3),
        "de_ratio": round(de_ratio, 4),
    }

    # 扣分
    score = 100.0
    for h in rep.hits:
        score -= {"error": 8, "warn": 3, "info": 1}[h.rule.level]
    if cv < 0.45:
        score -= 12      # 句长太均匀（AI 特征）
    if de_ratio > 0.055:
        score -= 8       # "的"过多（书面腔）
    if avg > 32:
        score -= 6       # 句子过长
    rep.score = max(0, int(score))
    return rep


def hit_stats(report: Report) -> dict:
    """★ 命中率统计（验收要求"命中率可量化"）。"""
    by_label = Counter()
    for h in report.hits:
        by_label[h.rule.label] += h.count
    per_1k = report.words and (sum(h.count for h in report.hits) * 1000.0 / report.words)
    return {
        "score": report.score,
        "kinds": len(report.hits),
        "total": sum(h.count for h in report.hits),
        "per_1k": round(per_1k or 0, 2),
        "top": [{"label": k, "count": v} for k, v in by_label.most_common(8)],
    }


# ============================================================================
#  改写建议
# ============================================================================

def rewrite_suggestions(report: Report, limit: int = 8) -> list[dict]:
    """★ 生成可执行的改写清单（按严重度排序）。

    ★ 为什么只给"建议"不给"自动改写"：
      自动改写风险高（可能改坏文意）。规则层只负责**指出问题 + 给方向**，
      真正的改写由 LLM 或人工完成（见 prompt_for_rewrite）。
    """
    order = {"error": 0, "warn": 1, "info": 2}
    items = sorted(report.hits, key=lambda h: (order[h.rule.level], -h.count))
    out = []
    for h in items[:limit]:
        out.append({
            "level": h.rule.level,
            "label": h.rule.label,
            "count": h.count,
            "advice": h.rule.advice,
            "samples": h.samples,
        })
    return out


def prompt_for_rewrite(text: str, report: Report, *, max_items: int = 8) -> str:
    """★ 生成 LLM 改写指令（规则层 → LLM 层的桥）。

    这就是 InkFlow "双层改写"的落地：
      规则层定位问题 → LLM 按清单改写 → 规则层复验
    """
    sug = rewrite_suggestions(report, max_items)
    if not sug:
        return ""
    lines = [
        "请改写下面的文字，重点解决以下 AI 腔问题（保持情节与人物不变）：",
    ]
    for i, s in enumerate(sug, 1):
        lines.append(f"{i}. 【{s['label']}】出现 {s['count']} 次 —— {s['advice']}")
    lines.append("")
    lines.append("要求：")
    lines.append("- 不要增加新的情节或人物")
    lines.append("- 长短句交错，减少形容词堆叠")
    lines.append("- 保留原有的对话内容与信息量")
    lines.append("")
    lines.append("原文：")
    lines.append(text[:6000])
    return "\n".join(lines)


def compare(before: str, after: str) -> dict:
    """★ A/B 对比（验收要求"产出质量优于单阶段"可验证）。"""
    rb = analyze(before)
    ra = analyze(after)
    return {
        "before": hit_stats(rb),
        "after": hit_stats(ra),
        "improvement": {
            "score_delta": ra.score - rb.score,
            "hit_delta": (sum(h.count for h in rb.hits)
                          - sum(h.count for h in ra.hits)),
            "kinds_delta": len(rb.hits) - len(ra.hits),
        },
    }


def rule_catalog() -> dict:
    """规则目录（后台展示 + 文档用）。"""
    by_level: dict[str, list] = {}
    for r in RULES:
        by_level.setdefault(r.level, []).append({
            "key": r.key, "label": r.label, "level": r.level,
            "advice": r.advice, "pattern": r.pattern})
    return {"total": len(RULES), "by_level": by_level,
            "levels": {"error": "必须改", "warn": "建议改", "info": "观察项"}}
