"""v0.8.0 平台签约适配冒烟测试。

覆盖：
1. platform.get_platform：tomato 命中、未知名/空 → None；
2. platform.check_meta：书名导向词 error、简介导向 warn、红海句式、书名超长；
3. gates 新闸门：detect_pov / check_meme_density / check_templating /
   check_opening / check_quotes / to_curly_quotes；
4. run_gates 平台接入：tomato 书触发新闸门；generic 书零影响（不触发）；
   平台字数覆盖（book 未设定也按 2000-2200 卡）；
5. audit 七维含"叙事健康度"、权重和=1.0、content_audit.md 同步；
6. platform.signing_report：tomato 书出 checklist+评分+ready；generic 书返回提示。

运行：python tests/v080_platform.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v080_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail and not cond else ""))


from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.models import Book, Chapter  # noqa: E402
from apps.api import platform, audit  # noqa: E402
from apps.api.orchestrator import gates  # noqa: E402

init_db()

# ---------- 1. 平台预设 ----------
print("\n[1] 平台预设")
prof = platform.get_platform("tomato")
check("tomato 命中预设", prof is not None and prof.get("label") == "番茄小说")
check("未知平台返回 None", platform.get_platform("nope") is None)
check("空平台返回 None", platform.get_platform("") is None and platform.get_platform(None) is None)

# ---------- 2. 书名/简介导向 lint ----------
print("\n[2] 书名/简介 lint")
m = platform.check_meta("躺平修仙：系统逼我变强", "简介含薅羊毛", prof)
rules = {x["rule"] for x in m}
check("书名含'躺平'→书名导向 error", any(x["rule"] == "书名导向" and x["level"] == "error" for x in m), str(m))
check("命中红海句式'系统逼我变强'", "红海句式" in rules, str(rules))
check("简介含导向词 warn", any(x["rule"] == "简介导向" for x in m))
m2 = platform.check_meta("一个非常非常长的书名足足有十五七个字", "干净简介", prof)
check("书名超长 warn", any(x["rule"] == "书名长度" for x in m2))
check("干净书名零问题", platform.check_meta("剑来", "少年持剑上路", prof) == [])
check("prof 为空不 lint", platform.check_meta("躺平", "薅羊毛", None) == [])

# ---------- 3. 新闸门单元 ----------
print("\n[3] 新闸门")
fp = "我苦笑。我愣在原地。我想，这到底是怎么回事。我缓缓抬起手，又无力地放下。"
tp = "沈闲苦笑。他愣在原地。马天成盯着他，眼中闪过一丝厉色。钱多多在一旁焦急地搓手。"
check("detect_pov 第一人称", gates.detect_pov(fp) == "first", gates.detect_pov(fp))
check("detect_pov 第三人称", gates.detect_pov(tp) == "third", gates.detect_pov(tp))

rep = gates.GateReport()
gates.check_meme_density("加班到深夜，盯着钉钉的KPI，还有房贷和周报，绩效和画饼，内卷和摸鱼，ICU和996。", prof, rep)
check("梗密度超阈 warn", any(i.rule == "梗密度" for i in rep.issues),
      str([i.rule for i in rep.issues]))
check("梗密度写入 stats", "meme_per_k" in rep.stats)

rep = gates.GateReport()
gates.check_templating("他叹气。你越躺大爷越急，这仙路真是没救了。",
                       ["他叹了口气。你越躺大爷越急，这仙路你越想躺它越热闹。"], rep)
check("跨章复用句式 warn(模板化)", any(i.rule == "模板化" for i in rep.issues),
      str([i.rule for i in rep.issues]))

rep = gates.GateReport()
gates.check_opening("我猝死在电脑前的那一刻，钉钉还在弹窗。后面还有很多内容。", prof, rep)
check("开篇敏感 warn", any(i.rule == "开篇敏感" for i in rep.issues))

rep = gates.GateReport()
gates.check_quotes('他低声说"你好"，又补了一句"走吧"。', rep)
check("ASCII 直引号 warn", any(i.rule == "标点" for i in rep.issues))
check("to_curly_quotes 成对转换",
      gates.to_curly_quotes('他说"你好"就走了') == "他说“你好”就走了",
      gates.to_curly_quotes('他说"你好"就走了'))

# ---------- 4. run_gates 平台接入 ----------
print("\n[4] run_gates 平台接入")
tomato = Book(id="bk_v080_t", title="测试", platform="tomato")
generic = Book(id="bk_v080_g", title="测试", platform="generic")
db = SessionLocal()
db.add(tomato); db.add(generic); db.commit()

r_t = gates.run_gates(fp, tomato, None)
check("tomato 书触发人称闸门", any(i.rule == "人称" for i in r_t.issues),
      str([i.rule for i in r_t.issues]))
r_g = gates.run_gates(fp, generic, None)
check("generic 书不触发人称闸门(零影响)", not any(i.rule == "人称" for i in r_g.issues),
      str([i.rule for i in r_g.issues]))

long_body = "字" * 2500
check("tomato 未设定字数也按平台 2200 卡", any(i.rule == "字数" for i in gates.run_gates(long_body, tomato, None).issues))
check("generic 未设定字数不卡", not any(i.rule == "字数" for i in gates.run_gates(long_body, generic, None).issues))

# ---------- 5. 审计七维 ----------
print("\n[5] 审计七维")
check("DIMENSIONS 含叙事健康度", "叙事健康度" in audit.DIMENSIONS, str(audit.DIMENSIONS))
check("七维权重和=1.0", abs(round(sum(audit.WEIGHTS.values()), 6) - 1.0) < 1e-6, str(audit.WEIGHTS))
tpl = (ROOT / "apps/api/skills/packs/content_audit.md").read_text(encoding="utf-8")
check("模板含叙事健康度维度", "叙事健康度" in tpl)
check("模板权重含 0.10", "叙事健康度 0.10" in tpl)

# ---------- 6. 签约就绪报告 ----------
print("\n[6] 签约就绪报告")
bk = Book(id="bk_v080_s", title="躺平修仙：系统逼我变强", platform="tomato")
db.add(bk); db.commit()
ch = Chapter(id="ch_v080_1", book_id=bk.id, idx=1, title="开篇",
             content='我猝死在电脑前，钉钉还在弹KPI，加班房贷周报绩效画饼。他说"你好"就走了。' + "内容" * 900)
db.add(ch); db.commit()
sr = platform.signing_report(db, bk)
check("报告平台=番茄", sr.get("platform") == "番茄小说", str(sr.get("platform")))
check("扫描到章节", sr.get("chapters_scanned", 0) >= 1)
check("有 error 项(书名导向)", any(c["status"] == "error" for c in sr.get("checklist", [])),
      str([c for c in sr.get("checklist", []) if c["status"] == "error"]))
check("ready=False(有硬伤)", sr.get("ready") is False)
check("checklist 含字数/标点", {c["rule"] for c in sr["checklist"]} >= {"字数", "标点"})
check("封面/黄金三章规范在位", bool(sr.get("cover_spec")) and bool(sr.get("golden_three")))
check("总分<100", sr.get("score", 100) < 100)

sr_g = platform.signing_report(db, generic)
check("generic 书返回未选平台提示", "error" in sr_g and "hint" in sr_g, str(sr_g))
db.close()

print(f"\n===== PASS {len(PASS)} / FAIL {len(FAIL)} =====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
