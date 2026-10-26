"""v0.7.1 修复冒烟测试。

覆盖：
1. 新建书不再预填 100 万字 / 2000-2200（0 = 未设定，dashboard 同步）；
2. 闸门字数检查在未设定时不告警、设定后生效；
3. 人物入册守卫（validators + save_character 工具 + 自动入册过滤）；
4. 境界体系闸门：体系外境界 error，体系内+阶段后缀放行；
5. importer.parse_realm 不再做全文兜底（伏笔不再串味进"境界"）。

运行：python tests/v071_fixes.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v071_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail and not cond else ""))


from fastapi.testclient import TestClient  # noqa: E402
from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.main import app  # noqa: E402

init_db()
c = TestClient(app)

# ---------- 1. 新建书零预填 ----------
print("\n[1] 新建书零预填")
r = c.post("/api/auth/register", json={"username": "t071", "password": "secret123"})
tok = r.json()["token"]
H = {"Authorization": f"Bearer {tok}"}
bid = c.post("/api/books", json={"title": "零预填测试"}, headers=H).json()["id"]
b = c.get(f"/api/books/{bid}", headers=H).json()
b = b.get("book") or b
check("target_words 未设定为 0", int(b.get("target_words", -1)) == 0, str(b.get("target_words")))
check("chapter_min 未设定为 0", int(b.get("chapter_min", -1)) == 0, str(b.get("chapter_min")))
check("chapter_max 未设定为 0", int(b.get("chapter_max", -1)) == 0, str(b.get("chapter_max")))
dash = c.get("/api/dashboard", headers=H).json()["items"][0]
check("dashboard 不再兜底 100 万", int(dash["target_words"]) == 0, str(dash["target_words"]))

# ---------- 2. 字数闸门：未设定不告警 / 设定后生效 ----------
print("\n[2] 字数闸门")
from apps.api.orchestrator import gates  # noqa: E402
from apps.api.models import Book, WorldSetting  # noqa: E402

db = SessionLocal()
book = db.get(Book, bid)
body_2k = "字" * 2100
rep = gates.run_gates(f"# 第001章 测试\n\n{body_2k}", book, None)
check("未设定字数时不告警", not any(i.rule == "字数" for i in rep.issues),
      str([i.message for i in rep.issues if i.rule == "字数"]))
book.chapter_min, book.chapter_max = 2000, 2200
rep2 = gates.run_gates(f"# 第001章 测试\n\n{body_2k}", book, None)
check("设定后字数达标不告警", not any(i.rule == "字数" for i in rep2.issues))
book.chapter_min, book.chapter_max = 3000, 3200
rep3 = gates.run_gates(f"# 第001章 测试\n\n{body_2k}", book, None)
check("低于下限报 error", any(i.rule == "字数" and i.level == "error" for i in rep3.issues))
book.chapter_min, book.chapter_max = 0, 0   # 复位，避免影响后面的境界闸门用例
db.commit()

# ---------- 3. 人物入册守卫 ----------
print("\n[3] 人物入册守卫")
from apps.api.validators import valid_character_name, is_realm_like  # noqa: E402

check("正常人物名通过", valid_character_name("楚清霜"))
check("编号大纲条目被拒", not valid_character_name("1. 退婚流的反转"))
check("含换行被拒", not valid_character_name("退婚流的反转\n传统退婚流"))
check("伏笔特征被拒", not valid_character_name("沈家破落真相（第 1 章）"))
check("卖点句式被拒", not valid_character_name("薅羊毛正反馈循环"))

from apps.api import agent as agent_mod  # noqa: E402


async def _fake_emit(*a, **k):
    pass

msg = asyncio.run(agent_mod._t_save_character(db, book, {"name": "1. 退婚流的反转", "role": "x"}, _fake_emit))
check("save_character 拒绝伪人物", "未保存" in msg, msg[:60])
asyncio.run(agent_mod._t_save_character(db, book, {"name": "测试人物甲", "role": "龙套"}, _fake_emit))

from sqlalchemy import select  # noqa: E402
from apps.api.models import Character  # noqa: E402

row = db.scalar(select(Character).where(Character.book_id == bid, Character.name == "测试人物甲"))
check("save_character 保留合法人物(复查)", row is not None)

# ---------- 4. 境界体系闸门 ----------
print("\n[4] 境界体系闸门")
for k, v in [("凡境", "一至九层"), ("灵海境", "开灵海，驭气"), ("真元境", "凝真元")]:
    db.add(WorldSetting(id=f"ws_{k}", book_id=bid, category="境界", key=k, value=v))
db.commit()
wl = gates.realm_whitelist_for(db, bid)
check("白名单提取", set(wl) >= {"凡境", "灵海境", "真元境"}, str(wl))

t1 = "# 第002章 测试\n\n" + "他如今是元婴境，随手一挥。"
rep4 = gates.run_gates(t1, book, None, realm_whitelist=wl)
check("体系外境界报 error", any(i.rule == "境界" and i.level == "error" for i in rep4.issues),
      str([i.message for i in rep4.issues if i.rule == "境界"]))
t2 = "# 第003章 测试\n\n" + "他还停在灵海境初期，不敢越级。"
rep5 = gates.run_gates(t2, book, None, realm_whitelist=wl)
check("体系内境界+阶段后缀放行", not any(i.rule == "境界" for i in rep5.issues),
      str([i.message for i in rep5.issues if i.rule == "境界"]))
t3 = "# 第004章 测试\n\n" + "这里的环境十分安静，他心如止水。"
rep6 = gates.run_gates(t3, book, None, realm_whitelist=wl)
check("环境/心境等非体系词放行", not any(i.rule == "境界" for i in rep6.issues),
      str([i.message for i in rep6.issues if i.rule == "境界"]))
check("白名单为空时闸门不启用",
      not any(i.rule == "境界" for i in gates.run_gates(
          "# 第005章 x\n\n他突破元婴境。", book, None, realm_whitelist=[]).issues))

# ---------- 5. importer 防串味 ----------
print("\n[5] importer 防串味")
from apps.api.importer import parse_realm, parse_characters  # noqa: E402

trap = """# 《陷阱书》

## 一、核心卖点
1. **沈家破落真相**（第 1 章）：家道为何中落未交代。— 待回收（卷三）
2. **系统来历暗示**（第 1 章）：系统原主人是谁？— 待回收（卷四）

## 二、人物小传
### 沈闲（主角）
想躺平的社畜。
"""
realms = parse_realm(trap)
check("无境界小节时不再全文兜底", realms == [], str(realms))
chars = parse_characters(trap)
check("人物抽取不收卖点条目", [x["name"] for x in chars] == ["沈闲"], str([x["name"] for x in chars]))

good_outline = """# 《正例书》

## 三、力量境界
1. **凡境**（一至九层）——打基础
2. **灵海境**（开灵海）——驭气
3. **化神境**（神识外放）——大能
"""
realms2 = parse_realm(good_outline)
names2 = [k for k, _ in realms2]
check("正常境界小节照常解析", set(names2) >= {"凡境", "灵海境", "化神境"}, str(names2))

db.close()

print(f"\n========== v0.7.1 冒烟测试：通过 {len(PASS)}，失败 {len(FAIL)} ==========")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
