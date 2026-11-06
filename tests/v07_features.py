"""v0.7 功能冒烟：对话历史持久化 + 系统设置（平台名/注册开关）。

用独立临时库，不污染 data/。覆盖：
1. 对话线程 CRUD（列表/新建/消息读写/删除）+ active 指针落库
2. 刷新后能从库里恢复对话上下文（S.history 等价物）
3. 系统设置 GET 带默认值、POST 白名单校验、注册开关落库
4. allow-register 返回平台名/标语，auth/me 返回 site_name
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v07_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
from apps.api.main import app  # noqa: E402

init_db = __import__("apps.api.db", fromlist=["init_db"]).init_db
init_db()
c = TestClient(app)
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + extra) if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


print("== 1. allow-register 带平台名/标语（登录页文案跟着设置走）==")
r = c.get("/api/auth/allow-register")
check("allow-register 200", r.status_code == 200, r.text[:160])
d = r.json()
check("默认允许注册", d.get("allow") is True)
check("默认平台名=织字工坊", d.get("site_name") == "织字工坊", str(d.get("site_name")))
check("默认标语非空", bool(d.get("site_tagline")), str(d.get("site_tagline")))

print("== 2. 注册并建书（首用户即管理员，可改设置）==")
r = c.post("/api/auth/register", json={"username": "admin7", "password": "secret123"})
check("注册 200", r.status_code == 200, r.text[:160])
tok = r.json()["token"]
H = {"Authorization": "Bearer " + tok}
bid = c.post("/api/books", json={"title": "v0.7 测试书"}, headers=H).json()["id"]
check("建书 200", bool(bid))

print("== 3. 对话线程：新建 → 写消息 → 列表计数 → 读取恢复 ==")
r = c.post(f"/api/books/{bid}/conversations", json={"title": ""}, headers=H)
check("新建对话 200", r.status_code == 200, r.text[:160])
cid = r.json()["id"]
check("返回 cid", bool(cid))

# 写两条消息（一轮 user + assistant）
r = c.post(f"/api/books/{bid}/conversations/{cid}/messages",
           json={"messages": [{"role": "user", "content": "主角叫什么？"},
                              {"role": "assistant", "content": "主角叫陈长安。"}]}, headers=H)
check("追加消息 200", r.status_code == 200, r.text[:160])
check("seq 推进到 2", r.json().get("count") == 2, str(r.json()))

# 列表：应能拿到这条会话且 count=2，且被标为 active
r = c.get(f"/api/books/{bid}/conversations", headers=H)
check("列表 200", r.status_code == 200, r.text[:160])
lst = r.json()
check("列表含 1 条会话", len(lst["conversations"]) == 1, str(lst))
check("会话计数=2", lst["conversations"][0]["count"] == 2, str(lst["conversations"][0]))
check("active 指向新建会话", lst["active"] == cid, str(lst.get("active")))
check("首条用户消息自动成标题", lst["conversations"][0]["title"] == "主角叫什么？", str(lst["conversations"][0]["title"]))

# 模拟刷新：重新获取消息，等价于前端 switchConv 恢复 S.history
r = c.get(f"/api/books/{bid}/conversations/{cid}/messages", headers=H)
check("读取消息 200", r.status_code == 200, r.text[:160])
msgs = r.json()["messages"]
check("恢复出 2 条消息", len(msgs) == 2, str(msgs))
check("顺序正确 user→assistant", msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", str(msgs))
check("内容无损", msgs[0]["content"] == "主角叫什么？" and msgs[1]["content"] == "主角叫陈长安。", str(msgs))

print("== 4. 第二条对话 + active 指针切换 ==")
cid2 = c.post(f"/api/books/{bid}/conversations", json={"title": "大纲脑暴"}, headers=H).json()["id"]
c.post(f"/api/books/{bid}/conversations/{cid2}/messages",
       json={"messages": [{"role": "user", "content": "三幕结构怎么搭"}]}, headers=H)
r = c.get(f"/api/books/{bid}/conversations/{cid2}/messages", headers=H)  # 打开即切 active
check("切到 cid2 后 active 更新", c.get(f"/api/books/{bid}/conversations", headers=H).json()["active"] == cid2)

print("== 5. 删除对话 + active 指针自动下移 ==")
r = c.delete(f"/api/books/{bid}/conversations/{cid2}", headers=H)
check("删除 200", r.status_code == 200, r.text[:160])
r = c.get(f"/api/books/{bid}/conversations", headers=H)
check("剩 1 条会话", len(r.json()["conversations"]) == 1, str(r.json()))
check("active 自动回到 cid", r.json()["active"] == cid, str(r.json().get("active")))

print("== 6. 系统设置：默认值 / 修改落库 / 白名单校验 ==")
r = c.get("/api/settings", headers=H)
check("设置 GET 200", r.status_code == 200, r.text[:160])
sd = r.json()
check("默认 site_name", sd.get("site_name") == "织字工坊", str(sd.get("site_name")))
check("默认 allow_register=1", sd.get("allow_register") == "1", str(sd.get("allow_register")))

r = c.post("/api/settings", json={
    "site_name": "字工坊Pro", "site_tagline": "新一代创作主编",
    "allow_register": "0"}, headers=H)
check("设置 POST 200", r.status_code == 200, r.text[:160])

r = c.get("/api/settings", headers=H)
check("平台名已落库", r.json().get("site_name") == "字工坊Pro", str(r.json().get("site_name")))
check("注册已关闭", r.json().get("allow_register") == "0", str(r.json().get("allow_register")))

# 注册开关关闭后，allow-register 应同步反映（且平台名跟着变）
r = c.get("/api/auth/allow-register")
check("allow 回退为 False", r.json().get("allow") is False, str(r.json().get("allow")))
check("allow-register 体现新平台名", r.json().get("site_name") == "字工坊Pro", str(r.json().get("site_name")))

# 非法 key 必须被白名单挡住（400 或 422 都算拒绝）
r = c.post("/api/settings", json={"evil_key": "x"}, headers=H)
check("非法设置 key 被拒", r.status_code >= 400, str(r.status_code))

print("== 7. auth/me 返回 site_name ==")
r = c.get("/api/auth/me", headers=H)
check("auth/me 200", r.status_code == 200, r.text[:160])
check("auth/me 带 site_name", r.json().get("site_name") == "字工坊Pro", str(r.json().get("site_name")))

print("\n== 汇总 ==")
print(f"FAILED: {len(FAILED)}")
for x in FAILED:
    print("  - " + x)
sys.exit(1 if FAILED else 0)
