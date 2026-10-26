# -*- coding: utf-8 -*-
"""v0.18 T4：题材包加载性能回归（验收 < 5ms）。

对应计划书 §7.2 验收项：「题材包加载 < 5ms（_DB_CACHE 缓存后）」。

阈值放得比实测宽（实测 0.0003ms），目的是**防性能退化**而不是卡死 CI ——
CI 机器可能慢/负载高，用 5ms 的验收线即可。
"""
from __future__ import annotations

import os
import statistics
import tempfile
import time

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_t4_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-t4")

BUDGET_MS = 5.0        # 计划书验收标准
MEDIAN_SAMPLES = 300


def _median_ms(fn, n=MEDIAN_SAMPLES):
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(xs)


@pytest.fixture()
def db():
    from apps.api import main
    with main.SessionLocal() as s:
        yield s


def test_cached_load_under_budget(db):
    """★ 验收：缓存命中时加载远低于 5ms。"""
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)          # 预热
    med = _median_ms(lambda: G.effective_genres(db))
    assert med < BUDGET_MS, f"缓存命中 {med:.4f}ms 超出预算 {BUDGET_MS}ms"


def test_cold_load_under_budget(db):
    """★ 验收：冷加载（含 DB 查询）也在 5ms 内。"""
    import apps.api.genres as G

    def cold():
        G.invalidate_cache()
        G.effective_genres(db)

    med = _median_ms(cold, n=100)
    assert med < BUDGET_MS, f"冷加载 {med:.4f}ms 超出预算 {BUDGET_MS}ms"


def test_cache_speeds_up_significantly(db):
    """★ 缓存确实有收益（冷/热比 > 10×，防止缓存被误删）。"""
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)
    hot = _median_ms(lambda: G.effective_genres(db), n=500)

    def cold():
        G.invalidate_cache()
        G.effective_genres(db)
    cold_ms = _median_ms(cold, n=100)
    assert cold_ms / max(hot, 1e-9) > 10, \
        f"缓存收益不足（冷 {cold_ms:.4f}ms / 热 {hot:.4f}ms）"


@pytest.mark.parametrize("fn_name,args", [
    ("metrics_for", ("urban",)),
    ("scan_patterns_for", ("urban",)),
    ("realm_dim_for", ("urban",)),
    ("realm_stops_for", ("urban",)),
    ("style_hints_for", ("urban",)),
    ("cover_anchors_for", ("urban",)),
    ("prompt_hints_for", ("urban",)),
])
def test_query_functions_fast(db, fn_name, args):
    """各查询接口都必须快（写章时高频调用）。"""
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)
    fn = getattr(G, fn_name)
    med = _median_ms(lambda: fn(*args, db))
    assert med < BUDGET_MS, f"{fn_name} {med:.4f}ms 超预算"


def test_normalize_key_fast(db):
    """中文名归一也要快（立项/建书路径高频）。"""
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)
    med = _median_ms(lambda: G.normalize_key("都市", db))
    assert med < BUDGET_MS, f"normalize_key {med:.4f}ms 超预算"


def test_catalog_fast(db):
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)
    med = _median_ms(lambda: G.catalog(db), n=200)
    assert med < BUDGET_MS, f"catalog {med:.4f}ms 超预算"


def test_export_packs_fast(db):
    import apps.api.genres as G
    G.invalidate_cache()
    G.effective_genres(db)
    med = _median_ms(lambda: G.export_packs(db), n=200)
    assert med < BUDGET_MS, f"export_packs {med:.4f}ms 超预算"


def test_scan_numbers_scales_reasonably():
    """数值扫描：1000 字正文应在 50ms 内（含全部 5 题材模式）。"""
    from apps.api import consistency as C
    text = "他掏出三百灵石，练气三层。" * 80      # ≈1000 字
    t0 = time.perf_counter()
    for _ in range(20):
        C.scan_numbers(text)
    ms = (time.perf_counter() - t0) / 20 * 1000
    assert ms < 50, f"1000 字扫描 {ms:.2f}ms，过慢"
