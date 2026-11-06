"""v0.9.0 无源梗（dangling meme）防护冒烟测试。

来源：WorkBuddy 写作实测第003章——「第二天我是被“饿”醒的。不是真饿，是系统
在我脑子里敲锣：【…报名截止了。】」。“饿”与后文无关且被亲手否定；AI 第一版
只把句子接顺没治本。本版本把该坑固化为四层能力：生成纪律 / 规则闸门 /
LLM 审计兜底 / 改稿纪律。

覆盖：
1. gates.check_dangling_meme：自拆台命中、无落点命中、有落点放行、表外词不误报；
2. run_gates 集成：generic/tomato 均常开，warn 不阻断（passed 仍 True）；
3. 审计：meme_setups 预筛、规则层并入叙事健康度、_rule_report 扣分、
   content_audit.md 含第④类无源梗定义与 meme_setups 变量；
4. 生成纪律：chapter_write.md 含无源梗禁止项；agent SYSTEM_PROMPT 含
   "先判根因"改稿纪律与玩梗落点硬约束；
5. 版本号 0.9.0。

运行：python tests/v090_dangling_meme.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v090_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail and not cond else ""))


from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.models import Book  # noqa: E402
from apps.api import audit  # noqa: E402
from apps.api.orchestrator import gates  # noqa: E402

init_db()

# ---------- 1. check_dangling_meme 单元 ----------
print("\n[1] check_dangling_meme 单元")

# 记录里的病例原句：被"饿"醒 + 不是真饿 + 系统台词与饿无关
CASE_RAW = ("第二天我是被“饿”醒的。\n\n不是真饿，是系统在我脑子里敲锣："
            "【崽啊，辰时三刻了，再不去青云宗，报名就截止了。】")
# AI 第一版的"表面通顺"修法：句子接顺了，"饿"仍然悬空（用户判定仍不合格）
CASE_PATCHED = ("第二天我是被“饿”醒的。\n\n不是真饿，是被系统吵醒的。它在我脑子里敲锣："
                "【崽啊，辰时三刻了，再不去青云宗，报名就截止了。】")
# 最终版：删掉无源梗，陈述独立成立
CASE_FIXED = "第二天我是被系统吵醒的。\n\n它在我脑子里敲锣：【崽啊，辰时三刻了，再不去青云宗，报名就截止了。】"
# 有落点：后文同义场呼应（咕咕/干粮）
CASE_LANDED = "我是被“饿”醒的。肚子咕咕叫了两声，我摸出半块干粮啃了，又灌了半碗凉粥。"
# 有落点：引号词本词在否定之外复现
CASE_ECHO = "他是被“吓”醒的。吓他的是窗外那只猫，一跳三尺高。"
# 表外词：常见揭晓句式，规则层不猜、不误报
CASE_OUTSIDE = "钱包被“偷”了。不是偷，是借。借的人说明日子就还。"
# 无引号的正常"被困"，不触发
CASE_PLAIN = "他被困在阵法里三天三夜，灵力早已枯竭。"

rep = gates.GateReport()
gates.check_dangling_meme(CASE_RAW, rep)
dm = [i for i in rep.issues if i.rule == "无源梗"]
check("病例原句命中无源梗", len(dm) == 1, str([i.rule for i in rep.issues]))
check("命中信息含'亲手否定'", dm and "亲手否定" in dm[0].message, dm and dm[0].message)
check("span 含'饿'引号片段", dm and "饿" in dm[0].span, dm and dm[0].span)
check("warn 级（不阻断）", dm and dm[0].level == "warn")
check("stats 记录 dangling_meme", rep.stats.get("dangling_meme") == 1, str(rep.stats))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_PATCHED, rep)
check("表面通顺版仍命中（只接顺没治本）", any(i.rule == "无源梗" for i in rep.issues),
      str([i.rule for i in rep.issues]))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_FIXED, rep)
check("最终版放行（删梗后独立成立）", not rep.issues and rep.stats.get("dangling_meme") == 0,
      str(rep.issues))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_LANDED, rep)
check("同义场落点放行（咕咕/干粮）", not rep.issues, str(rep.issues))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_ECHO, rep)
check("本词复现落点放行", not rep.issues, str(rep.issues))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_OUTSIDE, rep)
check("表外词不误报（被'偷'了→不是偷是借）", not rep.issues, str(rep.issues))

rep = gates.GateReport()
gates.check_dangling_meme(CASE_PLAIN, rep)
check("无引号正常句不触发", not rep.issues, str(rep.issues))

rep = gates.GateReport()
gates.check_dangling_meme("我被“困”住了。此处风景如画，实在是妙。", rep)
check("表内词无落点也命中（困+无呼应）", any(i.rule == "无源梗" for i in rep.issues),
      str([i.rule for i in rep.issues]))

# ---------- 2. run_gates 集成 ----------
print("\n[2] run_gates 集成")
tomato = Book(id="bk_v090_t", title="测试", platform="tomato")
generic = Book(id="bk_v090_g", title="测试", platform="generic")
db = SessionLocal()
db.add(tomato); db.add(generic); db.commit()

body = "# 第003章 全城笑柄去报名\n\n" + CASE_RAW + "\n\n" + "\n\n".join(["后续内容，一切如常。"] * 200)
r_t = gates.run_gates(body, tomato, None)
r_g = gates.run_gates(body, generic, None)
check("tomato 书触发无源梗", any(i.rule == "无源梗" for i in r_t.issues),
      str([i.rule for i in r_t.issues]))
check("generic 书同样触发（通用纪律，非平台专属）", any(i.rule == "无源梗" for i in r_g.issues),
      str([i.rule for i in r_g.issues]))
check("warn 不阻断：generic passed 仍 True", r_g.passed is True,
      str([(i.rule, i.level) for i in r_g.issues]))
check("warn 不阻断：tomato passed 仍 True（无源梗仅 warn）", r_t.passed is True,
      str([(i.rule, i.level) for i in r_t.issues]))
check("无源梗恒为 warn 级", all(i.level == "warn" for i in r_t.issues + r_g.issues
                              if i.rule == "无源梗"))
check("stats 透出 dangling_meme", r_g.stats.get("dangling_meme") == 1, str(r_g.stats))

clean = "# 第001章 开头\n\n" + CASE_FIXED + "\n\n" + "后续内容。" * 200
r_c = gates.run_gates(clean, generic, None)
check("干净正文不误报", not any(i.rule == "无源梗" for i in r_c.issues),
      str([i.rule for i in r_c.issues]))

# ---------- 3. 审计兜底 ----------
print("\n[3] 审计兜底")
spans = audit._meme_setup_spans(CASE_RAW)
check("玩梗预筛抓到 setup 片段", len(spans) >= 1 and "饿" in spans[0], str(spans))
check("无 setup 返回空", audit._meme_setup_spans(CASE_FIXED) == [], str(audit._meme_setup_spans(CASE_FIXED)))

book = Book(id="bk_v090_a", title="测试", platform="generic")
items = audit._rule_audit(CASE_RAW, book, None, [], ch_idx=3)
dm_items = [i for i in items if i["dim"] == "叙事健康度" and "无源梗" in i["message"]]
check("规则审计并入叙事健康度", len(dm_items) >= 1, str(items))

rr = audit._rule_report(CASE_RAW, book, None, [], ch_idx=3)
nh = next(d for d in rr["dimensions"] if d["name"] == "叙事健康度")
check("_rule_report 叙事健康度被扣分", nh["score"] < 100 and nh["issues"], str(nh["score"]))

tpl = (ROOT / "apps/api/skills/packs/content_audit.md").read_text(encoding="utf-8")
check("审计模板含无源梗定义", "无源梗/空转梗" in tpl)
check("审计模板含自拆台示例", "被‘X’醒→不是真X" in tpl or "被“X”醒→不是真X" in tpl)
check("审计模板含 meme_setups 变量", "{{meme_setups}}" in tpl and "meme_setups" in tpl.split("---")[1])
check("审计模板七维权重含 0.10", "叙事健康度 0.10" in tpl)
check("DIMENSIONS 仍七维", len(audit.DIMENSIONS) == 7 and audit.DIMENSIONS[-1] == "叙事健康度")
check("权重和=1.0", abs(round(sum(audit.WEIGHTS.values()), 6) - 1.0) < 1e-6)

# ---------- 4. 生成与改稿纪律 ----------
print("\n[4] 生成与改稿纪律")
cw = (ROOT / "apps/api/skills/packs/chapter_write.md").read_text(encoding="utf-8")
check("chapter_write 含无源梗禁止", "无源梗" in cw and "自拆台" in cw)
check("chapter_write 承节点提落点", "笑点与梗必须在后文有落点" in cw)

agent_src = (ROOT / "apps/api/agent.py").read_text(encoding="utf-8")
check("SYSTEM_PROMPT 含玩梗落点硬约束", "玩梗必须有落点" in agent_src)
check("SYSTEM_PROMPT 含先判根因改稿纪律", "先判根因" in agent_src and "表面通顺不等于逻辑成立" in agent_src)

# ---------- 5. 版本号 ----------
print("\n[5] 版本号")
main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
check("main.py version=0.9.0", 'version="0.9.0"' in main_src)

db.close()
print(f"\n===== PASS {len(PASS)} / FAIL {len(FAIL)} =====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
