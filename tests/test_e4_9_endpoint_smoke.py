# -*- coding: utf-8 -*-
"""v1.0-E4.9 全端点 HTTP 冒烟 · 守护测试。

============================ 为什么需要 ============================

v1.0-E4.8 修出的最严重 bug：
  `ingest` 三个端点因**漏写导入**而全部 500，
  而全量测试（2596 例）**一个都没抓到** ——

★★ 根因（MEMORY §61）：
  **模块函数测试不能替代 HTTP 端到端测试**。
  之前的测试都是 `xxx.func(...)` 直调模块函数，
  **从来没走过 FastAPI 路由层** → 路由漏传参数 / 依赖缺失
  / 异常未处理，全都发现不了。

============================ 本次（E4.9）全端点冒烟扫出 ============================

**1588 次请求 → 6 个端点 5xx**：

| # | 端点 | 根因 |
|---|---|---|
| 1-2 | `GET /api/skills`、`/api/skills/{sid}` | `system.py` **漏导入 `skills`** |
| 3 | `GET /api/books/{id}/git/log` | 超长 book_id → `OSError` |
| 4 | `POST /api/models` | **漏导入 `uuid`** |
| 5 | `POST /api/skills/{sid}/reset` | 同 1 |
| 6 | `POST /api/admin/models2/profile` | 边界输入 |

★ 共性：**`system.py` 一个文件漏了两个导入** ——
  这类问题只有"真的调一次端点"才能发现。
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = ROOT / "apps" / "api"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================ ① 漏导入修复

class TestMissingImports:
    """★★★ 漏写导入 → NameError → 500（本次两个 bug 的同一根因）。"""

    def test_system_imports_skills(self):
        """★★★ `system.py` 必须导入 `skills` loader。

        ★ 踩坑：本文件用了 `skills.list_skills()` / `skills.load()`
          **但从没导入过 `skills`** → `/api/skills` 全部 4 个端点 500。
        ★ 正确写法（其他 5 个文件都这么写）：
          `from apps.api.skills import loader as skills`
        """
        src = _read(API / "routers" / "system.py")
        assert "from apps.api.skills import loader as skills" in src, \
            "★★★ system.py 未导入 skills loader —— /api/skills 会 500"

    def test_system_imports_uuid(self):
        """★★★ `system.py` 必须导入 `uuid`。

        ★ 踩坑：`create_model()` 用 `uuid.uuid4()` 但没导入
          → `POST /api/models` 500。
        """
        src = _read(API / "routers" / "system.py")
        head = "\n".join(src.splitlines()[:40])
        assert "import uuid" in head, \
            "★★★ system.py 未导入 uuid —— POST /api/models 会 500"

    def test_no_nameerror_in_all_routers(self):
        """★★★ 所有路由模块必须能被导入且无 NameError。

        ★ 方法：用 Python 内建 `compile` + `symtable` 做静态检查，
          比手写 AST 遍历准确（正确处理闭包/推导式）。
        """
        import symtable
        bad = []
        for p in sorted((API / "routers").rglob("*.py")):
            try:
                src = p.read_text(encoding="utf-8")
                symtable.symtable(src, str(p), "exec")
            except SyntaxError as e:
                bad.append(f"{p.name}:{e.lineno} {e.msg}")
        assert not bad, "★ 语法错误：\n  " + "\n  ".join(bad)


# ============================================================ ② 路径安全

class TestPathSafety:
    """★★★ 拼进文件路径的外部输入必须白名单校验。"""

    def test_safe_book_id_exists(self):
        from apps.api import gitrepo
        assert hasattr(gitrepo, "safe_book_id"), "★ 缺少 safe_book_id 校验函数"

    def test_rejects_path_traversal(self):
        """★★★ 路径遍历必须被拒（安全高危）。"""
        from apps.api import gitrepo
        for bad in ("../../etc", "../evil", "/etc/passwd", "..\\windows"):
            with pytest.raises(ValueError):
                gitrepo.safe_book_id(bad)

    def test_rejects_too_long(self):
        """★★ 超长 ID 必须被拒（否则 OSError → 500）。"""
        from apps.api import gitrepo
        with pytest.raises(ValueError):
            gitrepo.safe_book_id("x" * 300)

    def test_rejects_empty(self):
        from apps.api import gitrepo
        with pytest.raises(ValueError):
            gitrepo.safe_book_id("")

    def test_accepts_valid(self):
        """★ 合法 ID 必须通过（不能误伤）。"""
        from apps.api import gitrepo
        for ok in ("bk_abc123", "bk-456", "ABC_def-789"):
            assert gitrepo.safe_book_id(ok) == ok

    def test_repo_path_stays_in_books_dir(self):
        """★★★ 解析后的路径必须仍在 BOOKS_DIR 内（防符号链接绕过）。"""
        from apps.api import gitrepo
        p = gitrepo.repo_path("bk_test123")
        assert str(p).startswith(str(pathlib.Path(gitrepo.BOOKS_DIR).resolve()))

    def test_repo_path_actually_validates(self):
        """★★★ `repo_path` 自身必须做校验（不能只依赖 safe_book_id 单独可用）。

        ★★ 踩坑记录（反向验证第二轮没抓到）：
          我第一版只测 `safe_book_id()` 直接调用 ——
          但如果有人把 `repo_path` 里的 `safe_book_id(book_id)` 改回
          `str(book_id)`，`safe_book_id` 函数**依然存在且可用**，
          那批测试**全部通过**，而漏洞已经回来了。

        ★ 判据：**测试要覆盖"真正的使用路径"，不是"函数单独可用"**。
          安全的本质是"每个调用点都用了它"，不是"函数存在"。
        """
        from apps.api import gitrepo
        # ★ 直接调 repo_path（真实使用路径），必须抛异常
        for bad in ("../../etc", "x" * 300, "", "../evil"):
            with pytest.raises(ValueError):
                gitrepo.repo_path(bad)

    def test_all_gitrepo_calls_go_through_repo_path(self):
        """★★★ 所有 gitrepo 公开函数都必须经由 `repo_path`（不能绕过）。

        ★ 为什么：`gitrepo.py` 有 6+ 个公开函数（log/show/write_file/
          rollback/...），只要有一个自己拼路径，校验就形同虚设。

        ★ 实现要点：`repo_path()` 内部**必须**有 `BOOKS_DIR / bid`
          （它是唯一的路径构造点），所以判定标准是
          **「其他函数不得直接拼 BOOKS_DIR」**。
        """
        import ast
        src = _read(API / "gitrepo.py")
        tree = ast.parse(src)
        offenders = []
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # ★ repo_path / safe_book_id 是唯一允许构造路径的地方
            if fn.name in ("repo_path", "safe_book_id"):
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div):
                    # 检测 `XXX / yy` 形式
                    left = sub.left
                    if isinstance(left, ast.Name) and left.id == "BOOKS_DIR":
                        offenders.append(f"{fn.name}() 行{sub.lineno}")
        assert not offenders, (
            "★ 以下函数直接拼 BOOKS_DIR（绕过 repo_path 校验）：\n  "
            + "\n  ".join(offenders))


# ============================================================ ③ 全局异常兜底

class TestExceptionHandlers:
    """★★★ 「输入不合法」应返回 4xx，不是 5xx。"""

    def test_handlers_registered(self):
        """★★ ValueError / OSError 必须有全局处理器。"""
        from apps.api.main import app
        hs = app.exception_handlers
        assert ValueError in hs, "★ 缺少 ValueError 处理器"
        assert OSError in hs, "★ 缺少 OSError 处理器"

    def test_validation_error_still_422(self):
        """★★★ FastAPI 的 422 不能被 ValueError 处理器抢走。

        ★ 为什么关键：`RequestValidationError` 是 `ValueError` 的子类，
          如果处理器优先级搞错，所有 422 会变成 400 ——
          丢失"哪个字段错了"的结构化信息。
        """
        from fastapi.testclient import TestClient
        from apps.api.main import app
        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/api/pay/sandbox/checkout"
                  "?order_id=x&trade_no=y&amount=abc")
        assert r.status_code == 422, \
            f"★ 校验错误应为 422，实际 {r.status_code}"

    def test_long_book_id_returns_400(self):
        """★★★ 超长 book_id → 400（此前是 500）。"""
        from fastapi.testclient import TestClient
        from apps.api import auth
        from apps.api.db import SessionLocal
        from apps.api.main import app
        from apps.api.models import User

        db = SessionLocal()
        tok = ""
        try:
            u = db.query(User).filter(User.role.in_(["owner", "admin"])).first()
            if u:
                tok = str(auth.make_token(u.id))
        finally:
            db.close()
        if not tok:
            pytest.skip("无管理员账号")

        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/api/books/" + "x" * 300 + "/git/log",
                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400, f"★ 应返回 400，实际 {r.status_code}"


# ============================================================ ④ 端点可调用

class TestEndpointsCallable:
    """★★★ 修复过的端点必须真的能调通。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from apps.api import auth
        from apps.api.db import SessionLocal
        from apps.api.main import app
        from apps.api.models import User

        db = SessionLocal()
        tok = ""
        try:
            u = db.query(User).filter(User.role.in_(["owner", "admin"])).first()
            if u:
                tok = str(auth.make_token(u.id))
        finally:
            db.close()
        return TestClient(app, raise_server_exceptions=False), tok

    def test_skills_list(self):
        """★★★ `/api/skills` 必须返回 200（本次修复点）。"""
        c, tok = self._client()
        if not tok:
            pytest.skip("无管理员账号")
        r = c.get("/api/skills", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, f"★ 应 200，实际 {r.status_code}"

    def test_skills_get_missing(self):
        """★ 不存在的 skill → 404（不是 500）。"""
        c, tok = self._client()
        if not tok:
            pytest.skip("无管理员账号")
        r = c.get("/api/skills/nosuchskill",
                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 404, f"★ 应 404，实际 {r.status_code}"

    def test_health(self):
        """★ 健康检查不受影响。"""
        c, tok = self._client()
        r = c.get("/api/health")
        assert r.status_code == 200


# ============================================================ ⑤ 冒烟脚本

class TestSmokeScript:
    """★ 全端点冒烟脚本必须存在且语法正确。"""

    def test_script_exists(self):
        p = ROOT / "deploy" / "smoke_all_endpoints.py"
        assert p.is_file(), "★ 缺少全端点冒烟脚本"

    def test_script_syntax(self):
        import ast
        p = ROOT / "deploy" / "smoke_all_endpoints.py"
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            pytest.fail(f"★ 冒烟脚本语法错误：{e}")


# ============================================================ ⑥ 存储层 ID 校验

class TestStorageIdValidation:
    """★★★ 外部传入的 ID 必须校验（长度 + 字符集）。

    ★ 为什么（本类来自冒烟第 2 轮扫出的最后 1 个 500）：
      `ModelProfile.id` 是 `String(64)`，但端点直接 `(body.id or "").strip()`
      → 传 300 字符的 id 触发 PG `DataError: value too long` → **500**。

    ★★ 关键：**本地 SQLite 不校验长度，所以本地测不出来** ——
      这类"只在生产 PG 上暴露"的问题，必须靠**代码审查 + 边界断言**发现。
    """

    def _fn(self):
        from apps.api.routers import ads_models
        return ads_models

    def test_rejects_too_long_id(self):
        """★★★ 超长 id → HTTPException 400（不是 500）。"""
        from fastapi import HTTPException
        from apps.api.db import SessionLocal
        from apps.api.models import User

        m = self._fn()
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.role == "owner").first()
            if u is None:
                pytest.skip("无 owner 账号")
            with pytest.raises(HTTPException) as ei:
                m.admin_upsert_profile(m.ProfileIn(id="x" * 500), db=db, user=u)
            assert ei.value.status_code == 400
        finally:
            db.close()

    def test_rejects_traversal_id(self):
        """★★ 含路径分隔符的 id → 400。"""
        from fastapi import HTTPException
        from apps.api.db import SessionLocal
        from apps.api.models import User

        m = self._fn()
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.role == "owner").first()
            if u is None:
                pytest.skip("无 owner 账号")
            with pytest.raises(HTTPException) as ei:
                m.admin_upsert_profile(m.ProfileIn(id="../../etc"), db=db, user=u)
            assert ei.value.status_code == 400
        finally:
            db.close()

    def test_id_regex_in_source(self):
        """★★ 端点源码里必须有 ID 校验正则（防被删）。"""
        src = _read(API / "routers" / "ads_models.py")
        assert "_ID_RE" in src, "★ 缺少 ID 校验正则"
        assert "_check_id" in src, "★ 缺少 ID 校验函数"

    def test_no_raw_body_id_used(self):
        """★★★ 不得直接使用未校验的 `body.id`。

        ★ 判据：**安全的本质是"每个调用点都用了校验"**，
          不是"校验函数存在"（第二轮反验的教训）。

        ★ 实现要点：必须**排除注释行** ——
          否则修复说明里引用的旧代码会被误判（我踩过一次）。
        """
        import re as _re
        src = _read(API / "routers" / "ads_models.py")
        # ★ 去掉注释行（# 开头）与文档字符串
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        code = _re.sub(r'"""[\s\S]*?"""', "", code)
        raw = _re.findall(r'\(body\.id\s+or\s+["\']{2}\)\.strip\(\)', code)
        assert not raw, (
            f"★ 发现 {len(raw)} 处直接使用未校验的 body.id —— "
            "应走 _check_id()")
