"""v0.4 端到端冒烟：注册/登录/鉴权/仪表盘/质量技能/中性化导出。

用独立的临时库跑，不污染 data/。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v04_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from apps.api import models  # noqa: E402
from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.main import app  # noqa: E402

init_db()
c = TestClient(app)
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + extra) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 健康与开放注册探测 ==")
r = c.get("/api/health")
check("health 200", r.status_code == 200, str(r.status_code))
check("version 0.6.1", r.json().get("version") == "0.6.1", str(r.json()))
r = c.get("/api/auth/allow-register")
check("allow-register 可用（白名单内）", r.status_code == 200 and r.json().get("allow") is True, r.text[:120])
check("first_user=True（库为空）", r.json().get("first_user") is True, r.text[:120])

print("== 2. 鉴权拦截 ==")
check("无令牌访问 /api/books → 401", c.get("/api/books").status_code == 401)
check("无令牌访问 /api/dashboard → 401", c.get("/api/dashboard").status_code == 401)
check("无令牌访问 /api/models → 401", c.get("/api/models").status_code == 401)
check("无令牌访问 /api/admin/users → 401", c.get("/api/admin/users").status_code == 401)
check("坏令牌 → 401", c.get("/api/books", headers={"Authorization": "Bearer bogus"}).status_code == 401)

print("== 3. 注册 → 首位管理员 ==")
r = c.post("/api/auth/register", json={"username": "david", "password": "secret123", "email": "d@x.com"})
check("注册 200", r.status_code == 200, r.text[:160])
body = r.json()
check("首位注册者为 admin", body.get("user", {}).get("role") == "admin", r.text[:160])
T1 = body.get("token")
check("拿到 token", bool(T1))
H1 = {"Authorization": "Bearer " + (T1 or "")}

r = c.post("/api/auth/register", json={"username": "david", "password": "secret123"})
check("重名注册 → 409", r.status_code == 409, r.text[:120])
r = c.post("/api/auth/register", json={"username": "ab", "password": "123"})
check("弱密码注册 → 400", r.status_code == 400, r.text[:120])

print("== 4. 登录 ==")
r = c.post("/api/auth/login", json={"username": "david", "password": "wrongpass"})
check("错密码 → 401", r.status_code == 401, r.text[:120])
r = c.post("/api/auth/login", json={"username": "david", "password": "secret123"})
check("正确密码登录 200", r.status_code == 200, r.text[:160])
r = c.get("/api/auth/me", headers=H1)
check("/auth/me 返回用户名", r.json().get("username") == "david", r.text[:160])

print("== 5. 管理员创建普通用户 + 越权检查 ==")
r = c.post("/api/admin/users", headers=H1, json={"username": "writer2", "password": "pass1234", "role": "user"})
check("管理员建用户 200", r.status_code == 200, r.text[:160])
r = c.post("/api/auth/login", json={"username": "writer2", "password": "pass1234"})
H2 = {"Authorization": "Bearer " + r.json().get("token", "")}
check("普通用户登录 200", bool(H2["Authorization"].split(" ", 1)[1]), r.text[:120])
check("普通用户访问 /admin/users → 403", c.get("/api/admin/users", headers=H2).status_code == 403)
check("普通用户删除用户 → 403",
      c.delete("/api/admin/users/" + r.json().get("id", "x"), headers=H2).status_code in (403, 404))

print("== 6. 建书 + 归属隔离（两个普通作者互不可见；管理员可见全部） ==")
# 建第二个普通作者 writer3，用于验证普通用户之间的隔离
r = c.post("/api/admin/users", headers=H1, json={"username": "writer3", "password": "pass1234", "role": "user"})
check("管理员再建普通用户 200", r.status_code == 200, r.text[:160])
r = c.post("/api/auth/login", json={"username": "writer3", "password": "pass1234"})
H3 = {"Authorization": "Bearer " + r.json().get("token", "")}

r = c.post("/api/books", headers=H2, json={"title": "作者二的书", "genre": "女频"})
bk2 = r.json()["id"]
r = c.post("/api/books", headers=H3, json={"title": "作者三的书", "genre": "悬疑"})
bk3 = r.json()["id"]
check("作者二看不到作者三的书",
      "作者三的书" not in [b["title"] for b in c.get("/api/books", headers=H2).json()])
check("作者三看不到作者二的书",
      "作者二的书" not in [b["title"] for b in c.get("/api/books", headers=H3).json()])
check("作者二访问作者三的书 → 403", c.get(f"/api/books/{bk3}/bible", headers=H2).status_code == 403)
check("作者三访问自己的书 → 200", c.get(f"/api/books/{bk3}/bible", headers=H3).status_code == 200)
admin_titles = [b["title"] for b in c.get("/api/books", headers=H1).json()]
check("管理员可见全部书（%s）" % admin_titles,
      {"作者二的书", "作者三的书"} <= set(admin_titles), str(admin_titles))
check("管理员可访问他人书籍", c.get(f"/api/books/{bk2}/bible", headers=H1).status_code == 200)

print("== 7. 仪表盘 ==")
r = c.post("/api/books", headers=H1, json={"title": "躺平修仙", "genre": ""})
bk = r.json()["id"]
r = c.get("/api/dashboard", headers=H1)
check("dashboard 200", r.status_code == 200, r.text[:160])
d = r.json()
check("含全部项目（%s）" % d.get("total"), d.get("total", 0) >= 3, str(d.get("total")))
it = [x for x in d["items"] if x["id"] == bk][0]
check("含进度字段 progress", "progress" in it, str(it.keys()))
check("含评分字段 quality_score", "quality_score" in it, str(it.keys()))
check("含下一步建议", "next" in it and it["next"].get("step"), str(it.get("next")))
check("下一步=立项（空项目）", it["next"]["step"] == "立项", str(it["next"]))

print("== 8. 质量技能引擎 ==")
from apps.api import quality  # noqa: E402

bad = (
    "夜色如墨，这一刻他感到十分愤怒，心中涌起一股莫名的情绪。"
    "他愤怒地说道：“我不服！”她冷冷地回答：“你不服也没用。”"
    "他感觉到周围的空气都凝固了，非常的压抑。这一刻，仿佛命运的齿轮开始转动。"
    "这一幕让人心头涌上莫名的情绪，嘴角勾起一抹冷笑，他淡淡一笑。"
    "五味杂陈，百感交集。欲知后事如何。"
)
dg = quality.diagnose(bad, None, [{"name": "陈长安"}])
check("检出问题 > 0", len(dg.findings) > 0, str(len(dg.findings)))
names = {f.skill for f in dg.findings}
for s in ("去AI味", "Show-Don't-Tell", "对话", "章末钩子"):
    check(f"命中技能 {s}", s in names, str(names))
notes = dg.notes()
check("notes 可渲染为提示文本", "必须修" in notes or "建议改" in notes, notes[:120])

good = ("陈长安把茶盏磕在桌沿，水渍溅到手背。他没抬头。\n"
        "“这章过了。”他开口。\n"
        "门外脚步声顿住，又退远。")
dg2 = quality.diagnose(good, None, [{"name": "陈长安"}])
check("干净段落问题更少", len(dg2.findings) <= len(dg.findings),
      f"good={len(dg2.findings)} bad={len(dg.findings)}")
check("统计出字数", dg2.stats.get("words", 0) > 0, str(dg2.stats))

print("== 9. 精修技能包可用 ==")
from apps.api.skills import loader as skills  # noqa: E402

sk = skills.load("chapter_polish")
check("chapter_polish 存在", sk is not None)
if sk:
    diag_notes = dg.notes()
    r = skills.render(sk, draft="D", style_dna="S", world_view="W", characters="C",
                      foreshadows="F", issue_notes=diag_notes,
                      chapter_min=2000, chapter_max=2200)
    check("精修模板变量已注入", "D" in r and "2000" in r and "{{" not in r, r[:160])
    check("精修模板含六项标准", all(k in r for k in ("去 AI 味", "Show, Don't Tell", "章末钩子", "连续性")))

print("== 10. 导出中性化 ==")
from apps.api import exporter  # noqa: E402

check("exporter 无 to_tomato", not hasattr(exporter, "to_tomato"))
check("exporter 有 to_submission", hasattr(exporter, "to_submission"))
db = SessionLocal()
book_row = db.get(models.Book, bk)
data, mime, fname = exporter.build(book_row, [], "publish")
text = data.decode("utf-8")
check("投稿包文件名中性", "投稿包" in fname and "番茄" not in fname, fname)
check("投稿包内容无平台名", "番茄" not in text, text[:200])
db.close()

for fmt in ("txt", "md", "epub", "publish"):
    rr = c.get(f"/api/books/{bk}/export?fmt={fmt}", headers=H1)
    check(f"导出 {fmt} → 200", rr.status_code == 200, str(rr.status_code))

print("== 11. 全库中性化扫描 ==")
hits = []
for p in (ROOT / "apps").rglob("*"):
    if p.suffix in (".py", ".js", ".html", ".css", ".md") and p.is_file():
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "番茄" in txt:
            hits.append(str(p.relative_to(ROOT)))
        if "tomato" in txt.lower():
            hits.append(str(p.relative_to(ROOT)) + " (tomato)")
check("源码中无平台名残留", not hits, ", ".join(hits))

print("== 12. 配置中心：仅管理员可用（v0.5 权限收紧） ==")
# 普通用户（writer2）对配置类接口必须一律 403，读写都不行
for name, method, path in [
    ("模型列表", "GET", "/api/models"),
    ("模型 ping", "POST", "/api/models/mid_x/ping"),
    ("技能列表", "GET", "/api/skills"),
    ("技能详情", "GET", "/api/skills/chapter_polish"),
    ("设置读取", "GET", "/api/settings"),
]:
    rr = c.request(method, path, headers=H2)
    check(f"普通用户 {name} → 403", rr.status_code == 403, str(rr.status_code))

rr = c.post("/api/models", headers=H2, json={"name": "x", "provider": "openai",
                                             "base_url": "http://x", "model_id": "m"})
check("普通用户 新建模型 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.put("/api/models/mid_x", headers=H2, json={"name": "y"})
check("普通用户 改模型 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.delete("/api/models/mid_x", headers=H2)
check("普通用户 删模型 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.put("/api/skills/chapter_polish", headers=H2, json={"body": "hack"})
check("普通用户 改技能 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post("/api/skills/chapter_polish/reset", headers=H2)
check("普通用户 重置技能 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post("/api/settings", headers=H2, json={"k": "v"})
check("普通用户 写设置 → 403", rr.status_code == 403, str(rr.status_code))
rr = c.post("/api/books/import", headers=H2, json={"src_dir": "/tmp/nope", "workspace_id": "default"})
check("普通用户 导入书籍 → 403", rr.status_code == 403, str(rr.status_code))

# 管理员必须仍然畅通
check("管理员 模型列表 → 200", c.get("/api/models", headers=H1).status_code == 200)
check("管理员 技能列表 → 200", c.get("/api/skills", headers=H1).status_code == 200)
check("管理员 设置读取 → 200", c.get("/api/settings", headers=H1).status_code == 200)

print("== 13. 无主书不再对普通用户可见（隔离漏洞回归） ==")
db = SessionLocal()
ghost = models.Book(id="bk_ghost_test", title="无主幽灵书", owner_id="")
db.add(ghost)
db.commit()
db.close()
g2 = [b["title"] for b in c.get("/api/books", headers=H2).json()]
check("普通用户看不到无主书", "无主幽灵书" not in g2, str(g2))
check("普通用户访问无主书 → 403",
      c.get("/api/books/bk_ghost_test", headers=H2).status_code == 403)
ga = [b["title"] for b in c.get("/api/books", headers=H1).json()]
check("管理员仍可见无主书", "无主幽灵书" in ga, str(ga))
check("管理员可访问无主书",
      c.get("/api/books/bk_ghost_test", headers=H1).status_code == 200)
# 仪表盘同样不能泄漏
gd = c.get("/api/dashboard", headers=H2).json()
check("仪表盘不泄漏无主书",
      "无主幽灵书" not in [x["title"] for x in gd["items"]])
db2 = SessionLocal()
db2.delete(db2.get(models.Book, "bk_ghost_test"))
db2.commit()
db2.close()

print()
if FAILED:
    print(f"RESULT: {len(FAILED)} 项失败 -> " + "; ".join(FAILED))
    sys.exit(1)
print("RESULT: 全部通过 ✅")