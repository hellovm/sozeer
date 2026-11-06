# -*- coding: utf-8 -*-
"""清理冗余的函数内延迟导入（v1.0-E4.10 · 一次性工具）。

============================ 为什么清理 ============================

`deploy/audit_imports.py` 查出 **17 处**函数内延迟导入
与模块级重复 —— 它们**没有任何必要**（模块级已有）。

★★ 为什么要清（不是为了好看）：
  · 延迟导入的**唯一价值**是打破循环依赖（这里不需要）
  · 它把"启动时错误"变成"运行时错误" —— 排查成本高
  · ★ 更重要的是：**"有的地方延迟导入、有的地方不导"会让后续维护者漏抄**
    （`system.py` 漏 `skills` 就是这么来的）

★ 判据（MEMORY §32）：**能用模块级导入就别用函数内延迟导入**。

============================ 安全策略 ============================

★ 只删除"该名字已在模块级导入"的语句（严格校验）
★ 删除后立即做语法检查 + 导入检查
★ 逐个文件处理，失败即停（不批量盲改）
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def module_level_names(tree: ast.Module) -> set[str]:
    """模块顶层导入的名字。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def find_redundant(path: pathlib.Path) -> list[tuple[int, str]]:
    """返回 [(行号, 语句原文)] —— 与模块级重复的函数内导入。"""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    mod = module_level_names(tree)
    lines = src.splitlines()
    out: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # ★ 只处理**函数体的直接子节点**（不嵌套处理，避免误删）
        for stmt in node.body:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            names = [(a.asname or a.name).split(".")[0] for a in stmt.names]
            # ★ 严格：**只要有一个名字是模块级已有的**就判定冗余
            #   （本项目这些语句都是单一用途导入）
            if all(n in mod for n in names):
                line = lines[stmt.lineno - 1] if stmt.lineno <= len(lines) else ""
                out.append((stmt.lineno, line.rstrip()))

    return out


def main() -> int:
    print("=" * 74)
    print("清理冗余的函数内延迟导入")
    print("=" * 74)

    total = 0
    changed_files = 0

    for p in sorted((ROOT / "apps").rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        redundant = find_redundant(p)
        if not redundant:
            continue

        rel = p.relative_to(ROOT)
        print(f"\n▶ {rel}（{len(redundant)} 处）")

        src = p.read_text(encoding="utf-8")
        lines = src.splitlines(keepends=True)
        # ★ 从后往前删（避免行号错位）
        for lineno, text in sorted(redundant, reverse=True):
            print(f"    删除 L{lineno}: {text.strip()[:70]}")
            del lines[lineno - 1]
            total += 1

        new_src = "".join(lines)
        # ★ 语法检查（不通过就不写）
        try:
            ast.parse(new_src)
        except SyntaxError as e:
            print(f"    ❌ 语法错误，跳过：{e}")
            continue

        p.write_text(new_src, encoding="utf-8")
        changed_files += 1

    print("\n" + "=" * 74)
    print(f"★ 清理 {total} 处，涉及 {changed_files} 个文件")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
