"""RBAC 权限体系（v0.20.7 A7）。

★★★ 为什么需要 RBAC：
  当前是硬编码的二元角色（`role == "admin"`）。问题：
    1. **粒度太粗**：想给财务单独开"只能看账单"的权限做不到
    2. **改不动**：加个角色要改代码、发版
    3. **容易漏**：几十个端点各自判断 `role == "admin"`，漏一个就是越权漏洞

  RBAC 要解决的是"**谁能在什么资源上做什么操作**"。

================================ 设计 ================================

★ 三维模型：
    Subject（谁）  ×  Role（角色）  ×  Permission（权限点）
    角色 → 权限集 通过 **表驱动**（role_permissions），加角色不改代码

★ 权限点命名：`<resource>.<action>`
    book.read / book.write / book.delete
    drama.run / drama.export
    member.invite / member.remove / member.role
    admin.access / audit.view / config.update / config.export / system.health
    moderation.handle

★ 通配符：`book.*` 表示 book 下所有权限；`*` 表示全部
   → 检查时做**模式匹配**，而不是精确相等

================================ 内置角色 ================================

  owner     — 工作区所有者：全部权限（含删除工作区、改他人角色）
  admin     — 管理员：除"删除工作区/转移所有权"外全部
  editor    — 编辑：内容全权（读写删书、跑短剧），无财务/成员管理
  viewer    — 只读：只能看书与自己的账单

★ 向后兼容（重要）：
  数据库里已有用户的 role 是 `user` / `admin`。
  · `admin` → 直接对应内置 admin
  · `user`  → 映射为 editor（能创作自己的书，但无管理权限）
  这样升级后**老用户行为不变**，不需要数据迁移。

================================ 资源归属 ================================

  光有角色不够 —— 还要判断"这本书是不是我的"。
  · editor 能改**自己的**书，不能改**别人的**书
  · admin 可以改任何书（越权管理）
  这就是 `has_permission(..., owner_id=...)` 参数的作用。
"""
from __future__ import annotations

import fnmatch
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import RolePermission

log = logging.getLogger("novelforge")


# ================================================================ 权限点定义

# ★ 全部权限点（单一事实源，后台配置界面据此渲染）
PERMISSIONS: dict[str, str] = {
    # 内容
    "book.read": "查看作品",
    "book.write": "创建/编辑作品",
    "book.delete": "删除作品",
    "book.audit": "内容审计",
    "book.publish": "发布作品",
    # 短剧
    "drama.run": "生成短剧",
    "drama.export": "导出成片",
    # 成员
    "member.invite": "邀请成员",
    "member.remove": "移除成员",
    "member.role": "变更成员角色",
    # 管理
    "admin.access": "访问管理后台",
    "audit.view": "查看审计日志",
    "config.update": "修改系统配置",
    # ★ v0.25.4 I11：导出含计费/安全参数，导入可批量覆盖 —— 比 config.update 更高
    "config.export": "导出/导入配置",
    "system.health": "查看系统状态",
    "moderation.handle": "处理违规内容",
    # ★ v0.26.2：模型配置含 api_key 与计费参数，且可直接决定成本 → 高影响面
    "model.manage": "配置模型 / 供应商 / 套餐路由",
    # 工作区（最高权限）
    "workspace.manage": "管理工作区（含删除）",
}

# ★ 权限分组（UI 分组展示用）
PERMISSION_GROUPS: dict[str, list[str]] = {
    "内容": ["book.read", "book.write", "book.delete", "book.audit", "book.publish"],
    "短剧": ["drama.run", "drama.export"],
    "成员": ["member.invite", "member.remove", "member.role"],
    "管理": ["admin.access", "audit.view", "config.update", "system.health",
             "moderation.handle", "config.export", "model.manage"],
    "工作区": ["workspace.manage"],
}

# ★ 内置角色 → 权限集（默认值；DB 可覆盖）
BUILTIN_ROLES: dict[str, dict] = {
    "owner": {
        "label": "所有者",
        "desc": "工作区所有者，拥有全部权限",
        "permissions": ["*"],
    },
    "admin": {
        "label": "管理员",
        "desc": "除工作区删除、所有权转移、广告与模型配置外的全部权限",
        "permissions": [
            "book.*", "drama.*", "member.*",
            "admin.access", "audit.view", "config.update",
            "system.health", "moderation.handle",
            # ★ 刻意不含 ads.manage / model.manage：
            #   广告代码＝可注入前台的任意代码；模型配置＝直接决定成本与 Key。
            #   这两项只给 owner（§55 权限按"影响面"分层）。
        ],
    },
    "editor": {
        "label": "编辑",
        "desc": "内容创作全权（读写删自己的作品、跑短剧），无财务与成员管理",
        "permissions": ["book.read", "book.write", "book.delete",
                        "book.audit", "drama.run", "drama.export"],
    },
    "viewer": {
        "label": "只读",
        "desc": "只能查看作品与自己的账单",
        "permissions": ["book.read"],
    },
}

# ★ 旧角色名 → 新角色名（向后兼容，升级不改数据）
LEGACY_ROLE_MAP = {
    "user": "editor",
    "admin": "admin",
}

# ★ 角色优先级（数值越大权限越高；用于"能否变更他人角色"判断）
ROLE_RANK = {"viewer": 10, "editor": 30, "admin": 80, "owner": 100}


def normalize_role(role: str) -> str:
    """把旧角色名归一为新角色名（兼容老数据）。"""
    r = (role or "").strip().lower()
    return LEGACY_ROLE_MAP.get(r, r or "viewer")


def role_label(role: str) -> str:
    r = normalize_role(role)
    return (BUILTIN_ROLES.get(r) or {}).get("label", r)


def all_roles() -> list[dict]:
    """全部角色（含权限数）。"""
    out = []
    for k, v in BUILTIN_ROLES.items():
        out.append({"key": k, "label": v["label"], "desc": v["desc"],
                    "permissions": list(v["permissions"]),
                    "rank": ROLE_RANK.get(k, 0)})
    out.sort(key=lambda x: -x["rank"])
    return out


# ================================================================ 表驱动读取

def role_permissions(db: Session, role: str) -> list[str]:
    """★ 取角色的权限集（表驱动，DB 优先，内置兜底）。

    ★ 为什么"DB 优先 + 内置兜底"：
      · DB 优先 → 运营可在后台改，不用发版
      · 内置兜底 → 零依赖启动（DB 里没数据时也能跑），
                   且**避免"表被清空 = 全站无权限"的事故**
    """
    r = normalize_role(role)
    try:
        rows = db.scalars(select(RolePermission)
                          .where(RolePermission.role == r)).all()
        if rows:
            return [x.permission for x in rows]
    except Exception as e:                          # noqa: BLE001
        # 表不存在等情况 → 用内置（不能让权限系统故障导致全站不可用）
        log.warning("读取角色权限失败，降级用内置定义：%s: %s", type(e).__name__, e)
    return list((BUILTIN_ROLES.get(r) or {}).get("permissions", []))


def _match(pattern: str, perm: str) -> bool:
    """权限通配符匹配。

    · `*`      → 全部
    · `book.*` → book 下所有（含 book.read 但不含 bookx.read）
    · 精确     → 相等
    ★ 用 `.` 作分隔符而不是 fnmatch 的裸 `*`：
      否则 `book.*` 会误匹配 `bookmark.read`（前缀相同但不同资源）。
    """
    if pattern == "*":
        return True
    if pattern == perm:
        return True
    if pattern.endswith(".*"):
        return perm.startswith(pattern[:-1])        # "book." 前缀
    return False


def has_permission(db: Session, role: str, perm: str) -> bool:
    """判断角色是否拥有某权限点。"""
    if not perm:
        return False
    for p in role_permissions(db, role):
        if _match(p, perm):
            return True
    return False


def expand(patterns: list[str]) -> list[str]:
    """把通配符展开为具体权限点（后台展示用）。"""
    out: set[str] = set()
    for p in patterns:
        if p == "*":
            out.update(PERMISSIONS.keys())
        elif p.endswith(".*"):
            out.update(k for k in PERMISSIONS if k.startswith(p[:-1]))
        elif p in PERMISSIONS:
            out.add(p)
    return sorted(out)


# ================================================================ 归属判断

def can_access_book(db: Session, user, book, perm: str = "book.read") -> bool:
    """★ 资源级权限判断（角色 + 归属）。

    规则：
      1. 有 `book.*` 通配（admin/owner）→ 任意书都可访问
      2. 否则必须**是自己的书** + 拥有对应权限点
    为什么必须有这一层：光判断"是不是 editor"，无法区分
    "改自己的书"和"改别人的书" —— 后者是越权。
    """
    if user is None or book is None:
        return False
    role = getattr(user, "role", "")
    # 管理级通配：可访问全部
    perms = role_permissions(db, role)
    if any(p in ("*", "book.*") for p in perms) or has_permission(db, role, "workspace.manage"):
        return has_permission(db, role, perm) or perm == "book.read"
    # 普通用户：必须是自己的书
    if (getattr(book, "owner_id", "") or "") != (getattr(user, "id", "") or ""):
        return False
    if not (getattr(book, "owner_id", "") or ""):
        return False                                # 无主书不给普通用户
    return has_permission(db, role, perm)


def can_manage_member(db: Session, operator, target_role: str) -> tuple[bool, str]:
    """★ 能否变更某角色的成员（防"同级提权/降权"）。

    规则：
      · 必须有 member.role 权限
      · **不能操作 rank ≥ 自己的对象**（admin 不能改 owner，editor 不能改 admin）
      · owner 例外（可改任何人）
    """
    if operator is None:
        return False, "未登录"
    orole = normalize_role(getattr(operator, "role", ""))
    if not has_permission(db, orole, "member.role"):
        return False, "无变更成员角色的权限"
    if orole == "owner":
        return True, ""
    my_rank = ROLE_RANK.get(orole, 0)
    tgt_rank = ROLE_RANK.get(normalize_role(target_role), 0)
    if tgt_rank >= my_rank:
        return False, f"不能操作权限不低于自己的成员（你的角色：{orole}）"
    return True, ""


# ================================================================ 初始化

def seed_role_permissions(db: Session) -> int:
    """★ 把内置角色权限写入 DB（幂等）。

    ★ 为什么要 seed 到 DB：
      否则"表驱动"名不副实 —— 运营在后台看不到任何数据，也无从修改。
      seed 后 DB 成为可编辑源，内置定义退化为兜底。
    """
    n = 0
    try:
        existing = {r.role for r in db.scalars(select(RolePermission)).all()}
    except Exception:
        return 0
    for role, cfg in BUILTIN_ROLES.items():
        if role in existing:
            continue
        for p in cfg["permissions"]:
            db.add(RolePermission(id=f"rp_{role}_{p.replace('.', '_').replace('*', 'all')}",
                                  role=role, permission=p))
            n += 1
    if n:
        db.commit()
        log.info("RBAC 角色权限已初始化：%d 条", n)
    return n


def matrix() -> dict:
    """权限矩阵（角色 × 权限），后台展示与测试用。"""
    out = {}
    for role, cfg in BUILTIN_ROLES.items():
        expanded = expand(list(cfg["permissions"]))
        out[role] = {"label": cfg["label"], "desc": cfg["desc"],
                     "rank": ROLE_RANK.get(role, 0),
                     "patterns": list(cfg["permissions"]),
                     "permissions": expanded}
    return {"roles": out, "permissions": PERMISSIONS,
            "groups": PERMISSION_GROUPS}
