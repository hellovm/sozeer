# -*- coding: utf-8 -*-
"""v0.15 G4 回归：consistency.py 接入题材包（度量衡 + 扫描模式）。

核心验收（计划书 §五 G4）：
  * 仙侠书记账结果与改前**逐字节一致**（零回归铁证）
  * 都市书可用「金钱/职级/粉丝」记账
  * 无题材信息时回退仙侠默认表（老书零影响）
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g4")


# ==================================================== 模块级符号兼容（老调用点零改动）
def test_module_level_metrics_still_available():
    """main.py 等 6 处引用模块级 METRICS/SCAN_PATTERNS，必须仍可用。"""
    from apps.api import consistency as C
    assert isinstance(C.METRICS, dict) and C.METRICS
    assert isinstance(C.SCAN_PATTERNS, list) and C.SCAN_PATTERNS


def test_module_metrics_equals_xianxia():
    """模块级 METRICS 必须 = genres 里 xianxia 包（同源，不会漂移）。"""
    from apps.api import consistency as C, genres
    assert C.METRICS == genres.GENRES["xianxia"]["metrics"]


def test_module_metrics_byte_identical_to_v1410():
    """★ 零回归铁证：模块级 METRICS 与 v0.14.10 硬编码值逐字节一致。"""
    from apps.api import consistency as C
    v1410 = {
        "金钱": {"units": ["枚", "块", "两", "文", "灵石", "金币", "银两", "万"], "cumulative": True},
        "经验": {"units": ["点", "经验"], "cumulative": True},
        "年龄": {"units": ["岁", "年"], "cumulative": True},
        "功法": {"units": ["层", "重", "阶", "品"], "cumulative": True},
        "级别": {"units": ["级", "阶", "段", "星"], "cumulative": True},
        "声望": {"units": ["点"], "cumulative": True},
        "其他": {"units": [], "cumulative": True},
    }
    assert C.METRICS == v1410


def test_module_scan_patterns_compile_and_match():
    """模块级 SCAN_PATTERNS 仍是 (维度, compiled_regex)，且行为不变。"""
    from apps.api import consistency as C
    dims = [d for d, _ in C.SCAN_PATTERNS]
    assert dims == ["金钱", "经验", "年龄", "功法", "级别"]
    # 行为验证：仙侠文本能扫出 5 类
    got = C.scan_numbers("他有三百灵石，练气三层，五级，十六岁，经验值一百点")
    assert "金钱" in got and got["金钱"][0]["value"] == 300
    assert "功法" in got and got["功法"][0]["value"] == 3
    assert "年龄" in got and got["年龄"][0]["value"] == 16


# ==================================================== 题材感知取数
def test_metrics_for_theme_xianxia_matches_default():
    from apps.api import consistency as C
    assert C.metrics_for_theme("xianxia") == C.METRICS
    assert C.metrics_for_theme(None) == C.METRICS, "空题材必须回退默认"


def test_scan_patterns_for_theme_urban():
    """都市题材扫出「金钱/职级/年龄/粉丝」，且不含仙侠的『功法』。"""
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme("urban")
    dims = [d for d, _ in pats]
    assert "金钱" in dims and "职级" in dims
    assert "功法" not in dims, "都市文不该有功法维度"

    got = C.scan_numbers("他月薪三万块，职级三级，粉丝一百万", patterns=pats)
    assert "金钱" in got, f"都市文本未扫出金钱：{got}"
    assert "职级" in got
    assert any(h["value"] == 30000 for h in got["金钱"])


def test_scan_numbers_backward_compatible():
    """scan_numbers(text) 不传 patterns 时行为与改前一致。"""
    from apps.api import consistency as C
    a = C.scan_numbers("一百灵石")
    b = C.scan_numbers("一百灵石", patterns=C.SCAN_PATTERNS)
    assert a == b, "省略 patterns 应与显式传默认模式结果相同"


def test_scan_numbers_bad_pattern_skipped():
    """坏正则不该让整批扫描崩溃。"""
    from apps.api import consistency as C
    import re
    bad = [("坏", re.compile(r"(unclosed"))] if False else []
    # 构造一个会被 _compile_scan_patterns 过滤的坏模板
    pats = C._compile_scan_patterns([("好", r"{NUM}\s*岁"), ("坏", r"{NUM}[")])
    assert len(pats) == 1, "坏正则应被跳过"
    assert pats[0][0] == "好"


# ==================================================== 按书取数（读 book.theme）
@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _mk_book(db, bid, theme="", genre=""):
    from apps.api.models import Book
    b = db.get(Book, bid)
    if b:
        db.delete(b)
        db.commit()
    b = Book(id=bid, title="测试书", genre=genre, theme=theme, owner_id="u1")
    db.add(b)
    db.commit()
    return b


def test_metrics_for_book_reads_theme(db):
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_urban", theme="urban")
    m = C.metrics_for_book(db, b)
    assert "职级" in m and "功法" not in m


def test_metrics_for_book_fallback_to_genre(db):
    """theme 为空时读 genre（实测老书只有 genre 自由文本）。"""
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_genre", theme="", genre="系统流 + 轻松搞笑 + 修仙 + 反套路")
    m = C.metrics_for_book(db, b)
    assert "功法" in m, "含'修仙'的 genre 应命中仙侠包"


def test_metrics_for_book_no_info_falls_back(db):
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_empty", theme="", genre="")
    assert C.metrics_for_book(db, b) == C.METRICS, "无题材信息必须回退默认"


def test_scan_patterns_for_book_urban(db):
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_urban2", theme="urban")
    pats = C.scan_patterns_for_book(db, b)
    dims = [d for d, _ in pats]
    assert "职级" in dims and "功法" not in dims


def test_add_ledger_accepts_theme_metric(db):
    """★ 都市书能用「职级」记账（不被降级为『其他』）。"""
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_ledger", theme="urban")
    row = C.add_ledger(db, b.id, "主角", "职级", 1, ch_idx=1, unit="级")
    assert row.metric == "职级", f"都市书『职级』不该被降级，实际 {row.metric}"

    # 仙侠书里「职级」不在表内 → 应降级为「其他」
    b2 = _mk_book(db, "bk_g4_ledger2", theme="xianxia")
    row2 = C.add_ledger(db, b2.id, "主角", "职级", 1, ch_idx=1)
    assert row2.metric == "其他", "仙侠书里『职级』不是合法维度，应归入『其他』"


def test_add_ledger_xianxia_unchanged(db):
    """★ 零回归：仙侠书的『功法』记账行为与改前一致。"""
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_xianxia", theme="xianxia")
    r1 = C.add_ledger(db, b.id, "主角", "功法", 1, ch_idx=1, unit="层")
    r2 = C.add_ledger(db, b.id, "主角", "功法", 2, ch_idx=2)
    assert r1.metric == "功法" and r2.metric == "功法"
    assert r2.balance == 3.0, f"余额推导错误：{r2.balance}"


def test_check_text_vs_ledger_theme_aware(db):
    """对账走题材感知扫描：都市文本的金钱不会因『功法』模式漏扫。"""
    from apps.api import consistency as C
    b = _mk_book(db, "bk_g4_check", theme="urban")
    C.add_ledger(db, b.id, "主角", "金钱", 5000, ch_idx=1, unit="元")
    # 正文写出远超余额的金额 → 应报可疑
    issues = C.check_text_vs_ledger(db, b.id, 2, "他账户里有 500000 元。")
    assert isinstance(issues, list)
    assert any(i["scope"] == "金钱" for i in issues), f"未报出金钱异常：{issues}"
