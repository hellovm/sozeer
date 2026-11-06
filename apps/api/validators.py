"""入册合法性守卫（v0.7.1）。

背景：本地《躺平修仙》库中发现两类数据污染——
1. 大纲卖点条目（"1. 退婚流的反转"）被当成人物入册；
2. 伏笔追踪行（"沈家破落真相（第 1 章）……待回收"）因 importer 的
   `parse_realm` 全文兜底被错分进 world_settings 的「境界」分类。
本模块提供统一的判定函数，供导入器、人物入册工具、自动入册共用。
"""
from __future__ import annotations

import re

# 伏笔/剧情条目的行式特征（来自伏笔追踪.md："1. **XX**（第 N 章）：……— 待回收"）
_FS_SIGNATURE = re.compile(r"第\s*\d+\s*章|待回收|回收中|已回收|伏笔|—\s*待")
# 大纲/卖点小节的句式特征（含箭头的推进链、方法论词汇）
_OUTLINE_SIGNATURE = re.compile(
    r"→|↔|=>|卖点|爽点|正反馈|发动机|内核|套路|节奏|大纲|对位|反转点|金手指")
# "1." "2、" 这类编号前缀
_NUM_PREFIX = re.compile(r"^\s*\d+\s*[\.、．)）]")

# 境界/力量体系名称的合法尾字（凡境/灵海境/炼气期/金丹/至尊/筑基期……）
_REALM_TAIL = re.compile(r"(境|阶|级|段|层|重|转|品|尊|期|丹|元|婴|神|仙|圣|王|宗师|体)$")


def valid_character_name(name: str) -> bool:
    """人物名必须是干净的短名词：无换行、无编号前缀、无大纲/伏笔句式特征。"""
    n = (name or "").strip()
    if not n or "\n" in n or len(n) > 16 or _NUM_PREFIX.match(n):
        return False
    if _OUTLINE_SIGNATURE.search(n) or _FS_SIGNATURE.search(n):
        return False
    return True


def invalid_character_reason(name: str) -> str:
    """给人看（也给模型看）的拒绝原因，便于对话里自我纠正。"""
    n = (name or "").strip()
    if not n:
        return "人物名为空"
    if "\n" in n:
        return "人物名含换行（疑似把整段大纲当成了人物）"
    if len(n) > 16:
        return "人物名超过 16 字（真实人物名不该这么长）"
    if _NUM_PREFIX.match(n):
        return "人物名以编号开头（疑似大纲条目）"
    if _OUTLINE_SIGNATURE.search(n):
        return "人物名含大纲/卖点句式特征"
    if _FS_SIGNATURE.search(n):
        return "人物名含伏笔条目特征"
    return "人物名不合法"


def is_realm_like(key: str, value: str = "") -> bool:
    """world_settings「境界」条目必须干净：不带伏笔特征、无编号/换行。

    命名本身放宽（各书境界体系差异大），主要靠排除伏笔/剧情特征防串味。
    """
    k = (key or "").strip()
    if not k or "\n" in k or len(k) > 20 or _NUM_PREFIX.match(k):
        return False
    if _FS_SIGNATURE.search(k) or _FS_SIGNATURE.search(value or ""):
        return False
    return True


def looks_like_realm_name(key: str, dim: str = "") -> bool:
    """更严格的"像实力体系名"判定，供闸门提取白名单时过滤。

    v0.15 题材解耦：`_REALM_TAIL` 只认仙侠系后缀（境/阶/丹/婴…），
    对都市「职级」体系（见习专员/主管/总监/总裁）会全部误杀。
    因此：
      * dim == "境界" 或未指定（老调用）→ 用原判定（零回归）
      * 其他维度名（职级/等级/军衔…）→ 放宽为 is_realm_like（只排除伏笔/编号特征）
    """
    k = (key or "").strip()
    if not is_realm_like(k):
        return False
    # 非仙侠体系：后缀词表不适用，只做"干净度"校验（已在 is_realm_like 内完成）
    if dim and dim != "境界":
        return True
    return bool(_REALM_TAIL.search(k) or "境" in k)
