# -*- coding: utf-8 -*-
"""v1.1 #307 回归 ——「版本历史弹窗一闪而逝」（AppModal 遮罩误关）。

============================ 根因 ============================
点「打开弹窗」的那一下手势会**穿透**到全屏遮罩（modal__mask），同一次
click 既触发了「开」又触发了「关」→ 弹窗刚亮起就被关掉，表现为
「版本历史」等弹窗一闪而逝。

============================ 修复 ============================
AppModal 打开瞬间置 ``justOpened=true``，250ms 内 onMaskClick 直接忽略
（``if (props.maskClosable && !justOpened.value) close()``）。

============================ 测试（源码级） ==================
前端无 vitest（web2 只跑 vue-tsc + Python 源码断言），按项目惯例读
AppModal.vue 源码，断言**可执行代码**里的防护三件套（不断言注释）。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"


def _code_only(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_appmodal_exists():
    assert (WEB / "components" / "AppModal.vue").is_file()


def test_appmodal_has_just_opened_guard():
    """★ #307 核心：必须存在 justOpened ref + 遮罩点击防护。"""
    src = (WEB / "components" / "AppModal.vue").read_text(encoding="utf-8")
    code = _code_only(src)
    assert "const justOpened = ref(false)" in code, \
        "★ 缺少 justOpened 防护态 → 遮罩点击会立即关掉刚打开的弹窗"


def test_appmodal_mask_click_shielded():
    """onMaskClick 必须在 justOpened 期间不关（穿透防护）。"""
    src = (WEB / "components" / "AppModal.vue").read_text(encoding="utf-8")
    code = _code_only(src)
    # 遮罩点击分支必须检查 justOpened
    assert "!justOpened.value" in code, \
        "onMaskClick 未屏蔽 justOpened 期间的误关（版本历史弹窗会一闪而逝）"
    # 且仍保留正常 maskClosable 语义（不是把遮罩关闭整个禁了）
    assert "props.maskClosable" in code, "★ 误删了 maskClosable 遮罩关闭语义"


def test_appmodal_open_sets_guard_with_timeout():
    """打开瞬间置 justOpened，并用 setTimeout 在 250ms 后复位。"""
    src = (WEB / "components" / "AppModal.vue").read_text(encoding="utf-8")
    code = _code_only(src)
    assert "justOpened.value = true" in code, "打开时未置 justOpened=true"
    assert "justOpened.value = false" in code, "复位逻辑缺失（防护态永不解除）"
    # 复位必须是定时器（不是同步复位，否则等同没屏蔽）
    assert "setTimeout" in code and "openGuardTimer" in code, \
        "justOpened 复位必须走 setTimeout（同步复位=防穿透失效）"
    # 卸载/关闭前清定时器（防泄漏 + 防僵尸 guard）
    assert "clearTimeout(openGuardTimer)" in code, "未清理 openGuardTimer（卸载泄漏）"
