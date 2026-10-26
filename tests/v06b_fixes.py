"""v0.6b 修复回归：审计出的 16 项缺陷，逐条钉住防止回归。

用独立临时库，不污染 data/。重点覆盖：
1. 被停用账号的旧令牌必须失效（P0 越权）
2. 导入的书必须拿到 owner_id（P1 功能失效）
3. 未知导出格式必须 400 而不是静默给别的格式
4. agent 成本不再恒零
5. 伏笔回收不再用未转义的 LIKE
6. 列表接口字段契约不变（N+1 修复不能改接口）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v06b_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
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


print("== 1. 被停用账号的旧令牌必须失效（P0）==")
tok_admin = c.post("/api/auth/register", json={"username": "boss", "password": "secret123"}).json()["token"]
HA = {"Authorization": "Bearer " + tok_admin}
r = c.post("/api/auth/register", json={"username": "writer", "password": "secret123"})
check("version 0.9.0", c.get("/api/health").json().get("version") == "0.9.0")
check("注册第二个账号 200", r.status_code == 200, r.text[:160])
tok_writer = r.json()["token"]
HW = {"Authorization": "Bearer " + tok_writer}

bid = c.post("/api/books", json={"title": "回归测试书"}, headers=HW).json()["id"]
check("建书 200", bool(bid))

r = c.get(f"/api/books/{bid}/bible", headers=HW)
check("停用前可读自己的书", r.status_code == 200, str(r.status_code))

# 管理员把 writer 停用
db = SessionLocal()
from apps.api.models import User, Book  # noqa: E402
u = db.scalar(__import__("sqlalchemy").select(User).where(User.username == "writer"))
u.is_active = 0
db.commit()
db.close()

for path, method in [(f"/api/books/{bid}/bible", "get"),
                     (f"/api/books/{bid}/chapters/1", "get"),
                     (f"/api/books/{bid}/export?fmt=txt", "get")]:
    r = getattr(c, method)(path, headers=HW)
    check(f"停用后 {method.upper()} {path} → 401", r.status_code == 401,
          f"status={r.status_code} body={r.text[:120]}")

r = c.post(f"/api/books/{bid}/chat", json={"message": "hi", "history": []}, headers=HW)
check("停用后 POST /chat → 401", r.status_code == 401, f"status={r.status_code}")

r = c.get(f"/api/books/{bid}/bible", headers=HA)
check("管理员仍可访问该书", r.status_code == 200, str(r.status_code))

print("== 2. 导入的书必须拿到 owner_id ==")
db = SessionLocal()
b = db.get(Book, bid)
db.close()
# importer.import_book 返回的键是 book_id；main.py 原先读 "id"，
# 导致 owner_id 永远是空。这里直接断言两边的键名对得上。
import inspect  # noqa: E402
from apps.api import importer as imp_mod  # noqa: E402
src = inspect.getsource(imp_mod.import_book)
check("importer 返回键含 book_id", '"book_id"' in src)
main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
check("main.py 读取 book_id", 'res.get("book_id")' in main_src,
      "导入路由仍在读 'id'，owner 永远不会被设置")

print("== 3. 未知导出格式必须 400，不能静默降级 ==")
r = c.get(f"/api/books/{bid}/export?fmt=nope", headers=HA)
check("未知格式 → 400", r.status_code == 400, f"status={r.status_code}")
check("400 里说明可用格式", "txt" in r.text and "publish" in r.text, r.text[:200])
r = c.get(f"/api/books/{bid}/export?fmt=txt", headers=HA)
check("合法格式 txt 仍 200", r.status_code == 200, str(r.status_code))

print("== 4. agent 成本不再恒零 ==")
agent_src = (ROOT / "apps/api/agent.py").read_text(encoding="utf-8")
check("不再有 (turn and 0 or 0) 恒零表达式", "turn and 0 or 0" not in agent_src)
llm_src = (ROOT / "apps/api/llm.py").read_text(encoding="utf-8")
check("AgentTurn 已有 cost 字段", "cost: float = 0.0" in llm_src)
check("act() 解析 usage", "usage.get(\"prompt_tokens\")" in llm_src or
      "usage.get('prompt_tokens')" in llm_src)

print("== 5. 伏笔回收不再用未转义 LIKE ==")
graph_src = (ROOT / "apps/api/orchestrator/graph.py").read_text(encoding="utf-8")
check("graph.py 无 content.like(f\"%{", 'content.like(f"%{' not in graph_src)
check("改用 contains()", "Foreshadow.content.contains(" in graph_src)

print("== 6. 列表接口字段契约不变（N+1 修复不能改接口）==")
r = c.get("/api/books", headers=HA).json()
check("列表非空", len(r) >= 1, str(len(r)))
need = {"id", "title", "author", "genre", "platform", "chapters", "last_idx", "words",
        "status", "quality_score", "owner_id"}
check("字段齐全", need <= set(r[0].keys()), f"keys={sorted(r[0].keys())}")
check("chapters 是 int", isinstance(r[0]["chapters"], int), str(type(r[0]["chapters"])))
check("words 是 int", isinstance(r[0]["words"], int), str(type(r[0]["words"])))
check("last_idx 是 int", isinstance(r[0]["last_idx"], int), str(type(r[0]["last_idx"])))
mine = [x for x in r if x["id"] == bid][0]
check("新建书 chapters=0 / words=0 / last_idx=0",
      (mine["chapters"], mine["words"], mine["last_idx"]) == (0, 0, 0),
      f"{mine['chapters']}/{mine['words']}/{mine['last_idx']}")

print("== 7. 仪表盘 next 指引可用 ==")
r = c.get("/api/dashboard", headers=HA)
check("dashboard 200", r.status_code == 200, r.text[:200])
d = r.json()
check("total >= 1", d.get("total", 0) >= 1, str(d.get("total")))
item = [i for i in d["items"] if i["id"] == bid][0]
check("next 有 step 与 text", "step" in item["next"] and "text" in item["next"],
      str(item["next"]))

print()
print(f"v0.6b 修复回归：{'FAILED: ' + str(len(FAILED)) if FAILED else 'FAILED: 0'}")
sys.exit(1 if FAILED else 0)
