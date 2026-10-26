"""v0.6 冒烟：纪年表 / 度量衡 / 一致性 / 内容审计 / 人物自动入册 / 主编状态感知。

用独立的临时库跑，不污染 data/。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v06_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.main import app  # noqa: E402

init_db()
c = TestClient(app)
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + extra) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 版本与健康 ==")
r = c.get("/api/health")
check("health 200", r.status_code == 200, str(r.status_code))
check("version 0.6.1", r.json().get("version") == "0.6.1", str(r.json()))

print("== 2. 建号 / 建书 / 灌章节 ==")
r = c.post("/api/auth/register", json={"username": "boss", "password": "secret123"})
check("注册管理员 200", r.status_code == 200, r.text[:160])
H = {"Authorization": "Bearer " + r.json()["token"]}

r = c.post("/api/books", json={"title": "测试书", "genre": "男频玄幻"}, headers=H)
check("建书 200", r.status_code == 200, r.text[:160])
BID = r.json()["id"]

from apps.api.models import Chapter, jd  # noqa: E402
db = SessionLocal()
for i in (1, 2, 3):
    db.add(Chapter(id=f"c{i}", book_id=BID, idx=i, title=f"第{i}章",
                   content=f"# 第{i:03d}章\n\n" + ("陈长安握紧长剑。林晚秋冷笑。张三丰长老不语。" * 40),
                   words=600, summary=f"第{i}章摘要"))
db.commit()
db.close()
check("灌入 3 章", True)

print("== 3. 主编状态快照（修复『看不到已有内容』）==")
from apps.api.orchestrator import context as ctx  # noqa: E402
from apps.api.models import Book  # noqa: E402
db = SessionLocal()
b = db.get(Book, BID)
snap = ctx.build_agent_state(db, b)
db.close()
check("快照含『共 3 章』", "共 3 章" in snap, snap[:300])
check("快照含最新章号", "第 3 章" in snap, snap[-500:])
check("快照列出章节标题", "第001章" in snap)
check("快照含唯一事实来源声明", "唯一事实来源" in snap)

db = SessionLocal()
b2 = db.get(Book, BID)
from apps.api.models import Chapter as _Ch  # noqa: E402
for x in db.scalars(__import__("sqlalchemy").select(_Ch).where(_Ch.book_id == BID)).all():
    db.delete(x)
db.commit()
snap0 = ctx.build_agent_state(db, b2)
db.close()
check("0 章时快照如实说 0 章", "0 章" in snap0 and "尚无任何章节" in snap0, snap0[:300])

# 上面为了验证 0 章场景把章节删了，这里补回来供后续用例使用
db = SessionLocal()
db.add(Chapter(id="c1", book_id=BID, idx=1, title="第1章",
               content="# 第001章\n\n" + ("陈长安握紧长剑。林晚秋冷笑。张三丰长老不语。" * 40),
               words=600, summary="第1章摘要"))
db.commit()
db.close()

print("== 4. 纪年表 ==")
r = c.post(f"/api/books/{BID}/timeline",
           json={"title": "陈长安入宗", "ch_idx": 1, "era": "开元",
                 "time_label": "开元三年春", "kind": "转折"}, headers=H)
check("记入纪年事件 200", r.status_code == 200, r.text[:160])
check("排序锚点=3", r.json().get("sort_key") == 3.0, str(r.json()))
c.post(f"/api/books/{BID}/timeline",
       json={"title": "宗门大比", "ch_idx": 2, "era": "开元", "time_label": "开元五年秋"}, headers=H)
r = c.get(f"/api/books/{BID}/timeline", headers=H)
check("纪年表 2 条", len(r.json()["items"]) == 2, str(r.json()))
# 故意制造时间倒流
c.post(f"/api/books/{BID}/timeline",
       json={"title": "倒流事件", "ch_idx": 3, "era": "开元", "time_label": "开元元年"}, headers=H)
r = c.get(f"/api/books/{BID}/consistency", headers=H)
check("检出时间倒流", any(i["kind"] == "timeline" for i in r.json()["issues"]),
      str(r.json()["issues"])[:200])
c.delete(f"/api/books/{BID}/timeline/{c.get(f'/api/books/{BID}/timeline', headers=H).json()['items'][-1]['id']}",
         headers=H)
check("删除后只剩 2 条", len(c.get(f"/api/books/{BID}/timeline", headers=H).json()["items"]) == 2)
r = c.get(f"/api/books/{BID}/consistency", headers=H)
check("删除后无时间线错误", not any(i["kind"] == "timeline" for i in r.json()["issues"]))

print("== 5. 度量衡账本 ==")
r = c.post(f"/api/books/{BID}/ledger",
           json={"subject": "陈长安", "metric": "金钱", "delta": 300, "ch_idx": 1,
                 "unit": "枚", "reason": "入门俸禄"}, headers=H)
check("记账 200", r.status_code == 200, r.text[:160])
check("余额=300", r.json().get("balance") == 300.0, str(r.json()))
r = c.post(f"/api/books/{BID}/ledger",
           json={"subject": "陈长安", "metric": "金钱", "delta": -120, "ch_idx": 2,
                 "unit": "枚", "reason": "买丹药"}, headers=H)
check("余额累加=180", r.json().get("balance") == 180.0, str(r.json()))
r = c.post(f"/api/books/{BID}/ledger",
           json={"subject": "陈长安", "metric": "级别", "delta": 1, "ch_idx": 2,
                 "unit": "级", "reason": "突破练气二层"}, headers=H)
check("不同维度独立记账", r.json().get("balance") == 1.0, str(r.json()))

r = c.get(f"/api/books/{BID}/ledger", headers=H)
check("账本 3 笔", len(r.json()["items"]) == 3, str(len(r.json()["items"])))
check("余额快照 2 组", len(r.json()["balances"]) == 2, str(r.json()["balances"]))
check("metrics 含 7 类", len(r.json()["metrics"]) == 7, str(r.json()["metrics"]))

# 手工制造断层：直接改坏一条 balance
db = SessionLocal()
from apps.api.models import LedgerEntry  # noqa: E402
bad = db.scalars(__import__("sqlalchemy").select(LedgerEntry).where(
    LedgerEntry.book_id == BID, LedgerEntry.metric == "金钱").order_by(LedgerEntry.id)).all()[1]
bad.balance = 999.0
db.commit()
db.close()
r = c.get(f"/api/books/{BID}/consistency", headers=H)
check("检出余额断层", any(i["kind"] == "ledger" for i in r.json()["issues"]),
      str(r.json()["issues"])[:200])
# 删掉中间一笔 → 自动重算
lid = c.get(f"/api/books/{BID}/ledger", headers=H).json()["items"][1]["id"]
r = c.delete(f"/api/books/{BID}/ledger/{lid}", headers=H)
check("删除并重算 200", r.status_code == 200 and r.json().get("recomputed", 0) >= 1, r.text[:160])
r = c.get(f"/api/books/{BID}/consistency", headers=H)
check("重算后无断层", not any(i["kind"] == "ledger" for i in r.json()["issues"]),
      str(r.json()["issues"])[:200])

print("== 5b. 多笔流水顺序（回归：id 是 uuid，按 id 排序会算错余额）==")
c.post(f"/api/books/{BID}/ledger", headers=H, json={"subject": "苏明月", "metric": "经验", "delta": 10, "ch_idx": 1, "unit": "点"})
c.post(f"/api/books/{BID}/ledger", headers=H, json={"subject": "苏明月", "metric": "经验", "delta": 20, "ch_idx": 2, "unit": "点"})
c.post(f"/api/books/{BID}/ledger", headers=H, json={"subject": "苏明月", "metric": "经验", "delta": 30, "ch_idx": 3, "unit": "点"})
c.post(f"/api/books/{BID}/ledger", headers=H, json={"subject": "苏明月", "metric": "经验", "delta": 40, "ch_idx": 4, "unit": "点"})
c.post(f"/api/books/{BID}/ledger", headers=H, json={"subject": "苏明月", "metric": "经验", "delta": 50, "ch_idx": 5, "unit": "点"})
flow = [r for r in c.get(f"/api/books/{BID}/ledger", headers=H).json()["items"]
        if r["subject"] == "苏明月"]
check("5 笔流水齐全", len(flow) == 5, str(len(flow)))
check("余额严格累加 10/30/60/100/150",
      [r["balance"] for r in flow] == [10, 30, 60, 100, 150], str([r["balance"] for r in flow]))
check("seq 单调递增", [r["seq"] for r in flow] == sorted(r["seq"] for r in flow),
      str([r["seq"] for r in flow]))

print("== 6. 正文数值 vs 账本对账 ==")
from apps.api import consistency as K  # noqa: E402
db = SessionLocal()
c.post(f"/api/books/{BID}/ledger",
       json={"subject": "陈长安", "metric": "金钱", "delta": 100, "ch_idx": 3, "unit": "枚"})
# 第 3 章余额 100，正文却写 500 枚 → 应报警
iss = K.check_text_vs_ledger(db, BID, 3, "他掏出五百枚灵石拍在桌上。")
db.close()
check("检出正文超支", len(iss) >= 1 and iss[0]["kind"] == "text_vs_ledger", str(iss)[:200])

print("== 7. 人物自动入册 ==")
r = c.post(f"/api/books/{BID}/characters/sync",
           json={"ch_idx": 1, "min_mentions": 2,
                 "text": "陈长安握紧长剑。林晚秋冷笑道：“你也不过如此。”张三丰长老从殿内走出，盯着陈长安。林晚秋又说：“我十六岁那年就见过你。”张三丰长老摇头。"},
           headers=H)
check("sync 200", r.status_code == 200, r.text[:200])
added = r.json().get("added", [])
names = [a["name"] for a in added]
check("陈长安入册", "陈长安" in names, str(names))
check("林晚秋入册", "林晚秋" in names, str(names))
check("张三丰入册", "张三丰" in names, str(names))
r = c.get(f"/api/books/{BID}/characters", headers=H)
check("人物清单非空", len(r.json()["items"]) >= 3, str(r.json()["items"])[:200])
check("自动人物带 is_auto 标记", any(i["is_auto"] == 1 for i in r.json()["items"]),
      str(r.json()["items"])[:200])
# 重复 sync 不应重复建
r2 = c.post(f"/api/books/{BID}/characters/sync",
            json={"ch_idx": 1, "min_mentions": 2,
                  "text": "陈长安握紧长剑。林晚秋冷笑道：“你也不过如此。”张三丰长老从殿内走出，盯着陈长安。林晚秋又说：“我十六岁那年就见过你。”张三丰长老摇头。"},
            headers=H)
check("重复 sync 不重复建", len(r2.json().get("added", [])) == 0, str(r2.json()))

print("== 8. 内容审计（未配模型 → 规则降级）==")
r = c.post(f"/api/books/{BID}/audit", json={"ch_idx": 1}, headers=H)
check("审计 200", r.status_code == 200, r.text[:200])
rep = r.json()
check("五维齐全", len(rep.get("dimensions", [])) == 5, str([d["name"] for d in rep.get("dimensions", [])]))
check("维度命名正确",
      [d["name"] for d in rep["dimensions"]] == ["去AI味", "逻辑一致性", "内容一致性", "世界观一致性", "大纲遵从度"],
      str([d["name"] for d in rep["dimensions"]]))
check("score 在 0-100", 0 <= rep.get("score", -1) <= 100, str(rep.get("score")))
check("mode=rules（无模型降级）", rep.get("mode") == "rules", str(rep.get("mode")))
r = c.get(f"/api/books/{BID}/audits", headers=H)
check("审计留档可查", len(r.json()["items"]) >= 1, str(r.json()))

print("== 9. 权限：普通用户不得操作以上接口 ==")
r = c.post("/api/auth/register", json={"username": "guest", "password": "secret123"})
GH = {"Authorization": "Bearer " + r.json()["token"]}
check("普通用户角色为 user", r.json()["user"]["role"] == "user")
for path, method in [("/timeline", "get"), ("/ledger", "get"), ("/consistency", "get"),
                     ("/audits", "get"), ("/characters", "get")]:
    rr = c.get(f"/api/books/{BID}{path}", headers=GH)
    check(f"普通用户 GET {path} → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post(f"/api/books/{BID}/timeline", json={"title": "x"}, headers=GH)
check("普通用户 POST /timeline → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post(f"/api/books/{BID}/ledger", json={"subject": "a", "metric": "金钱", "delta": 1}, headers=GH)
check("普通用户 POST /ledger → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post(f"/api/books/{BID}/audit", json={"ch_idx": 1}, headers=GH)
check("普通用户 POST /audit → 403", rr.status_code == 403, str(rr.status_code))

print("== 10. Agent 工具注册 ==")
from apps.api import agent  # noqa: E402
names = [t["function"]["name"] for t in agent.TOOLS]
for t in ("read_chapter", "add_timeline_event", "add_ledger_entry",
          "check_consistency", "audit_chapter"):
    check(f"工具 {t} 已注册", t in names, str(names))
check("SYSTEM_PROMPT 声明状态快照为事实来源", "唯一事实来源" in agent.SYSTEM_PROMPT)

print()
print("FAILED:", len(FAILED))
for f in FAILED:
    print("  -", f)
sys.exit(1 if FAILED else 0)
