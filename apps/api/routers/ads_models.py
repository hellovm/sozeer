# -*- coding: utf-8 -*-
"""管理后台：广告位 + 模型配置（v0.26.1 / v0.26.2）。

★ 权限：`ads.manage` / `model.manage` —— 只给 owner（理由见 rbac.py）。
★ 所有写操作都走审计（`audit.record`），与后台其它敏感操作一致。
"""
from __future__ import annotations

import re
import datetime as dt
import json
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import comfy_workflows, keyvault, model_router
from apps.api.admin_service import write_audit
from apps.api.db import get_db
from apps.api.models import (
    ModelProfile, ModelProvider, PlanModelRoute, User,
)
from apps.api.permissions import require_perm

router = APIRouter()

# ★★ v1.0 E2：站点信息缓存（见 public_site_info 的说明）
#   为什么独立于 ads 缓存：两者失效时机不同（广告变了不该清站点缓存）。
SITE_CACHE_TTL = 20.0
_site_cache: dict[str, tuple[float, dict]] = {}
_site_cache_lock = threading.RLock()


def _now() -> dt.datetime:
    """★ naive UTC（§7 时区铁律）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _parse_dt(v: str | None) -> dt.datetime | None:
    """解析 ISO 日期/时间串 → naive UTC。None/空串 = None（不设限）。

    ★ 入参是 `str | None`：前端契约 start_at/end_at 是 `string | null`
      （未排期时提交 null），pydantic 字段也放宽为 `Optional[str]`，
      若字段仍写 `str` 则 null 直接 422（用户截图实锤：新建 global_head
      广告两条 null → detail 两条 → toast 显示 [object Object]）。"""
    s = (v or "").strip()
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo:
            d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return d
    except Exception:                                   # noqa: BLE001
        return None


def _jload(s: str, default):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, type(default)) else default
    except Exception:                                   # noqa: BLE001
        return default


# ================================================================ 广告位（管理端）



# ================================================================ 模型配置（管理端）

class ProviderIn(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""          # 留空 = 不修改（新建时留空 = 无密钥）
    weight: int = 100
    enabled: int = 1


class ProfileIn(BaseModel):
    id: str = ""
    name: str = ""
    provider: str = "openai-compatible"
    base_url: str = ""
    api_key: str = ""
    model_id: str = ""
    role: str = "default"
    is_default: int = 0
    enabled: int = 1
    context_window: int = 128000
    price_in: float = 2.0
    price_out: float = 8.0
    temperature: float = 0.85
    max_tokens: int = 8192
    providers: list[ProviderIn] = Field(default_factory=list)


@router.get("/api/admin/models2")
def admin_models_overview(db: Session = Depends(get_db),
                          _: User = Depends(require_perm("model.manage"))):
    """模型配置总览：模型槽 + 多供应商端点 + 套餐路由 + 元数据。"""
    data = model_router.admin_view(db)
    data["templates"] = comfy_workflows.list_templates()
    # ★ v1.0-E4.11（G6）：云端视频通道并排进模板清单（kind=cloud-video）
    from apps.api import cloud_video as _cv
    data["templates"] = list(data["templates"]) + _cv.list_cloud_templates()
    return data


@router.post("/api/admin/models2/profile")
def admin_upsert_profile(body: ProfileIn, db: Session = Depends(get_db),
                         user: User = Depends(require_perm("model.manage"))):
    """新建/更新一个模型槽（含其下的多个供应商端点）。

    ★ api_key 处理规则（与旧接口一致）：
      空 = 清除；留空不传 = 不动。这里用"空串=清除"简化（后台表单总是全量提交）。
    """
    if body.role and body.role not in model_router.ROLES:
        raise HTTPException(400, f"未知角色 {body.role}")
    if body.provider and body.provider not in model_router.PROVIDER_KINDS:
        raise HTTPException(400, f"未知 provider 类型 {body.provider}")

    # ★★ 修复（v1.0-E4.9）：id 必须校验长度与字符集
    #
    #   ★ 问题（全端点冒烟扫出）：`ModelProfile.id` 是 `String(64)`，
    #     但这里直接 `(body.id or "").strip()` 就用 ——
    #     传 300 字符的 id 会触发 PG `DataError: value too long`
    #     → **500**（本地 SQLite 不校验长度，所以本地测不出来）。
    #
    #   ★ 同类风险：下面 `sub_id` 也是外部传入的 id（ModelProvider.id）。
    #
    #   ★★ 判据：**外部传入的 ID 必须校验**（长度 + 字符集）——
    #     这与 gitrepo 的 book_id 是同一类问题（拼进存储层的输入）。
    _ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def _check_id(v: str, label: str) -> str:
        s = (v or "").strip()
        if s and not _ID_RE.match(s):
            raise HTTPException(
                400, f"{label} 不合法（只允许字母数字下划线连字符，1-64 位）")
        return s

    pid = _check_id(body.id, "模型 ID")
    is_new = not pid
    if is_new:
        pid = f"md_{uuid.uuid4().hex[:12]}"
    row = db.get(ModelProfile, pid)
    if row is None:
        row = ModelProfile(id=pid)
        db.add(row)

    row.name = (body.name or "")[:128]
    row.provider = body.provider or "openai-compatible"
    row.base_url = (body.base_url or "")[:512]
    row.model_id = (body.model_id or "")[:128]
    row.role = body.role or "default"
    row.enabled = 1 if body.enabled else 0
    row.context_window = max(1000, int(body.context_window or 128000))
    row.price_in = float(body.price_in or 0)
    row.price_out = float(body.price_out or 0)
    row.temperature = float(body.temperature or 0.85)
    row.max_tokens = max(1, int(body.max_tokens or 8192))
    if body.api_key:
        keyvault.set_api_key(row, body.api_key)

    # ★ is_default 是"角色内唯一"：设为默认时清掉同角色其它默认
    row.is_default = 1 if body.is_default else 0
    if row.is_default:
        for x in db.scalars(select(ModelProfile).where(
                ModelProfile.role == row.role)):
            if x.id != row.id:
                x.is_default = 0

    db.flush()

    # ★ 供应商端点：全量替换（表单一次性提交全部端点，语义最清晰）
    old = list(db.scalars(select(ModelProvider).where(
        ModelProvider.profile_id == row.id)))
    old_by_id = {o.id: o for o in old}
    keep: set[str] = set()
    for p in body.providers:
        pname = (p.name or "").strip()
        burl = (p.base_url or "").strip()
        if not burl and not pname:
            continue                                   # 空行跳过
        # ★ 同样校验（ModelProvider.id 也是 String(64)，外部可传）
        sub_id = _check_id(p.id, "供应商 ID") or model_router.new_id()
        keep.add(sub_id)
        pr = old_by_id.get(sub_id)
        if pr is None:
            pr = ModelProvider(id=sub_id, profile_id=row.id)
            db.add(pr)
        pr.name = pname[:128]
        pr.base_url = burl[:512]
        pr.weight = max(0, int(p.weight or 0))
        pr.enabled = 1 if p.enabled else 0
        if p.api_key:
            keyvault.set_api_key(pr, p.api_key)
    # 删除本次未提交的旧端点
    for oid, o in old_by_id.items():
        if oid not in keep:
            db.delete(o)

    db.commit()
    write_audit(db, operator=user, action="model.upsert", target_type="model",
                target_id=row.id,
                detail={"role": row.role, "provider": row.provider,
                        "providers": len(body.providers), "is_new": is_new})
    return {"id": row.id, "is_new": is_new}


@router.delete("/api/admin/models2/profile/{mid}")
def admin_delete_profile(mid: str, db: Session = Depends(get_db),
                         user: User = Depends(require_perm("model.manage"))):
    row = db.get(ModelProfile, mid)
    if not row:
        raise HTTPException(404, "模型不存在")
    for p in db.scalars(select(ModelProvider).where(ModelProvider.profile_id == mid)):
        db.delete(p)
    for r in db.scalars(select(PlanModelRoute)):
        ids = _jload(r.profile_ids, [])
        if mid in ids:
            r.profile_ids = json.dumps([i for i in ids if i != mid])
    db.delete(row)
    db.commit()
    write_audit(db, operator=user, action="model.delete", target_type="model",
                target_id=mid, detail={"name": row.name})
    return {"ok": True}








@router.get("/api/admin/models2/templates")
def admin_workflow_templates(_: User = Depends(require_perm("model.manage")),
                             kind: str = ""):
    """内置 ComfyUI 工作流模板清单（管理员选模板即可，无需自己搭节点）。

    ★ v1.0-E4.11：kind=cloud-video 返回云端视频通道；缺省时本地+云端一起给。
    """
    items = comfy_workflows.list_templates(kind)
    from apps.api import cloud_video as _cv
    if kind in ("", "cloud-video"):
        items = list(items) + _cv.list_cloud_templates()
    return {"items": items}


class ComfyTestIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    profile_id: str = ""


async def _comfyui_checks(host: str, key: str) -> dict:
    """★ ComfyUI 连通性探测（被 test-comfyui 与 test-model 共用，逻辑只写一份）。

    检查 ① 能否连上 ② /system_stats 是否返回 ③ 模型文件是否齐全。
    失败信息要**可执行**（告诉管理员下一步查什么），不只是报错码。
    """
    import httpx
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    out: dict = {"host": host, "ok": False, "checks": []}

    def _add(name: str, ok: bool, detail: str) -> None:
        out["checks"].append({"name": name, "ok": ok, "detail": detail})

    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.get(f"{host}/system_stats", headers=headers)
        if r.status_code == 401 or r.status_code == 403:
            _add("连接", False, f"HTTP {r.status_code}：token 不正确或未授权")
            out["hint"] = "检查 API Token（ComfyUI 启动参数 --enable-cors-header 或 token 配置）"
            return out
        if r.status_code >= 400:
            _add("连接", False, f"HTTP {r.status_code}: {r.text[:150]}")
            out["hint"] = "确认地址端口正确、ComfyUI 已启动"
            return out
        _add("连接", True, "系统状态接口正常")
        try:
            st = r.json()
            devs = st.get("devices") or []
            if devs:
                d0 = devs[0]
                _add("设备", True,
                     f"{d0.get('name', 'GPU')} · 显存 {round((d0.get('vram_total') or 0)/1073741824, 1)}GB")
        except Exception:                               # noqa: BLE001
            pass

        # 检查模型文件（老版本 ComfyUI 无此接口 → 不阻断）
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r2 = await cli.get(f"{host}/object_info/UNETLoader", headers=headers)
            if r2.status_code < 400:
                info = r2.json()
                names = (((info or {}).get("UNETLoader") or {})
                         .get("input", {}).get("required", {}).get("unet_name", [[]])[0])
                names = names if isinstance(names, list) else []
                ltx = [n for n in names if "ltx" in str(n).lower()]
                if ltx:
                    _add("LTX 模型", True, f"找到 {len(ltx)} 个：" + ", ".join(map(str, ltx[:3])))
                else:
                    _add("LTX 模型", False,
                         "未找到 ltx 模型文件（需放入 models/diffusion_models/）")
                    out.setdefault("hint",
                                   "下载 ltx-2.5-22b-distilled-transformer-bf16.safetensors "
                                   "到 ComfyUI/models/diffusion_models/")
        except Exception:                               # noqa: BLE001
            pass

        # ★★ E4.15 安全自检：公网暴露 + 无 token 保护（SIP 扫描事件的代码层回应）。
        # 背景：DGX Spark 的 ComfyUI 8188 暴露公网，被外部 SIP 端口扫描
        # （OPTIONS sip:<ip> SIP/2.0 → InvalidURLError 刷屏）。自检两问：
        #   ① 地址是否公网可达（非私网/回环段）
        #   ② 是否配了 API Key（ComfyUI 无 token 时任何人可提交 /prompt 白嫖 GPU）
        # fail-soft：任何异常都不阻断连通性主结论，只多一条提示。
        try:
            from urllib.parse import urlparse as _up
            _h = (_up(host).hostname or "").lower()
            _private = (
                _h in ("localhost", "", "0.0.0.0")
                or _h.startswith("127.")
                or _h.startswith("10.")
                or _h.startswith("192.168.")
                or _h.startswith("fd")
                or _h.endswith(".local")
                or (_h.startswith("172.") and 16 <= int(_h.split(".")[1] or 0) <= 31)
            )
            if not _private:
                if key:
                    _add("安全", True, "公网地址 + 已配 API Key（受保护）")
                else:
                    _add("安全", False,
                         "公网地址且未配 API Key —— 任何人可提交任务占用你的 GPU")
                    out.setdefault("hint",
                                   "建议：① 安全组/防火墙仅放行平台服务器 IP；"
                                   "② ComfyUI 加 token 保护并在模型中心填 API Key；"
                                   "③ 或改绑 --listen 127.0.0.1 走内网")
        except Exception:                               # noqa: BLE001
            pass

        out["ok"] = all(c["ok"] for c in out["checks"] if c["name"] == "连接")
        if out["ok"] and not out.get("hint"):
            out["hint"] = "连接正常，可以保存并开始使用"
    except Exception as e:                              # noqa: BLE001
        _add("连接", False, f"{type(e).__name__}: {e}")
        out["hint"] = ("无法连接。请确认：① 地址含 http:// 前缀 "
                       "② 端口正确（默认 8188）③ 服务端能访问该内网地址")
    return out


async def _openai_compat_checks(host: str, key: str, model_id: str) -> dict:
    """★ OpenAI 兼容类（agnus / openai-compatible / minimax / custom）连通性探测。

    ★ 探针选 `{base_url}/models`（list models，最通用的只读端点）：
      - agnus base_url = `https://apihub.agnes-ai.com/v1` → 探 `/v1/models` 命中；
      - 若 base_url 未带 /v1，自动补探 `{base_url}/v1/models`（一次多探不报错）。
    命中后若配置了 model_id，顺带核对它是否在返回列表里（可执行的下一步建议）。
    """
    import httpx
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    out: dict = {"host": host, "ok": False, "checks": []}

    def _add(name: str, ok: bool, detail: str) -> None:
        out["checks"].append({"name": name, "ok": ok, "detail": detail})

    urls = [f"{host}/models"]
    if not host.endswith("/v1"):
        urls.append(f"{host}/v1/models")
    try:
        last = None
        async with httpx.AsyncClient(timeout=15.0) as cli:
            for u in urls:
                last = await cli.get(u, headers=headers)
                if last.status_code < 400:
                    break
        if last is None:
            _add("连接", False, "无响应")
            out["hint"] = "无法连接"
            return out
        if last.status_code in (401, 403):
            _add("认证", False, f"HTTP {last.status_code}：API Key 不正确或无权限")
            out["hint"] = "检查 API Key（密钥填错 / 已过期 / 该 key 无此模型的访问权限）"
            out["ok"] = False
            return out
        if last.status_code < 400:
            _add("连接", True, f"{last.url} 可连通")
            if model_id:
                try:
                    data = last.json()
                    ids = [(m.get("id") if isinstance(m, dict) else str(m))
                           for m in (data.get("data") or [])]
                    if model_id in ids:
                        _add("模型", True, f"返回列表含 {model_id}")
                    else:
                        _add("模型", False,
                             f"返回列表未含 {model_id}（共 {len(ids)} 个模型）")
                        out["hint"] = ("API 可连通，但你填的模型 ID 不在返回列表里"
                                      "—— 请核对模型 ID 是否写对（可参考上方列出的可用模型）")
                except Exception:                          # noqa: BLE001
                    _add("模型", True, "无法解析模型列表（不影响连通判断）")
            out["ok"] = True
            out.setdefault("hint", "连接正常，可以保存并开始使用")
        else:
            _add("连接", False, f"HTTP {last.status_code}: {last.text[:150]}")
            out["hint"] = ("无法连通。请确认 ① 地址带 http(s):// 前缀 ② 端口正确 "
                          "③ 是否需要 /v1 路径（如 https://api.example.com/v1）")
    except Exception as e:                              # noqa: BLE001
        _add("连接", False, f"{type(e).__name__}: {e}")
        out["hint"] = "无法连接（超时或服务端访问不到该 API 地址）。请确认地址正确、网络可达"
    return out


@router.post("/api/admin/models2/test-comfyui")
async def admin_test_comfyui(body: ComfyTestIn, db: Session = Depends(get_db),
                             _: User = Depends(require_perm("model.manage"))):
    """★ 测试 ComfyUI 连通性（配置向导的关键一步）。

    ★ 为什么必须有"测试连接"：
      管理员填完 IP/端口/token 后，如果不测就直接保存，
      第一次生成失败时根本不知道是"地址错"还是"工作流错"。
    检查项：① 能否连上 ② /system_stats 是否返回 ③ 模型文件是否齐全。
    """
    host = (body.base_url or "").strip().rstrip("/")
    if not host:
        # 没传就取已保存的配置
        prof = db.get(ModelProfile, body.profile_id) if body.profile_id else None
        host = (prof.base_url if prof else "").strip().rstrip("/")
    if not host:
        raise HTTPException(400, "请填写 ComfyUI 地址（如 http://localhost:8188）")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host

    key = (body.api_key or "").strip()
    if not key and body.profile_id:
        prof = db.get(ModelProfile, body.profile_id)
        if prof:
            key = keyvault.get_api_key(prof)
    return await _comfyui_checks(host, key)


class ModelTestIn(BaseModel):
    provider: str = ""
    base_url: str = ""
    api_key: str = ""        # 前端只有掩码，通常留空 → 靠 profile_id 从 keyvault 取真实 key
    profile_id: str = ""
    model_id: str = ""


@router.post("/api/admin/models2/test-model")
async def admin_test_model(body: ModelTestIn, db: Session = Depends(get_db),
                           _: User = Depends(require_perm("model.manage"))):
    """★ 通用模型连通性测试（按 provider 分支）。

    ★★ 为什么独立于 test-comfyui（agnus 误报根因）：
      原实现所有非 ComfyUI 模型也走 test-comfyui（打 /system_stats，ComfyUI 专有）
      → agnus 是 OpenAI 兼容 API，/system_stats 必然 404 → 误报"ComfyUI 已启动"。
      现在：`comfyui*` 走 /system_stats；其余（agnus/openai-compatible/minimax/custom）
      走 /models 只读探针。

    ★ 前端只有 api_key 掩码，真实密钥靠 profile_id 从 keyvault 还原（绝不回传前端）。
    """
    provider = (body.provider or "").strip()
    host = (body.base_url or "").strip().rstrip("/")
    key = (body.api_key or "").strip()
    model_id = (body.model_id or "").strip()

    # ★ 凭 profile_id 还原已保存配置（表单在编辑态/列表行点"测试"）。
    #
    # 旧版条件是 `if not (provider or host or model_id) and body.profile_id`
    # —— 但前端列表行"测试"发的是 {provider, base_url, model_id} 全填、
    # 唯独不带 api_key（明文绝不回传前端，前端只有掩码）→ 三个字段非空，
    # 该条件恒为 False → key 保持空 → 拿空 key 打供应商 → 401。
    # （用户实锤：本机 curl 带 key 通，后台点测试 401。）
    #
    # 修复：key 永远只存 keyvault。只要"key 缺失 + 有 profile_id"就还原；
    # 三个配置字段缺失的也顺带从 profile 补齐（编辑态全留空的场景）。
    if body.profile_id and (not key or not (provider and host and model_id)):
        prof = db.get(ModelProfile, body.profile_id)
        if prof:
            key = key or keyvault.get_api_key(prof)
            provider = provider or (prof.provider or "")
            host = host or (prof.base_url or "").strip().rstrip("/")
            model_id = model_id or (prof.model_id or "")

    if not host:
        raise HTTPException(400, "请填写 API 地址（如 https://api.example.com/v1）")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host

    if provider.startswith("comfyui"):
        return await _comfyui_checks(host, key)
    return await _openai_compat_checks(host, key, model_id)


@router.get("/api/admin/models2/template/{key}")
def admin_workflow_template(key: str,
                            _: User = Depends(require_perm("model.manage"))):
    """取某个模板的完整 workflow JSON（供后台预览/下载）。"""
    wf = comfy_workflows.get_workflow(key)
    if not wf:
        raise HTTPException(404, "模板不存在")
    return {"key": key, "workflow": wf}




@router.get("/api/public/site")
def public_site_info(db: Session = Depends(get_db)):
    """前台介绍页所需的站点信息（平台名/标语/价格/联系方式）。

    ★ 只返回**可公开**的字段，绝不回传任何密钥/内部配置。

    ★★ v1.0 E2 压测修正：加**进程级短缓存**。

      实测：本端点在 100 并发下 P95 714ms，到 200 并发直接崩
      （成功率 13%）。原因不是连接池，而是**每请求要跑 5 次查询**：
        · `get_setting` × 4（站点名/标语/注册开关/联系方式/ICP）
          —— 未注册进配置中心的键会走**兼容路径直接查 DB**，不吃缓存
        · `select(Plan)` × 1
      单请求下这点开销看不出来（6ms），100 并发就是每秒 800 次查询。

      ★ 为什么可以放心缓存：
        ① 站点信息是**运营级配置**，改一次可能几个月不动
        ② 前端在**每个公开页**都会请求它（PV 量最大），收益显著
        ③ 晚 20 秒看到新标语完全可接受
      ★ 后台改站点设置后调 `invalidate_site_cache()` 立即生效。
    """
    ck = "__site__"
    nowt = time.time()
    with _site_cache_lock:
        hit = _site_cache.get(ck)
        if hit is not None and (nowt - hit[0]) < SITE_CACHE_TTL:
            return hit[1]

    from apps.api.services.settings import get_setting
    _feats: list = []

    payload = {
        # ★★ v1.0-E4.3：改用配置中心的**规范键**（`system.*`）。
        #
        #   原实现用的是旧键（`site_name` / `site_slogan` / `site_icp`），
        #   而配置中心 v0.25.4 起统一为 `system.*` ——
        #   于是**后台改了站点名，这里读的却是旧键，永远读不到**
        #   （E4.1 已修过一次同类问题，这是残留的第二处）。
        #
        #   ★ `get_setting` 内部会做键名归一化，传新键即可。
        "site_name": get_setting(db, "system.site_name", "溯字 SOZEER"),
        "slogan": get_setting(db, "system.site_tagline",
                              "AI 长篇创作与短剧生成工作台"),
        "register_open": get_setting(db, "system.allow_register", "1") == "1",
        "contact_email": get_setting(db, "system.contact_email", ""),
        "icp": get_setting(db, "system.icp", ""),
    }
    with _site_cache_lock:
        _site_cache[ck] = (nowt, payload)
    return payload


def invalidate_site_cache() -> None:
    """清站点信息缓存（后台改站点设置后调用）。"""
    with _site_cache_lock:
        _site_cache.clear()


def site_cache_info() -> dict:
    with _site_cache_lock:
        return {"size": len(_site_cache), "ttl": SITE_CACHE_TTL}
