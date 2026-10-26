"""v0.10.0 静态一致性检查：抓 compileall 抓不到的那类 bug。

compileall 只保证语法正确，抓不到：
  - 调用了不存在的关键字参数（TypeError 只在运行到那一行才炸）
  - 模块顶层调用了后面才定义的函数（NameError，只在 import 时炸）
  - 重复的同路径路由装饰器
这些恰恰是本次部署实际炸掉的三个点，值得固化成回归。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
API = PROJECT / "apps" / "api"
MAIN = API / "main.py"
AGENT = API / "agent.py"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS {name}")
        PASS += 1
    else:
        print(f"  FAIL {name}" + (f"  → {detail}" if detail else ""))
        FAIL += 1


def collect_funcs(tree: ast.Module) -> dict:
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
    return out


def main() -> int:
    print("=== v0.10.0 静态一致性检查 ===\n")
    main_src = MAIN.read_text(encoding="utf-8")
    main_tree = ast.parse(main_src)
    funcs = collect_funcs(main_tree)

    # ---- 1. _new_task 的关键字参数与所有调用点是否匹配 ----
    new_task = funcs.get("_new_task")
    if new_task is None:
        check("_new_task 存在", False)
    else:
        a = new_task.args
        params = {p.arg for p in a.posonlyargs + a.args}
        if a.kwonlyargs:
            params |= {p.arg for p in a.kwonlyargs}
        ok = True
        detail = ""
        for node in ast.walk(main_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_new_task":
                for kw in node.keywords:
                    if kw.arg and kw.arg not in params:
                        ok = False
                        detail = (f"第 {node.lineno} 行传了 "
                                  f"{kw.arg}=，函数签名没有（可用：{sorted(params)}）")
        check("_new_task 调用参数与签名一致", ok, detail)
        check("_new_task 带 progress 参数", "progress" in params,
              f"当前签名：{sorted(params)}")

    # ---- 2. 模块顶层调用是否都在定义之后 ----
    # 关键：只统计「不在任何函数体内」的调用，也就是导入期真的会执行的那些。
    # 不能靠行首缩进判断：多行语句的后续行（如 return {...} 里的元素）顶格或半缩进都有，
    # 会把函数体内的调用误判成顶层调用（第一版就误报了 _task_dict / _run_audit_book_flow）。
    # 这里直接沿 AST 递归，进了 FunctionDef/Lambda 就停止下沉。
    toplevel_calls: list[tuple[str, int]] = []

    def walk_top(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                  ast.ClassDef)):
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                toplevel_calls.append((child.func.id, child.lineno))
            walk_top(child)

    walk_top(main_tree)
    late = []
    for fname, lineno in toplevel_calls:
        fn = funcs.get(fname)
        if fn is not None and fn.lineno > lineno:
            late.append(f"{fname}() 在第 {lineno} 行被调用，定义却在第 {fn.lineno} 行")
    check("模块顶层调用均在其定义之后", not late, "; ".join(late))
    check("_recover_tasks 改挂 startup 而非顶层调用",
          "_on_startup" in funcs and '@app.on_event("startup")' in main_src)

    # ---- 3. 路由装饰器路径不重复 ----
    seen: dict[str, int] = {}
    dups = []
    for node in ast.walk(main_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                        and dec.func.attr in ("get", "post", "put", "delete", "patch"):
                    path = ast.literal_eval(dec.args[0]) if dec.args and \
                        isinstance(dec.args[0], ast.Constant) else None
                    if path:
                        key = f"{dec.func.attr.upper()} {path}"
                        if key in seen:
                            dups.append(f"{key} (第 {seen[key]} 行与第 {node.lineno} 行)")
                        seen[key] = node.lineno
    check("无重复路由", not dups, "; ".join(dups))

    # ---- 4. agent.py 的 emit 事件不出现同名键覆盖 ----
    agent_src = AGENT.read_text(encoding="utf-8")
    agent_tree = ast.parse(agent_src)
    clash = []
    for node in ast.walk(agent_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "emit" and node.args:
            d = node.args[0]
            if isinstance(d, ast.Dict):
                keys = [k.value for k in d.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                dup = {k for k in keys if keys.count(k) > 1}
                # "kind" 出现两次 = 事件类型被业务类型覆盖
                if "kind" in dup:
                    clash.append(f"第 {node.lineno} 行 emit 的字典里 kind 重复")
    check("emit 事件字典无重复键", not clash, "; ".join(clash))

    # ---- 5. db.close() 之后不得再读 ORM 实例属性 ----
    # SQLAlchemy 2.0 抛 DetachedInstanceError，traceback 像"属性不存在"，
    # 极易误判成字段缺失。
    # 注意必须按源码行号比较先后：ast.walk 不保证顺序，
    # 无脑扫会把「close 之前」的正常读取也报出来（第一版就误报了 4 处）。
    detached = []
    for fn in [n for n in ast.walk(main_tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        closes = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "close"
                  and isinstance(n.func.value, ast.Name)
                  and n.func.value.id in ("db", "db2")]
        if not closes:
            continue
        first_close = min(closes)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Attribute) and node.lineno > first_close
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "t"
                    and node.attr in ("progress", "id", "status", "kind", "target")):
                detached.append(
                    f"{fn.name}() 第 {first_close} 行 close 后仍读 t.{node.attr}"
                    f"（行 {node.lineno}）")
                break
    check("db.close() 后不再读 ORM 实例属性", not detached, "; ".join(detached))

    # ---- 6. 跨模块同名函数必须各自齐全 ----
    # agent.py 与 main.py 都有 _task_event（两边刻意重复，import main 会成环）。
    # 本次真实故障：main.py 的 runner() 调了 agent.py 那份，运行时 NameError，
    # 任务卡在 running 直到用户手动排查。凡是新增后台任务驱动代码，
    # 都必须先确认被调函数在「本模块」里有定义。
    agent_funcs = set(collect_funcs(ast.parse(agent_src)))
    main_funcs = set(funcs)
    dup = agent_funcs & main_funcs
    check("main.py 定义 _task_event（后台任务驱动依赖它）", "_task_event" in main_funcs)
    check("main.py 定义 _append_task_event", "_append_task_event" in main_funcs)
    check("main.py 定义 _task_row / _new_task",
          {"_task_row", "_new_task"} <= main_funcs)
    if dup:
        print(f"  INFO  两模块同名函数（需各自实现）：{sorted(dup)}")

    # ---- 7. app.js 里 process / mountTaskStream / openChapterHistory 已被真正引用 ----
    app_js = (PROJECT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    for fn in ("ensureProcess", "pushProcess", "mountTaskStream", "restoreTasks",
               "reattachTask", "openChapterHistory", "diffLines", "showBookAuditResult"):
        n = app_js.count(fn)
        check(f"app.js 中 {fn} 有定义且被调用（出现 {n} 次）", n >= 2,
              "只出现 1 次 = 只有定义没调用，是死代码")

    # ---- 7. index.html 的挂载点存在 ----
    html = (PROJECT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
    for eid in ("taskBanner", "modalHistory", "chatLog"):
        check(f"index.html 含 id={eid}", f'id="{eid}"' in html)

    # ---- 8. style.css 浅色主题已生效（变量被重定义） ----
    css = (PROJECT / "apps" / "web" / "style.css").read_text(encoding="utf-8")
    check("style.css 定义浅色 --bg", "--bg: #f7f4ef" in css)
    check("style.css 定义暖色强调 --clay", "--clay:" in css)
    check("style.css 含 diff 高亮样式", ".d-add" in css and ".d-del" in css)

    # ---- 9. 版本号 ----
    check("main.py 版本号为 0.10.2", 'version="0.10.2"' in main_src)

    # ---- 10. 版本号来源唯一（v0.10.2）----
    html_v = re.search(r'id="verTag"[^>]*>([^<]*)<', html)
    check("index.html 的 verTag 不是硬编码版本号",
          html_v is not None and not re.search(r"v\d+\.\d+", html_v.group(1)),
          f"当前写死成 {html_v.group(1) if html_v else '?'}，应改为占位符并由 JS 从 /api/health 填充")
    check("app.js 有 showVersion() 从 /api/health 取真实版本",
          "function showVersion(" in app_js and "/api/health" in app_js)

    # ---- 11. 采纳接口必须回传质量警告（v0.10.2）----
    acc = main_src.split("async def accept(")[1].split("\n@app.")[0] if "async def accept(" in main_src else ""
    check("accept() 返回 warnings", '"warnings"' in acc)
    check("accept() 校验字数下限", "chapter_min" in acc)
    check("accept() 校验质检阈值", "polish_threshold" in acc)
    check("app.js 采纳后展示 warnings", "r.warnings" in app_js)
    check("app.js 低分草稿给出采纳前预警", "draft-warn" in app_js)

    print(f"\nRESULT {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())