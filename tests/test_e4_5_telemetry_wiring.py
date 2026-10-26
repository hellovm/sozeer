# -*- coding: utf-8 -*-
"""v1.0-E4.5 数据采集链路补全的守护测试。

============================ 修复的三个缺口 ============================

核查发现（`deploy/audit_telemetry.py` 实测）：

① **心跳没有调度器** → 整条链路是死的
   `maybe_heartbeat()` 注释写着「由调度器/启动钩子调用」，
   但**调度器从来没写过** —— 唯一调用点是管理端手动端点。
   实测：SaaS 侧 `telemetry_instances` 表 **0 条**。

② **功能埋点调用点为 0**
   `telemetry.track()` 定义完整，但没有任何业务调用点
   → 上报包里的 `feature` 永远是空 `{}`。

③ **缺"采纳信号"** → 不知道生成质量好不好
   只知道"生成了多少"。★ 生成次数多 ≠ 质量好（可能反复重试）。

============================ 核心判据 ============================

★★ **"基础设施建好了" ≠ "链路是活的"**
   本项目反复出现这个模式（MEMORY §49 队列、§24 可配置能力）。

★★ **隐私与分析性可以兼得**：只采集"计数 + 分桶 + 哈希"，
   既能做统计（改了多少），又无法还原内容（改成什么）。
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = ROOT / "apps" / "api"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    r"""去注释与文档字符串。"""
    import re as _re
    text = _re.sub(r"<!--[\s\S]*?-->", "", text)
    text = _re.sub(r'"""[\s\S]*?"""', "", text)
    text = _re.sub(r"'''[\s\S]*?'''", "", text)
    text = _re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith(("#", "//", "*")):
            continue
        out.append(line)
    return "\n".join(out)


# ============================================================ ① 心跳调度器

class TestHeartbeatScheduler:
    """★★★ 心跳必须有自动调度（否则整条链路是死的）。"""

    def test_scheduler_functions_exist(self):
        """★★ 必须有 start/stop/status 三个函数。"""
        from apps.api import telemetry as T
        for fn in ("start_scheduler", "stop_scheduler", "scheduler_status"):
            assert hasattr(T, fn), f"★ telemetry 缺少 {fn}"

    def test_scheduler_is_daemon(self):
        """★★ 必须是 **daemon 线程**。

        ★ 为什么：非 daemon 线程会阻塞进程退出 ——
          `systemctl stop` 会挂住直到超时，运维体验很差。
        """
        src = _strip_comments(_read(API / "telemetry.py"))
        assert "daemon=True" in src, \
            "★ 调度线程不是 daemon —— 会阻塞进程退出"

    def test_scheduler_is_idempotent(self):
        """★★ 重复调用只能启动一个线程（防 reload 模式起多个）。

        ★ 为什么：uvicorn --reload 会重复触发 startup；
          不幂等则每次 reload 都多一个线程 → 心跳次数翻倍。
        """
        src = _strip_comments(_read(API / "telemetry.py"))
        assert "is_alive()" in src, "★ start_scheduler 未做幂等判断"
        assert "already" in src, "★ 未返回'已启动'标识"

    def test_scheduler_wired_in_startup(self):
        """★★★ 必须在 main.py 的 startup 里真的调用。

        ★ 这是本次修复的**核心**：函数存在但没人调用 = 链路是死的。
        """
        src = _strip_comments(_read(API / "main.py"))
        assert "start_scheduler" in src, \
            "★★★ main.py 未调用 start_scheduler —— 链路依然是死的"

    def test_scheduler_starts_even_when_disabled(self):
        """★★ 无论开关状态都启动线程。

        ★ 为什么：运营可能在运行期通过后台打开遥测，
          此时不该要求重启服务。线程内部会检查 is_enabled()。
        """
        src = _read(API / "main.py")
        # 找到调用点，确认它不在 `if _on:` 分支内
        i = src.find("start_scheduler()")
        assert i > 0, "★ 未找到调用点"
        # 往回看 20 行，确认没有缩进在 if 里
        seg = src[max(0, i - 1500):i]
        assert "无论开关是否开启" in seg or "if _on:" not in seg[-400:], \
            "★ 调度器启动被包在开关判断里 —— 运行期打开遥测会失效"

    def test_loop_swallows_exceptions(self):
        """★★★ 循环体内异常必须全吞。

        ★ 判据：遥测是**旁路功能**，绝不能因它崩掉主服务。
          线程里的未捕获异常会导致线程静默死掉（更难发现）。
        """
        src = _read(API / "telemetry.py")
        i = src.find("_heartbeat_loop")
        assert i > 0
        seg = src[i:i + 1800]
        assert "except Exception" in seg, "★ 循环体未捕获异常"
        assert "已忽略" in seg or "pass" in seg, "★ 异常未静默处理"


# ============================================================ ② 功能埋点

class TestFeatureInstrumentation:
    """★★ 功能埋点必须真的有业务调用点。"""

    def test_track_has_callers(self):
        """★★★ `telemetry.track()` 必须有业务调用点。

        ★ 修复前实测：调用点 = 0 → `feature` 字段永远是空 `{}`。
        """
        hits = []
        for p in (API).rglob("*.py"):
            s = p.read_text(encoding="utf-8")
            # 排除定义处与测试
            if p.name in ("telemetry.py",):
                continue
            if "telemetry.track" in s or "_tm.track" in s or "tm.track" in s:
                hits.append(str(p.relative_to(ROOT)))
        assert hits, "★★★ telemetry.track 没有业务调用点 —— 功能埋点是空转"

    def test_task_event_recorder_exists(self):
        """★ 应有统一的任务事件记录函数。"""
        src = _strip_comments(_read(API / "orchestrator" / "graph.py"))
        assert "_record_task_event" in src, \
            "★ 缺少统一埋点函数（否则要在每个调用点各写一遍）"

    def test_instrumentation_in_unified_exit(self):
        """★★ 埋点放在**统一出口**（_track）而非各调用点。

        ★ 为什么：`_track` 是所有 LLM 调用的必经之路，
          在这里埋点能用最小改动覆盖全部环节。
        """
        src = _strip_comments(_read(API / "orchestrator" / "graph.py"))
        i = src.find("def _track(")
        assert i > 0
        seg = src[i:i + 1500]
        assert "_record_task_event" in seg, \
            "★ _track 未接入埋点"


# ============================================================ ③ 采纳信号

class TestAdoptionSignals:
    """★★★ 采纳信号 —— 判断生成质量的核心依据。"""

    def test_adoption_module_exists(self):
        """★★ 必须有独立的采纳采集模块。"""
        p = API / "adoption.py"
        assert p.is_file(), "★ 缺少 apps/api/adoption.py"

    def test_records_generate_and_adopt(self):
        """★★ 必须同时记录"生成"与"采纳"。

        ★ 判据：只有两者都有，才算得出采纳率。
          有生成无采纳 = 质量差。
        """
        from apps.api import adoption as A
        for fn in ("record_generate", "record_adopt", "record_discard",
                   "record_regenerate"):
            assert hasattr(A, fn), f"★ 缺少 {fn}"

    def test_buckets_not_raw_values(self):
        """★★★ 必须分桶，不能上报精确值。

        ★ 为什么：精确字数可以反推内容长度特征；
          分桶后仍能看分布，但无法定位到具体章节。
        """
        from apps.api import adoption as A
        # 不同输入应落到有限个桶
        buckets = {A.bucket_words(n) for n in (10, 400, 900, 1500, 2500,
                                               4000, 8000, 50000)}
        assert len(buckets) <= 8, f"★ 字数分桶过多：{buckets}"
        # 非法输入要有兜底
        assert A.bucket_words(-5) == "0"
        assert A.bucket_words("abc") == "unknown"

    def test_edit_ratio_buckets(self):
        """★★ 修改幅度分桶（判断质量的关键指标）。"""
        from apps.api import adoption as A
        assert A.bucket_edit_ratio(0.0) == "几乎未改"
        assert A.bucket_edit_ratio(0.05) == "1-10%"
        assert A.bucket_edit_ratio(0.95) == ">90%"
        assert A.bucket_edit_ratio("bad") == "unknown"

    def test_prompt_fingerprint_is_hash(self):
        """★★★ 提示词**只存哈希**，绝不存原文。

        ★ 为什么：提示词是核心资产，且可能含用户自定义内容。
          哈希足以做版本分组对比（同版本同哈希）。
        """
        from apps.api import adoption as A
        fp = A.prompt_fingerprint("这是一段提示词")
        assert fp.startswith("sha256:"), "★ 提示词不是哈希格式"
        assert "提示词" not in fp, "★★ 哈希里混入了原文！"
        # 同样输入应得同样哈希（可做版本对比）
        assert fp == A.prompt_fingerprint("这是一段提示词")
        # 不同输入不同哈希
        assert fp != A.prompt_fingerprint("另一段提示词")
        # 空值安全
        assert A.prompt_fingerprint("") == ""

    def test_wired_into_accept_path(self):
        """★★★ 必须接入**采纳的唯一出口**。

        ★ 为什么放核心函数而非路由层：采纳路径不止一条
          （直接采纳 / 任务流采纳 / 批量采纳），
          放核心函数才能穷举所有出口（同 MEMORY §10）。
        """
        src = _read(API / "orchestrator" / "graph.py")
        i = src.find("async def accept_draft")
        assert i > 0
        seg = src[i:i + 2200]
        assert "record_adopt" in seg, \
            "★★★ accept_draft 未接入采纳埋点"

    def test_wired_into_payload(self):
        """★★ 采纳数据必须进入上报包。"""
        src = _strip_comments(_read(API / "telemetry.py"))
        assert '"adoption"' in src, "★ build_payload 未包含 adoption 字段"

    def test_collect_output_is_aggregated(self):
        """★ 输出必须是**聚合结果**，不是逐条原始事件。

        ★ 为什么：逐条上报体积大，且时序信息可用于行为分析。
        """
        from apps.api import adoption as A
        A.reset()
        A.record_generate("t1", words=100, ok=True)
        A.record_adopt("t1", before_words=100, after_words=110)
        out = A.collect_adoption()
        assert "by_role" in out, "★ 缺少按环节聚合"
        assert "t1" in out["by_role"], "★ 未按 role 分组"
        assert out["by_role"]["t1"]["gen"] == 1
        assert out["by_role"]["t1"]["adopt"] == 1
        A.reset()

    def test_never_contains_content(self):
        """★★★ 采集结果里**绝不能出现正文内容**。"""
        from apps.api import adoption as A
        A.reset()
        secret = "这是一段绝密的小说正文内容ABC123"
        A.record_generate("t2", words=len(secret), prompt=secret, ok=True)
        A.record_adopt("t2", before_words=100, after_words=120, prompt=secret)
        blob = str(A.collect_adoption())
        assert secret not in blob, "★★★ 采集结果含正文内容！"
        assert "绝密" not in blob, "★★★ 采集结果含正文片段！"
        A.reset()

    def test_discard_reason_is_whitelisted(self):
        """★★ 丢弃原因必须是**枚举值**（防调用方误传自由文本）。"""
        from apps.api import adoption as A
        A.reset()
        A.record_discard("t3", reason="用户手滑输入了一段正文")
        snap = str(A.snapshot())
        assert "手滑" not in snap, "★★ 自由文本混入采集结果"
        assert "unknown" in snap, "★ 未做白名单兜底"
        A.reset()


# ============================================================ ④ 隐私披露

class TestDisclosureSync:
    """★★★ 新增采集字段必须同步到隐私披露（这是给用户的承诺）。"""

    def test_disclosure_covers_adoption(self):
        """★★ disclosure 必须说明采纳采集。"""
        from apps.api import telemetry as T
        d = T.disclosure()
        joined = str(d["collect"])
        assert "采纳" in joined, "★ 披露未说明采纳采集"
        assert "分桶" in joined or "区间" in joined, \
            "★ 未说明数据是分桶/区间（用户有权知道）"

    def test_never_collect_covers_new_risks(self):
        """★★ 反向承诺必须覆盖新增风险点。"""
        from apps.api import telemetry as T
        d = T.disclosure()
        nc = str(d["never_collect"])
        assert "提示词" in nc, "★ 未承诺「不上报提示词原文」"
        assert "精确字数" in nc or "区间" in nc, "★ 未承诺「不报精确字数」"

    def test_mechanism_documents_scheduler(self):
        """★ 机制说明必须包含调度信息（用户有权知道何时上报）。"""
        from apps.api import telemetry as T
        d = T.disclosure()
        assert "scheduler" in d["mechanism"], "★ 未说明自动上报机制"


# ============================================================ ⑤ 运维可见性

class TestOpsVisibility:
    """★★ 运维必须能一眼看出"链路是否是活的"。"""

    def test_stats_endpoint_includes_scheduler(self):
        """★★ 统计端点必须返回调度器状态。

        ★ 为什么：此前"没数据"时无法区分
          "遥测没开" / "调度器没跑" / "上报失败" —— 三种原因完全不同。
        """
        src = _strip_comments(_read(API / "routers" / "feedback_routes.py"))
        assert "scheduler_status" in src, "★ 统计端点未返回调度器状态"
        assert "hint" in src, "★ 缺少一句话诊断"

    def test_stats_endpoint_includes_adoption(self):
        """★ 统计端点应返回采纳计数（实时判断质量）。"""
        src = _strip_comments(_read(API / "routers" / "feedback_routes.py"))
        assert "adoption" in src, "★ 统计端点未返回采纳计数"


# ============================================================ ⑥ 导入有效性

class TestImports:
    """★ 新模块必须能被导入（防 ImportError → 端点 500）。"""

    def test_new_modules_import(self):
        import importlib
        for m in ("apps.api.adoption", "apps.api.telemetry"):
            try:
                importlib.import_module(m)
            except Exception as e:                      # noqa: BLE001
                pytest.fail(f"★ {m} 导入失败：{type(e).__name__}: {e}")

    def test_deferred_imports_resolve(self):
        """★ 延迟导入也必须可解析（AST 扫描，同 E4.4）。"""
        import ast
        import importlib
        import importlib.util

        bad = []
        for p in (API).rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not (node.module or "").startswith("apps.api"):
                    continue
                try:
                    mod = importlib.import_module(node.module)
                except Exception as e:                  # noqa: BLE001
                    bad.append(f"{p.relative_to(ROOT)}:{node.lineno} "
                               f"{node.module}: {e}")
                    continue
                for alias in node.names:
                    if alias.name == "*" or hasattr(mod, alias.name):
                        continue
                    try:
                        if importlib.util.find_spec(
                                f"{node.module}.{alias.name}") is not None:
                            continue
                    except (ImportError, ValueError, ModuleNotFoundError):
                        pass
                    bad.append(f"{p.relative_to(ROOT)}:{node.lineno} "
                               f"{node.module} 没有 {alias.name}")
        assert not bad, "★ 延迟导入失败：\n  " + "\n  ".join(bad[:10])


# ============================================================ ⑦ 采集键名审计

class TestCounterKeyAudit:
    """★★★ 采集结果的**键名**也不能泄漏内容。

    ============================ 为什么补这一条 ============================

    第一版 `test_never_contains_content` **抓不到**我注入的缺陷
    （把提示词哈希换成提示词原文）—— 因为那条测试走的是
    `prompt=` 参数路径，而注入点在**哈希分支内部**，
    两者恰好错开。

    ★★ 教训：**"检查值"不够，还要"检查键"**。
      因为本模块的实现方式是 `_counters[f"{prefix}.{x}"] += 1` ——
      如果 x 是内容，它会变成**键名的一部分**，而不在值里。

    ★ 判据：**任何进入 `_counters` 的字符串，都必须是
      "可枚举的分类值"或"哈希"**，绝不能是自由文本。
    """

    def test_全部键名都不含自由文本(self):
        """★★★ 注入含中文的提示词，检查键名是否泄漏。"""
        from apps.api import adoption as A
        A.reset()
        secrets = [
            "绝密情节：主角其实是反派",
            "ABC123SECRET",
            "第一章的完整大纲内容",
        ]
        for s in secrets:
            A.record_generate("t", words=100, prompt=s, ok=True)
            A.record_adopt("t", before_words=100, after_words=110, prompt=s)
            A.record_discard("t", reason=s)

        snap = A.snapshot()
        keys_blob = " ".join(snap.keys())
        for s in secrets:
            assert s not in keys_blob, f"★★★ 键名泄漏内容：{s}"
        # ★ 中文整体检查（任何中文字符出现在键名里都是可疑的）
        import re as _re
        cn = _re.findall(r"[\u4e00-\u9fff]+", keys_blob)
        # ★ 例外：分桶标签本身含中文（"几乎未改"）—— 白名单放行
        allowed_cn = {"几乎未改"}
        unexpected = [c for c in cn if c not in allowed_cn]
        assert not unexpected, f"★★★ 键名含非预期中文：{unexpected}"
        A.reset()

    def test_键名只能是安全的字符集(self):
        """★★ 键名只允许 ASCII 字母数字与有限符号。

        ★ 为什么：安全字符集是"内容泄漏"的**通用护栏** ——
          即使新增字段忘了过滤，非 ASCII 内容也进不去。
        """
        from apps.api import adoption as A
        import re as _re
        A.reset()
        # 各种恶意/意外输入
        for bad in ("中文内容", "emoji😀", "换行\n注入", "空格 分隔", "a" * 100):
            A.record_generate(bad, words=1, prompt=bad, ok=True)
            A.record_discard(bad, reason=bad)
        safe = _re.compile(r"^[A-Za-z0-9._:\-<>%+]+$")
        bad_keys = [k for k in A.snapshot() if not safe.match(k)]
        assert not bad_keys, f"★ 键名含不安全字符：{bad_keys[:5]}"
        A.reset()


# ============================================================ ⑧ 提示词指纹链路

class TestPromptFingerprintChain:
    """★★★ 提示词指纹必须能从生成侧传到采纳侧。

    ============================ 为什么需要 ============================

    要回答"这次改了 prompt，生成质量变好没有？"必须把
    **提示词版本**与**产出质量**关联起来。

    ★ 难点：**采纳发生在生成之后**（可能跨请求、跨天）。
      指纹必须被**持久化**，否则到采纳那一刻已经丢了。

    ★ 载体：任务台账 `Task.result_json`（唯一贯穿生成→采纳的存储）。
    """

    def test_flow_context_has_prompt_hash(self):
        """★★ FlowContext 必须有 prompt_hash 字段。"""
        src = _strip_comments(_read(API / "orchestrator" / "graph.py"))
        assert "prompt_hash" in src, "★ FlowContext 缺少 prompt_hash"
        assert "prompt_hash: str" in src, "★ 字段未声明"

    def test_track_accepts_prompt(self):
        """★★ `_track` 必须接受 prompt 参数。"""
        src = _strip_comments(_read(API / "orchestrator" / "graph.py"))
        assert 'prompt: str = ""' in src, "★ _track 未接受 prompt 参数"

    def test_all_call_sites_pass_prompt(self):
        """★★★ 所有埋点调用点都必须传 prompt。

        ★ 为什么：漏传会导致该环节永远没有版本信息 ——
          A/B 分析时那个环节是瞎的。
        """
        src = _read(API / "orchestrator" / "graph.py")
        import re as _re
        # 找所有 _track(fc, "xxx", ...) 调用（排除定义行）
        calls = _re.findall(r'_track\(fc,\s*"[^"]+"[^)]*\)', src, _re.S)
        assert calls, "★ 未找到调用点"
        bad = [c[:70].replace("\n", " ") for c in calls
               if "prompt" not in c]
        assert not bad, f"★ 以下调用点未传 prompt：{bad}"

    def test_fingerprint_persisted_to_task(self):
        """★★★ 指纹必须写入任务结果（否则传不到采纳侧）。

        ★★ 踩坑记录：第一版用 `'"prompt_hash"' in src` 判断 ——
          **抓不到**把字段改名成 `"prompt_hash_removed"` 的注入
          （子串包含！）。
          → 改用**正则精确匹配 JSON 键**：`"prompt_hash"\s*:`
        """
        import re as _re
        src = _read(API / "agent.py")
        # ★ 精确匹配 JSON 键（后跟冒号），避免 "prompt_hash_xxx" 蒙混过关
        assert _re.search(r'"prompt_hash"\s*:', src), \
            "★★★ agent.py 未把指纹写入 result_json —— 采纳侧拿不到"

    def test_adopt_reads_fingerprint(self):
        """★★ 采纳侧必须从任务结果读回指纹。

        ★ 同样用精确匹配（`.get("prompt_hash")` 后必须是 `)` 或空白）
        """
        import re as _re
        src = _read(API / "main.py")
        assert _re.search(r'\.get\("prompt_hash"\)', src), \
            "★ 采纳侧未读取 prompt_hash"

    def test_stream_path_idempotent(self):
        """★★ 流式路径埋点必须幂等（防 done 事件重复触发）。

        ★ 为什么：多轮工具调用场景下 done 可能触发多次，
          不防重会让"生成次数"虚高 → 污染采纳率分母。
        """
        src = _read(API / "orchestrator" / "graph.py")
        assert "_gen_tracked" in src, "★ 流式埋点缺少幂等标志"


class TestFingerprintPrivacy:
    """★★★ 指纹必须是哈希且可对比。"""

    def test_hash_not_content(self):
        from apps.api import adoption as A
        raw = "这是一段很长的提示词内容，包含剧情设定与写作准则"
        fp = A.prompt_fingerprint(raw)
        assert fp.startswith("sha256:")
        assert raw not in fp and "提示词" not in fp
        assert len(fp) <= 40, f"★ 指纹过长（可能存了原文）：{len(fp)}"

    def test_same_prompt_same_hash(self):
        """★ 同版本提示词必须得到同一指纹（否则无法分组对比）。"""
        from apps.api import adoption as A
        a = "提示词 A"
        assert A.prompt_fingerprint(a) == A.prompt_fingerprint(a)

    def test_different_prompt_different_hash(self):
        """★ 改了提示词必须得到不同指纹（否则 A/B 无意义）。"""
        from apps.api import adoption as A
        assert A.prompt_fingerprint("版本 1") != A.prompt_fingerprint("版本 2")

    def test_prompt_grouping_works(self):
        """★★ 必须能按提示词版本分组统计。"""
        from apps.api import adoption as A
        A.reset()
        p1, p2 = "提示词版本A" * 3, "提示词版本B" * 3
        for _ in range(3):
            A.record_generate("chapter_write", prompt=p1, ok=True)
        for _ in range(2):
            A.record_generate("chapter_write", prompt=p2, ok=True)
        A.record_adopt("chapter_write", before_words=100, after_words=105,
                       prompt=p1)
        out = A.collect_adoption()
        bp = out.get("by_prompt") or {}
        assert len(bp) == 2, f"★ 未按版本分组：{bp}"
        # 版本 A 应有 3 次生成 1 次采纳
        va = next(v for k, v in bp.items() if v["gen"] == 3)
        assert va["adopt"] == 1, f"★ 采纳数不对：{va}"
        A.reset()
