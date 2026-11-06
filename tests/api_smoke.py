"""API 层冒烟：验证两个历史 bug 的修复 + 新路由。

1) 模型「设为默认」：PUT /api/models/{id} 只传 is_default 时不能抹掉其它字段
   （update_model 改为 exclude_unset）。
2) 配置中心技能页：/api/skills 返回非空。
3) 新链路：bible 含 chapters 字段；番茄导出可下载且正文含「第N章」。
"""
import json
import os
import sys
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote

TMP = tempfile.mkdtemp(prefix="nf_api_")
os.environ.setdefault("NOVELFORGE_DATA", TMP)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{TMP}/nf.db"
# v0.5 起全站要登录。这个测试写于登录层之前，不带 token 一律 401，
# 之前一直挂在「POST /api/books 未登录」上，属于陈旧测试而非产品 bug。
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.api import db as dbmod
dbmod.init_db()
from apps.api import gitrepo
gitrepo.init_repo = lambda *a, **k: None
from apps.api.models import Chapter

from fastapi.testclient import TestClient
from apps.api.main import app

PY = sys.executable
ok = True


def check(name, cond, extra=""):
    global ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {extra}")
    if not cond:
        ok = False


# ---- 启动 mock LLM（后台子进程） ----
mock_proc = subprocess.Popen(
    [PY, "-m", "uvicorn", "mock_llm:app", "--host", "127.0.0.1", "--port", "8123",
     "--log-level", "error"],
    cwd=str(PROJECT / "tests"),
)
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:8123/health", timeout=1)
        break
    except Exception:
        time.sleep(0.2)
else:
    print("[WARN] mock LLM 未就绪，ping 测试将失败")


c = TestClient(app)

# ---- 登录（管理员） ----
_r = c.post("/api/auth/register", json={"username": "boss", "password": "secret123"})
check("注册管理员", _r.status_code == 200, _r.text[:160])
H = {"Authorization": "Bearer " + _r.json()["token"]}

# ---- 技能页 ----
r = c.get("/api/skills", headers=H)
skills = r.json()
check("GET /api/skills 非空", r.status_code == 200 and len(skills) > 0,
      f"status={r.status_code} packs={len(skills)}")

# ---- 建书 ----
r = c.post("/api/books", json={"title": "番茄投稿测试", "genre": "男频·修仙",
                               "platform": "番茄小说", "chapter_min": 2000, "chapter_max": 2200},
           headers=H)
check("POST /api/books", r.status_code == 200, str(r.json()))
bid = r.json().get("id")
check("建书返回 id", bool(bid), str(r.json()))

# ---- 先插一章，供番茄导出验证正文 ----
dbs = dbmod.SessionLocal()
dbs.add(Chapter(id="ch_seed", book_id=bid, idx=1, title="退婚那天，我觉醒了躺平系统",
                content="天刚蒙蒙亮，沈闲就被门外一阵吵闹声吵醒。", words=300, status="final"))
dbs.commit()
dbs.close()

# ---- 模型 CRUD ----
payload = {"name": "测试模型A", "base_url": "http://127.0.0.1:8123/v1",
           "model_id": "mock", "api_key": "sk-test", "role": "writer", "is_default": 0}
r = c.post("/api/models", json=payload, headers=H)
check("POST /api/models 新增", r.status_code == 200, str(r.json()))
mid = r.json().get("id")

# 修复点：只传 is_default，其余字段不能被清零（用 list 端点回查）
r = c.put(f"/api/models/{mid}", json={"is_default": 1}, headers=H)
check("PUT /api/models 设默认", r.status_code == 200)
models = c.get("/api/models", headers=H).json()
m = next((x for x in models if x["id"] == mid), {})
check("设默认不抹字段", m.get("is_default") == 1
      and m.get("name") == "测试模型A"
      and m.get("base_url") == "http://127.0.0.1:8123/v1"
      and m.get("model_id") == "mock",
      f"name={m.get('name')} base={m.get('base_url')} mid={m.get('model_id')} def={m.get('is_default')}")

# 连通测试（mock 已起，应返回 ok:True）
r = c.post(f"/api/models/{mid}/ping", headers=H)
pj = r.json()
check("POST /api/models ping 连通成功", r.status_code == 200 and pj.get("ok") is True, str(pj))

# 删除
r = c.delete(f"/api/models/{mid}", headers=H)
check("DELETE /api/models", r.status_code == 200)
left = [x for x in c.get("/api/models", headers=H).json() if x["id"] == mid]
check("删除后不存在", len(left) == 0)

# ---- 设定集（含 chapters） ----
r = c.get(f"/api/books/{bid}/bible", headers=H)
check("GET /api/books bible", r.status_code == 200, "")
if r.status_code == 200:
    b = r.json()
    check("bible 含 chapters 字段", "chapters" in b, f"keys={list(b.keys())}")

# ---- 番茄导出 ----
r = c.get(f"/api/books/{bid}/export?fmt=publish", headers=H)
check("GET /api/books export?fmt=tomato", r.status_code == 200, "")
if r.status_code == 200:
    disp = r.headers.get("content-disposition", "")
    # 真实文件名在 filename*=UTF-8'' 扩展属性里
    fname = ""
    for part in disp.split(";"):
        part = part.strip()
        if part.startswith("filename*="):
            fname = unquote(part.split("''", 1)[-1])
    body = r.text
    check("投稿包文件名正确", "投稿包" in fname, f"fname={fname}")
    check("投稿包正文含 第001章", "第001章" in body and "退婚那天" in body,
          f"body_len={len(body)}")

# ---- 收尾 ----
try:
    mock_proc.terminate()
except Exception:
    pass

print("\nAPI 冒烟", "全部通过 ✅" if ok else "存在失败 ❌")
sys.exit(0 if ok else 1)
