# -*- coding: utf-8 -*-
"""v0.17 B3 回归：质检结果回灌生成（评估报告缺口 5）。

缺口原文：
  "`qc_shot` 质检是「事后审计」而非「生成条件」——VLM 审图发现不一致后是重生成，
   而非在生成时约束"

实现：drama._qc_and_retry 增强
  * retry_hint 作为**生成约束**拼进 prompt（闭环）
  * ★ 重试必须保留 base_kw（ref_images/ip_weight）——修复前重试只传 prompt，
    会绕过 A1 刚接通的参考图链路，出现"改好问题、丢了角色一致性"
"""
import inspect
import json
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-b3")


# ==================================================== 签名与契约
def test_qc_and_retry_accepts_base_kw():
    """★ 核心契约：_qc_and_retry 必须接受 base_kw。"""
    from apps.api import drama
    sig = inspect.signature(drama._qc_and_retry)
    assert "base_kw" in sig.parameters, "缺 base_kw 参数"
    assert sig.parameters["base_kw"].default is None


def test_retry_preserves_generation_kw():
    """★ 核心修复：重试调用必须透传 kw（含参考图）。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert 'run_media(db, project.book_id, "image", retry_prompt, **kw)' in src, \
        "重试未透传生成条件 → 会丢角色一致性"
    assert "kw = dict(base_kw or {})" in src, "未初始化 kw"


def test_retry_hint_injected_into_prompt():
    """★ 闭环：retry_hint 必须拼进 prompt（而非只写报告）。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert "retry_prompt = f\"{base_prompt}" in src, "retry_hint 未拼进 prompt"
    assert "retry_hint" in src


def test_report_records_generation_kw():
    """报告里记录本次生成条件（便于回溯重试是否保住一致性）。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert "generation_kw" in src, "报告未记录生成条件"


def test_call_site_passes_base_kw():
    """★ 调用点必须传 base_kw（否则规则形同虚设）。"""
    from apps.api import drama
    src = inspect.getsource(drama)
    i = src.index("_qc_and_retry(db, project, scene, prompt, path,")
    seg = src[i:i + 200]
    assert "base_kw=" in seg, f"调用点未传 base_kw：{seg[:150]}"


def test_call_site_filters_expected_keys():
    """调用点应只透传与生成相关的键（避免把无关参数灌进去）。"""
    from apps.api import drama
    src = inspect.getsource(drama)
    assert '"ref_images"' in src and '"ip_weight"' in src
    assert "_qc_kw" in src


# ==================================================== 行为验证
def test_loop_structure_intact():
    """重试循环结构未被破坏（最多 max_retries 次，pass 即停）。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert "attempts < max_retries" in src
    assert "if rep.get(\"pass\"):" in src
    assert "break" in src


def test_never_crashes_on_qc_failure():
    """审图失败（异常）时退回首图，不阻塞主流程。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert "except Exception" in src, "未兜底异常"
    assert "skipped" in src, "缺 skipped 标记"


def test_behavior_retry_keeps_refs(monkeypatch):
    """★ 端到端：模拟 QC fail → 断言重试调用带上了 ref_images。"""
    import asyncio
    from apps.api import drama

    captured = []

    class FakeMedia:
        @staticmethod
        async def run_media(db, book_id, kind, prompt, **kw):
            captured.append({"prompt": prompt, **kw})
            return {"ok": True, "path": f"gen_{len(captured)}.png"}

    class FakeQC:
        calls = {"n": 0}

        @staticmethod
        async def qc_shot(db, project, scene, path):
            FakeQC.calls["n"] += 1
            # 第一次 fail（带 hint），第二次 pass
            if FakeQC.calls["n"] == 1:
                return {"ok": True, "pass": False, "retry_hint": "角色脸不一致，请加强参考"}
            return {"ok": True, "pass": True}

        @staticmethod
        def qc_media_call(*a, **k):
            pass

        @staticmethod
        def _pick_qc_model(db):
            return None

    import sys
    import apps.api.media as real_media
    import apps.api.qc as real_qc
    monkeypatch.setattr(real_media, "run_media", FakeMedia.run_media, raising=False)
    monkeypatch.setattr(real_qc, "qc_shot", FakeQC.qc_shot, raising=False)
    monkeypatch.setattr(real_qc, "qc_media_call", FakeQC.qc_media_call, raising=False)
    monkeypatch.setattr(real_qc, "_pick_qc_model", FakeQC._pick_qc_model, raising=False)

    class P:
        book_id = "bk_b3"

    async def run():
        return await drama._qc_and_retry(
            None, P(), None, "基础提示词", "first.png",
            base_kw={"ref_images": ["/api/files/a.png"], "ip_weight": 0.8})

    path, rep_json = asyncio.run(run())
    # 应发生一次重试，且重试调用带上了 ref_images
    assert len(captured) == 1, f"应有 1 次重试，实际 {len(captured)}"
    assert captured[0].get("ref_images") == ["/api/files/a.png"], \
        f"重试丢了参考图：{captured[0]}"
    assert captured[0].get("ip_weight") == 0.8
    assert "角色脸不一致" in captured[0]["prompt"], "retry_hint 未拼进 prompt"
    # 报告里应有 generation_kw
    rep = json.loads(rep_json)
    assert rep.get("retries") == 1
    assert "generation_kw" in rep


def test_behavior_pass_stops_retry(monkeypatch):
    """一次通过则不该重试。"""
    import asyncio
    from apps.api import drama

    captured = []

    class FakeMedia:
        @staticmethod
        async def run_media(db, book_id, kind, prompt, **kw):
            captured.append(prompt)
            return {"ok": True, "path": "x.png"}

    class FakeQC:
        @staticmethod
        async def qc_shot(db, project, scene, path):
            return {"ok": True, "pass": True}

        @staticmethod
        def qc_media_call(*a, **k):
            pass

        @staticmethod
        def _pick_qc_model(db):
            return None

    import sys
    import apps.api.media as real_media
    import apps.api.qc as real_qc
    monkeypatch.setattr(real_media, "run_media", FakeMedia.run_media, raising=False)
    monkeypatch.setattr(real_qc, "qc_shot", FakeQC.qc_shot, raising=False)
    monkeypatch.setattr(real_qc, "qc_media_call", FakeQC.qc_media_call, raising=False)
    monkeypatch.setattr(real_qc, "_pick_qc_model", FakeQC._pick_qc_model, raising=False)

    class P:
        book_id = "bk_b3b"

    async def run():
        return await drama._qc_and_retry(None, P(), None, "提示词", "first.png",
                                         base_kw={"ref_images": ["a.png"]})

    path, rep_json = asyncio.run(run())
    assert not captured, "已通过不该重试"
    assert json.loads(rep_json)["retries"] == 0


def test_module_doc_mentions_b3():
    """文档注明 B3 闭环 + 保留生成条件。"""
    from apps.api import drama
    src = inspect.getsource(drama._qc_and_retry)
    assert "B3" in src and "生成条件" in src
