# -*- coding: utf-8 -*-
"""v1.1 F4 回归 ——「短剧成片导出（拼接+混音+硬字幕）前端可取回」。

============================ 根因 ============================
后端 F4 早已完整：
  * ``film_export.py``：分镜拼接 + 旁白混音 + 台词硬字幕（无 ffmpeg 时降级
    字节拼接），产物 mp4 落 ``drama_media/``。
  * ``main.py``：``POST .../film/{ep}``（后台任务合成）、
    ``GET .../film/capability``（ffmpeg/字体自检）。

但成片产物存在**通用 Task 表**（kind=drama_film），而
``GET .../exports`` 端点当时**只查 DramaTask 的 jianying/grid**——
成片永远进不了下载列表；前端 ``dramaApi.exports`` 又**零调用**，
「导出成片」按钮只提交后台任务，用户合成了却**无处取 mp4**。

============================ 修复 ============================
① 后端：film 合成完成时把 ``project_id`` 写进 Task.result_json（便于
   /exports 按工程过滤，旧数据无 project_id 时按该书放行）；
   ``GET .../exports`` 增查通用 Task 表 kind=drama_film，返回 ``films``。
② 前端：
   * ``drama.ts`` 修正 exports()/filmCapability() 的返回类型（旧版误写
     ``{ items }``，与后端真实契约 { exports, grids, films } 不符）；
   * DramaDetailView 加「成品下载」区（成片 mp4 / 剪映 zip / 宫格图，
     逐条下载）+ ffmpeg 能力提示（无 ffmpeg 时明示已降级为字节拼接），
     并把「导出成片」按钮接 submitFilm()（提交后刷新成品列表）。

============================ 测试（源码级） ===================
前端无 vitest，按项目惯例读源码做**可执行代码 token** 断言
（不断言注释，防"注释当实现"误判）；后端补一个 /exports 契约源码断言。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"
API = ROOT / "apps" / "api"


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


# ---------------------------------------------------------------- 后端契约
def test_exports_endpoint_returns_films():
    """★ 后端：GET /exports 必须额外返回 film 产物（查通用 Task 表 drama_film）。"""
    code = (API / "main.py").read_text(encoding="utf-8")
    # 成片产物存通用 Task（kind=drama_film），/exports 要按它查
    assert 'Task.kind == "drama_film"' in code, \
        "★ /exports 未查询通用 Task 表的 drama_film 成片产物（成片进不了下载列表）"
    # 返回体要带 films
    assert '"films": films' in code or "'films': films" in code, \
        "★ /exports 返回体未带 films 字段（前端拿不到成片）"


def test_film_task_result_carries_project_id():
    """★ 前后端接缝：film 合成完成时把 project_id 写进 Task.result_json。"""
    code = (API / "main.py").read_text(encoding="utf-8")
    # film done 分支的 result 注入了 project_id（便于 /exports 按工程过滤）
    assert '"project_id": p.id' in code or "'project_id': p.id" in code, \
        "★ film 任务 result 未注入 project_id（/exports 无法按工程筛选成片）"


# ---------------------------------------------------------------- 前端类型契约
def test_drama_ts_exports_type_matches_backend():
    """★ 防"前端类型是猜的"：exports() 必须返回 { exports, grids, films }。"""
    code = _code_only(_read("api", "modules", "drama.ts"))
    assert "exports:" in code, "exports() 返回类型缺 exports（剪映 zip）"
    assert "grids:" in code, "exports() 返回类型缺 grids（宫格图）"
    assert "films:" in code, "★ exports() 返回类型缺 films（成片 mp4）"


# ---------------------------------------------------------------- 前端入口
def test_dramadetail_has_download_section():
    """★ 核心：DramaDetailView 必须有「成品下载」区（成片 mp4 可下载）。

    旧版「导出成片」只提交后台任务，合成了却无处取文件——这里锁住下载入口。
    """
    code = _code_only(_read("views", "drama", "DramaDetailView.vue"))
    assert "exportsData" in code, "★ 缺少成品清单状态 exportsData"
    assert "loadExports" in code, "★ 缺少拉取成品清单 loadExports（exports() 零调用）"
    assert "dramaApi.exports(" in code, "★ 未真调 dramaApi.exports（拿不到下载列表）"
    # 成片可下载（下载成片 mp4）
    assert "downloadFile(" in code, "★ 成品下载区未实现下载动作"
    assert "exportsData.films" in code, "★ 「成品下载」区未列出成片 mp4（用户拿不到）"


def test_dramadetail_submits_film_then_refreshes():
    """「导出成片」按钮应接 submitFilm()（提交后台任务后刷新成品列表，mp4 立即可见）。"""
    code = _code_only(_read("views", "drama", "DramaDetailView.vue"))
    assert "submitFilm" in code, "★ 缺少 submitFilm（导出成片后不刷新，刚合成的 mp4 看不见）"
    assert "dramaApi.film(" in code, "submitFilm 未真调 dramaApi.film 提交合成任务"
    # 提交后必须刷新成品列表（否则要手动刷新才见得到 mp4）
    assert "loadExports()" in code, "★ submitFilm 未刷新成品列表（成片合成后仍看不到）"


def test_dramadetail_film_capability_hint():
    """★ 告知性：无 ffmpeg 时后端会降级为字节拼接（无字幕/无混音），UI 要让用户知情。"""
    code = _code_only(_read("views", "drama", "DramaDetailView.vue"))
    assert "filmCap" in code, "★ 缺少 ffmpeg 能力自检状态 filmCap"
    assert "dramaApi.filmCapability(" in code, "★ 未调 filmCapability（ffmpeg 降级不提示）"
    assert "filmCap.ffmpeg === false" in code, "★ 无 ffmpeg 降级提示未生效（条件写错）"
