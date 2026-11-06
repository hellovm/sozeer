"""端到端：启动 API → 配 mock 模型 → 生成第 13 章 → 闸门 → 采纳 → 登记 → Git。"""
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://127.0.0.1:8000"
MOCK = "http://127.0.0.1:8123/v1"


def p(title):
    print(f"\n=== {title} ===")


def main():
    c = httpx.Client(base_url=API, timeout=60)

    p("健康检查")
    print(" ", c.get("/api/health").json())

    books = c.get("/api/books").json()
    if not books:
        print("没有书，先导入")
        r = c.post("/api/books/import", json={
            "src_dir": r"D:/AI/workbuddyData/2026-10-10-10-07-33/novel"})
        print(" ", r.json())
        books = c.get("/api/books").json()
    book = books[0]
    bid = book["id"]
    print(f"  书：{book['title']}  已写 {book['chapters']} 章 / {book['words']} 字")

    # 幂等：清掉上一轮测试产生的第 13 章及其伏笔
    from apps.api.db import SessionLocal
    from apps.api.models import Chapter, Foreshadow
    d = SessionLocal()
    d.query(Foreshadow).filter(Foreshadow.book_id == bid, Foreshadow.planted_ch >= 13).delete()
    d.query(Chapter).filter(Chapter.book_id == bid, Chapter.idx >= 13).delete()
    d.commit()
    d.close()

    p("配置 mock 模型（走云端 API 协议）")
    for r in c.get("/api/models").json():
        c.delete(f"/api/models/{r['id']}")
    c.post("/api/models", json={
        "name": "Mock-正文", "base_url": MOCK, "model_id": "mock-chat",
        "api_key": "sk-mock", "role": "writer", "is_default": 1,
        "price_in": 2, "price_out": 8,
    })
    c.post("/api/models", json={
        "name": "Mock-主编", "base_url": MOCK, "model_id": "mock-planner",
        "api_key": "sk-mock", "role": "planner", "price_in": 4, "price_out": 16,
    })
    c.post("/api/models", json={
        "name": "Mock-审校", "base_url": MOCK, "model_id": "mock-critic",
        "api_key": "sk-mock", "role": "critic", "price_in": 1, "price_out": 4,
    })
    models = c.get("/api/models").json()
    print(f"  已配 {len(models)} 个模型，角色路由：{[m['role'] for m in models]}")
    print("  连通测试：", c.post(f"/api/models/{models[0]['id']}/ping").json())

    p("生成第 13 章（SSE 流）")
    t0 = time.time()
    events = []
    with c.stream("POST", f"/api/books/{bid}/generate",
                  json={"instruction": "本章查守纹含义并被迫接下后山巡察"}) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                events.append(ev)
                if ev["kind"] == "stage":
                    print(f"  · {ev['text']}")
                elif ev["kind"] == "brief":
                    print(f"  任务单：{ev['brief'].get('title')} — {ev['brief'].get('goal','')[:40]}")
                elif ev["kind"] == "context":
                    print(f"  上下文：约 {ev['stats'].get('approx_tokens')} tokens / 前文 {ev['stats'].get('prev_chapters')} 章")
                elif ev["kind"] == "gate":
                    r = ev["report"]
                    print(f"  闸门：分数 {r['score']} 通过={r['passed']}  字数 {r['stats'].get('words')} "
                          f"AI味 {r['stats'].get('ai_score')} 句长变异 {r['stats'].get('sent_len_cv')}")
                    for i in r["issues"][:5]:
                        print(f"     [{i['level']}][{i['rule']}] {i['message']}")
                elif ev["kind"] == "done":
                    print(f"  完成：¥{ev['cost']} / run {ev['run_id'][-8:]}")
                elif ev["kind"] == "error":
                    print("  错误：", ev["text"])
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"  事件统计：{kinds}   耗时 {time.time() - t0:.1f}s")

    done = next((e for e in events if e["kind"] == "done"), None)
    assert done and done["content"], "生成失败"
    content = done["content"]
    report = done["report"]

    p("采纳并登记")
    m = content.splitlines()[0].lstrip("# ").strip()
    title = m.split(" ", 1)[1] if " " in m else m
    idx = int("".join(ch for ch in m.split(" ")[0] if ch.isdigit()) or 0) or 13
    r = c.post(f"/api/books/{bid}/accept", json={
        "ch_idx": idx, "title": title,
        "content": content.split("\n", 1)[1].strip(),
        "gate": report, "register": True,
    }).json()
    print(" ", {k: v for k, v in r.items() if k != "summary"})
    print("  摘要：", (r.get("summary") or "")[:70], "…")

    p("状态中枢变化")
    st = c.get(f"/api/books/{bid}/state").json()
    print(f"  章节 {st['progress']['chapters']} / 字数 {st['progress']['words']} / "
          f"未回收伏笔 {st['progress']['open_foreshadows']}")
    new_fs = [f for f in st["foreshadows"] if f["planted_ch"] == 13]
    print(f"  自动登记的新伏笔 {len(new_fs)} 条：")
    for f in new_fs:
        print(f"    - {f['content'][:50]}")
    print("  人物状态：", [(x["name"], x["state"][:24]) for x in st["characters"] if x["state"]])

    p("执行档案与成本")
    runs = c.get(f"/api/books/{bid}/runs").json()
    for r_ in runs[:4]:
        print(f"  {r_['time']} {r_['kind']} {r_['model']} ¥{r_['cost']} 闸{r_['gate_score']} {r_['duration_ms']}ms")
    print(f"  累计 ¥{sum(r_['cost'] for r_ in runs):.4f}")

    p("Git 版本")
    logs = c.get(f"/api/books/{bid}/git/log").json()
    print(f"  提交数 {len(logs)}，最近 3 条：")
    for l in logs[:3]:
        print(f"    {l['hash']} {l['time']} {l['message'][:40]}")

    p("技能可编辑性")
    s = c.get("/api/skills/chapter_write").json()
    print(f"  chapter_write 正文 {len(s['body'])} 字，来源 {s['source']}")
    c.put("/api/skills/chapter_write", json={"body": s["body"] + "\n\n# 自定义追加\n"})
    s2 = c.get("/api/skills/chapter_write").json()
    print(f"  覆盖后来源 {s2['source']}，长度 {len(s2['body'])}")
    c.post("/api/skills/chapter_write/reset")
    print("  恢复后来源", c.get("/api/skills/chapter_write").json()["source"])

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
