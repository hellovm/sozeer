# -*- coding: utf-8 -*-
"""v1.1 F2 回归 ——「整书打包导出/导入（.nfbook）前端入口」。

============================ 根因 ============================
后端 ``book_pack.py``（导出/导入/预检 + /pack、/import-pack、/inspect-pack
三端点）早已**完整**（v0.19.3 F2 就做过了）：能把一本书连同设定/大纲/
人物/伏笔/纪年表/账本/短剧工程/原始文件打成 .nfbook（ZIP 机器可还原
快照，刻意排除 users/model_profiles/settings 防 API Key 泄漏）。

但**前端零引用**——典型"建了没人触发"：
  * ``book.ts`` 没有 packBook / importPack / inspectPack 方法；
  * 作品列表页（BookListView）没有"备份"按钮、没有"导入备份"入口。
删除弹窗里那句"如果只是想暂时收起，可以改用「导出备份」保留数据"
**根本点不到任何东西**。

============================ 修复 ============================
① ``book.ts`` 加 3 个方法：
   · packBook(bookId, {drama,files}) → 下载 .nfbook（http.download，带鉴权）
   · importPack(file, mode)          → 上传导入（FormData + query.mode，
     copy=新 id / merge=沿用原 id，长超时）
   · inspectPack(file)              → 只读预检（校验魔数/格式版本/结构，不落库）
   + 结果类型 PackInspectResult / PackImportResult。
② ``BookListView.vue`` 补两条入口：
   · 每本作品卡片/行「备份」→ 下载该书 .nfbook；
   · 页级「导入备份」→ 选文件 → ①预检（通过后呈现 manifest）→
     ②选 复制(copy)/覆盖(merge，merge 需二次确认) → 执行导入 → 刷新列表。

============================ 测试（源码级） ===================
前端无 vitest，按项目惯例读 .ts/.vue 源码做**可执行代码 token** 断言
（不断言注释，防"注释当实现"误判）。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"


def _read(*parts) -> str:
    return (WEB.joinpath(*parts)).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """★ 去掉整行注释，只留可执行代码——防止"注释里写了却没实现"被误判通过。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- book.ts 契约
def test_book_ts_has_pack_methods():
    """★ book.ts 必须声明 packBook / importPack / inspectPack 三方法。"""
    code = _code_only(_read("api", "modules", "book.ts"))
    assert "packBook(" in code, "★ book.ts 缺少 packBook（整书备份导出）"
    assert "importPack(" in code, "★ book.ts 缺少 importPack（整书备份导入）"
    assert "inspectPack(" in code, "★ book.ts 缺少 inspectPack（导入前只读预检）"


def test_book_ts_pack_hits_backend_endpoints():
    """★ 前后端接缝：book.ts 三个方法必须命中后端 /pack、/import-pack、/inspect-pack。"""
    code = _code_only(_read("api", "modules", "book.ts"))
    assert "/pack" in code, "packBook 未命中后端 /pack 端点"
    assert "/import-pack" in code, "importPack 未命中后端 /import-pack 端点"
    assert "/inspect-pack" in code, "inspectPack 未命中后端 /inspect-pack 端点"


def test_book_ts_import_uses_formdata_and_mode_query():
    """importPack 必须走 FormData（后端是 UploadFile）+ query.mode（copy/merge）。"""
    code = _code_only(_read("api", "modules", "book.ts"))
    assert "new FormData()" in code, "★ importPack 未用 FormData 传文件（后端是 UploadFile）"
    assert "query: { mode" in code, "★ importPack 未把 mode 作为 query 传给后端"


# ---------------------------------------------------------------- 前端入口
def test_booklist_has_backup_entry():
    """★ 核心：作品列表必须有「备份」入口并真调 packBook（旧实现零引用）。"""
    code = _code_only(_read("views", "books", "BookListView.vue"))
    assert "backupBook" in code, "★ 作品列表缺少「备份」动作"
    assert "bookApi.packBook(" in code, "★ 备份按钮未调用 bookApi.packBook（不会真下载）"
    # 下载必须真触发浏览器保存（createObjectURL + a.download）
    assert "URL.createObjectURL" in code, "备份下载未触发浏览器保存（缺 createObjectURL）"
    assert ".download =" in code, "备份下载未设置 a.download 文件名"


def test_booklist_has_import_entry():
    """★ 核心：作品列表必须有「导入备份」入口 + 预检 + copy/merge 执行。"""
    code = _code_only(_read("views", "books", "BookListView.vue"))
    assert "openImport" in code, "★ 作品列表缺少「导入备份」入口"
    assert "bookApi.inspectPack(" in code, "★ 导入未做只读预检（直接写库，缺安全关口）"
    assert "bookApi.importPack(" in code, "★ 导入未真调 bookApi.importPack（不会真导入）"
    # 选文件后必须重新预检（防"换了文件还用旧预检结果"）
    assert "importInspect.value = null" in code, "★ 换文件后未清预检结果（会用陈旧校验）"


def test_booklist_merge_requires_confirm():
    """merge（覆盖原 id）必须二次确认；copy（新 id）最安全无需确认。"""
    code = _code_only(_read("views", "books", "BookListView.vue"))
    assert "merge" in code and "copy" in code, "★ 导入未区分 copy / merge 两种模式"
    # merge 是覆盖（不可逆）→ 必须有关卡；copy 是新建（可逆）→ 直接放行
    assert "importConfirmText" in code, "★ merge 覆盖缺少二次确认关卡（误操作不可挽回）"
