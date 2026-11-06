"""v0.7.2 境界体系语义审计冒烟测试。

覆盖：
1. audit.DIMENSIONS 含"实力体系一致性"，且 WEIGHTS 各项和为 1.0；
2. audit._realm_risk_spans 命中 降维/碾压/越级 预筛、无关正文不误报；
3. audit._realm_system 产出有序境界阶梯 + 角色境界归属；无境界时返回"未登记"；
4. audit._gather_context 返回 realm_system 且含全部境界名（按录入顺序）+ 角色归属；
5. audit._normalize 透传模型返回的六维；模型漏掉实力体系维度时仍补齐不崩；
6. content_audit.md 模板含 realm_system 变量 + 实力体系维度 + 六维权重；
7. gates.realm_whitelist_for 与审计上下文对同一境界集取到一致白名单。

运行：python tests/v072_realm_audit.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v072_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail and not cond else ""))


from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.models import Book, WorldSetting, Character  # noqa: E402
from apps.api import audit  # noqa: E402
from apps.api.orchestrator import gates  # noqa: E402

init_db()

# ---------- 1. 维度与权重 ----------
print("\n[1] 六维定义与权重")
check("DIMENSIONS 含实力体系一致性", "实力体系一致性" in audit.DIMENSIONS, str(audit.DIMENSIONS))
wsum = round(sum(audit.WEIGHTS.values()), 6)
check("WEIGHTS 各项和为 1.0", abs(wsum - 1.0) < 1e-6, str(audit.WEIGHTS))
check("每个维度都有权重", all(d in audit.WEIGHTS for d in audit.DIMENSIONS))

# ---------- 2. 越级/碾压预筛 ----------
print("\n[2] 越级/碾压预筛")
spans = audit._realm_risk_spans("他降维碾压般地击溃了马天成，又一招秒杀了另外两人。")
check("命中降维碾压", any("降维碾压" in s for s in spans), str(spans))
check("命中秒杀", any("秒杀" in s for s in spans), str(spans))
check("无关正文无命中", audit._realm_risk_spans("他慢慢地走在山间小路上。") == [])

# ---------- 3. 境界阶梯上下文装配 ----------
print("\n[3] 境界体系上下文装配")
db = SessionLocal()
book = Book(id="bk_v072", title="越级审计测试", owner_id="u_v072")
db.add(book)
db.commit()
for i, (k, v) in enumerate([("凡境", "一至九层打基础"), ("灵海境", "开灵海驭气"),
                            ("真元境", "凝真元御空"), ("化神境", "神识外放"),
                            ("至尊境", "本书天花板")], 1):
    db.add(WorldSetting(id=f"ws_v072_{i}", book_id=book.id, category="境界", key=k, value=v))
db.add(Character(id="ch_v072_1", book_id=book.id, name="沈闲", profile="少年，灵海境三层"))
db.add(Character(id="ch_v072_2", book_id=book.id, name="马天成", profile="外门弟子"))
db.commit()

rs = audit._realm_system(db, book, "他降维碾压般地击溃了马天成。")
check("境界阶梯按录入顺序",
      rs.index("凡境") < rs.index("灵海境") < rs.index("真元境")
      < rs.index("化神境") < rs.index("至尊境"))
check("含全部境界名",
      all(k in rs for k in ["凡境", "灵海境", "真元境", "化神境", "至尊境"]))
check("含角色境界归属(沈闲→灵海境)", "沈闲" in rs and "灵海境" in rs)
check("注入疑似碾压表述", "降维碾压" in rs)

book2 = Book(id="bk_v072_nr", title="无境界", owner_id="u_v072")
db.add(book2)
db.commit()
rs2 = audit._realm_system(db, book2, "")
check("无境界体系返回未登记", "未登记" in rs2, rs2[:40])

ctx = audit._gather_context(db, book, 1, "他降维碾压般地击溃了马天成。")
check("_gather_context 返回 realm_system",
      "realm_system" in ctx and "灵海境" in ctx["realm_system"])
db.close()

# ---------- 4. _normalize 透传六维 ----------
print("\n[4] _normalize 透传")
dims = []
for n in audit.DIMENSIONS:
    if n == "实力体系一致性":
        dims.append({"name": n, "score": 70,
                     "issues": [{"severity": "error", "message": "同境降维", "span": "x"}]})
    else:
        dims.append({"name": n, "score": 90, "issues": []})
rep = audit._normalize({"score": 80, "dimensions": dims, "summary": "test"})
got = {d["name"] for d in rep["dimensions"]}
check("_normalize 含六维", got == set(audit.DIMENSIONS), str(got))
sz = next(d for d in rep["dimensions"] if d["name"] == "实力体系一致性")
check("实力体系维度透传 issue",
      len(sz["issues"]) == 1 and sz["issues"][0]["message"] == "同境降维")

dims_missing = [{"name": n, "score": 90, "issues": []}
                for n in audit.DIMENSIONS if n != "实力体系一致性"]
rep2 = audit._normalize({"score": 0, "dimensions": dims_missing, "summary": "x"})
check("缺实力体系维度仍补齐不崩",
      any(d["name"] == "实力体系一致性" for d in rep2["dimensions"]))

# ---------- 5. 模板 ----------
print("\n[5] content_audit.md 模板")
tpl = (ROOT / "apps/api/skills/packs/content_audit.md").read_text(encoding="utf-8")
check("模板含 realm_system 变量", "realm_system" in tpl.split("---", 2)[1])
check("模板含境界体系区块", "境界体系（实力阶梯）" in tpl)
check("模板含实力体系一致性维度", "实力体系一致性" in tpl)
check("模板权重七维", "实力体系 0.13" in tpl and "叙事健康度 0.10" in tpl)

# ---------- 6. 白名单与闸门仍一致 ----------
print("\n[6] 白名单一致性")
db2 = SessionLocal()
check("realm_whitelist_for 返回5境界",
      len(gates.realm_whitelist_for(db2, "bk_v072")) == 5,
      str(gates.realm_whitelist_for(db2, "bk_v072")))
db2.close()

print(f"\n===== PASS {len(PASS)} / FAIL {len(FAIL)} =====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
