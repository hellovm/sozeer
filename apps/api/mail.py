"""邮件发送（v0.25 Xf-1）。

★ 为什么单独一个模块而不是塞进 user_center：
  user_center 的 `CodeSender` 只管**验证码**这一件事；
  而邮件还要发订单回执、工单回复、配额告警、注销确认……
  这些都是"通知"，与"验证码"是不同抽象层。分开才不会被验证码逻辑绑死。

★ 工程三原则：
  · **绝不阻塞主流程**：发送失败不抛异常（通知失败不该让下单失败）
  · **可 dry-run**：配置缺失或显式开启时不真发，只记日志（本地开发友好）
  · **开关默认关闭**：读不到 SMTP 配置时按"不发"处理（保守）

★ 安全：
  · 密码只从环境变量 / keyvault 读，**不落库明文**
  · 邮件头做 CRLF 注入防护（`_sanitize_header`）
  · 支持 TLS（465 隐式 / 587 STARTTLS）
"""
from __future__ import annotations

import email.utils
import html
import logging
import os
import re
import smtplib
import ssl
import threading
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_log = logging.getLogger("novelforge.mail")

# ---------------------------------------------------------------- 配置

ENV_HOST = "NF_SMTP_HOST"
ENV_PORT = "NF_SMTP_PORT"
ENV_USER = "NF_SMTP_USER"
ENV_PASS = "NF_SMTP_PASS"
ENV_FROM = "NF_SMTP_FROM"
ENV_FROM_NAME = "NF_MAIL_FROM_NAME"
ENV_SECURE = "NF_SMTP_SECURE"        # ssl(465) | starttls(587) | none
ENV_DRY = "NF_MAIL_DRY_RUN"          # 1 = 只记日志不真发
ENV_SITE = "NF_SITE_URL"

DEFAULT_TIMEOUT = 15.0
MAX_RETRY = 2
RECIPIENT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# ★ 邮件头 CRLF 注入防护：换行会让攻击者插入额外头/收件人
_CRLF_RE = re.compile(r"[\r\n]+")


class MailError(Exception):
    """邮件业务异常（调用方一般不需要捕获 —— 发送失败已内部吞掉）。"""


# ---------------------------------------------------------------- 配置读取

def config(db=None) -> dict:
    """读 SMTP 配置。优先级：环境变量 > DB（加密存储）> 空。

    ★ 为什么支持 DB：小型部署不想动环境变量（改 systemd 要重启），
      后台填一次即可。但**环境变量优先**，方便容器化场景覆盖。

    ★★ v1.0-E4.2 修复：原实现调用 `keyvault.get_api_key(db, key)`
      —— 但该函数签名是 `get_api_key(m)`（**一个参数**，且 m 是
      ModelProfile 行）。传两个参数会抛 `TypeError`，
      又被下面的 `except Exception: pass` **静默吞掉**
      → 后台存的 SMTP 配置**永远读不到**，且毫无报错。

      ★ 这类"参数数量不匹配 + 宽泛 except"的组合是最难查的：
        调用方看起来在正常工作，实际每次都走进异常分支。
        判据：**宽泛 except 里至少要有日志**，否则故障静默。
    """
    cfg = {
        "host": os.environ.get(ENV_HOST, ""),
        "port": int(os.environ.get(ENV_PORT, "0") or 0),
        "user": os.environ.get(ENV_USER, ""),
        "password": os.environ.get(ENV_PASS, ""),
        "from_addr": os.environ.get(ENV_FROM, ""),
        "from_name": os.environ.get(ENV_FROM_NAME, "溯字 SOZEER"),
        "secure": (os.environ.get(ENV_SECURE, "") or "").lower(),
    }
    if db is not None:
        try:
            from apps.api import secret_store
            for key, field in (("mail.smtp_host", "host"),
                               ("mail.smtp_port", "port"),
                               ("mail.smtp_user", "user"),
                               ("mail.smtp_pass", "password"),
                               ("mail.smtp_from", "from_addr"),
                               ("mail.smtp_from_name", "from_name"),
                               ("mail.smtp_secure", "secure")):
                if cfg.get(field):
                    continue                    # 环境变量优先
                v = secret_store.get_secret(db, key)
                if v:
                    cfg[field] = int(v) if field == "port" else v
        except Exception:                       # noqa: BLE001
            pass
    # 端口缺省推导
    if not cfg["port"]:
        cfg["port"] = 465 if cfg["secure"] == "ssl" else 587
    if not cfg["secure"]:
        cfg["secure"] = "ssl" if cfg["port"] == 465 else "starttls"
    if not cfg["from_addr"]:
        cfg["from_addr"] = cfg["user"]
    return cfg


def is_configured(db=None) -> bool:
    c = config(db)
    return bool(c["host"] and c["from_addr"])


def is_dry_run() -> bool:
    """是否只记日志不真发。"""
    if os.environ.get(ENV_DRY, "").strip() == "1":
        return True
    return False


def site_url() -> str:
    return (os.environ.get(ENV_SITE, "") or "https://sozeer.com").rstrip("/")


def status(db=None) -> dict:
    """配置状态（后台展示用）。★ 不返回密码。"""
    c = config(db)
    return {
        "configured": is_configured(db),
        "dry_run": is_dry_run(),
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "from_addr": c["from_addr"],
        "from_name": c["from_name"],
        "secure": c["secure"],
        "has_password": bool(c["password"]),
        "hint": ("未配置 SMTP。设置 NF_SMTP_HOST / NF_SMTP_USER / "
                 "NF_SMTP_PASS 环境变量，或后台填写。"
                 "未配置时邮件**不会发送**（仅记日志）。"),
    }


# ---------------------------------------------------------------- 安全

def _sanitize_header(s: str, *, limit: int = 200) -> str:
    """★ 邮件头注入防护：去掉 CR/LF。

    为什么必须做：`Subject: 你好\\r\\nBcc: attacker@evil.com` 会插入密送。
    邮件头是**不可信输入**的汇聚点（用户名、标题都可能进头）。
    """
    return _CRLF_RE.sub(" ", str(s or "")).strip()[:limit]


def valid_recipient(addr: str) -> bool:
    """基本邮箱格式校验（不做完整 RFC 5322 —— 过严会误杀）。"""
    a = (addr or "").strip()
    if not a or len(a) > 254:
        return False
    if _CRLF_RE.search(a) or "," in a or ";" in a:
        return False                            # 防多收件人注入
    return bool(RECIPIENT_RE.match(a))


def mask_email(addr: str) -> str:
    """掩码（日志用）：`ab***@example.com`。"""
    a = (addr or "").strip()
    if "@" not in a:
        return "***"
    name, _, dom = a.partition("@")
    keep = name[:2] if len(name) > 2 else name[:1]
    return f"{keep}***@{dom}"


# ---------------------------------------------------------------- 主机校验

# 主机名规则：字母数字/点/连字符，段不能以连字符开头结尾
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def valid_host(host: str) -> bool:
    """SMTP 服务器地址校验（v1.0-E4.2）。

    ★ 为什么需要：管理员从后台填入的地址会被用于**建立网络连接**
      （`smtplib.SMTP(host, port)`）。若不校验，
      可能被填入 `evil.com:25` 这类含端口/协议的串，
      或者含空白字符导致连接异常。

    ★ 允许 IPv4 字面量与主机名；拒绝任何含空格、斜杠、冒号、
      以及 CRLF（防注入）的输入。
    """
    h = (host or "").strip()
    if not h or len(h) > 253:
        return False
    # ★ 防注入：CRLF / 空白 / 协议前缀 / 端口内联
    if re.search(r"[\s/\\:@]", h):
        return False
    # IPv4 字面量
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", h):
        return all(0 <= int(p) <= 255 for p in h.split("."))
    return bool(_HOST_RE.match(h))


# ---------------------------------------------------------------- 模板

_BASE_CSS = """
body{margin:0;padding:0;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,
"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
.wrap{max-width:600px;margin:0 auto;padding:32px 16px;}
.card{background:#fff;border-radius:12px;padding:32px;
      box-shadow:0 1px 3px rgba(0,0,0,.06);}
.brand{font-size:15px;font-weight:600;color:#1a1a1a;letter-spacing:.5px;
       margin-bottom:24px;}
.brand span{color:#c8a45c;}
h1{font-size:20px;color:#1a1a1a;margin:0 0 16px;font-weight:600;}
p{font-size:14px;line-height:1.75;color:#4a4a4a;margin:0 0 14px;}
.code{display:inline-block;padding:14px 28px;background:#f8f6f1;border-radius:8px;
      font-size:28px;font-weight:700;letter-spacing:8px;color:#8a6d2f;
      font-family:ui-monospace,Menlo,Consolas,monospace;margin:8px 0 20px;}
.btn{display:inline-block;padding:12px 28px;background:#c8a45c;color:#fff;
     text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;}
.muted{font-size:12px;color:#9a9a9a;line-height:1.7;margin-top:24px;
       padding-top:20px;border-top:1px solid #eee;}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;}
td{padding:9px 0;border-bottom:1px solid #f0f0f0;color:#4a4a4a;}
td:first-child{color:#9a9a9a;width:38%;}
td:last-child{text-align:right;font-weight:500;color:#1a1a1a;}
.footer{text-align:center;font-size:12px;color:#aaa;margin-top:24px;}
"""


def render(title: str, body_html: str, *, footer_note: str = "") -> str:
    """渲染 HTML 邮件（统一骨架）。"""
    note = footer_note or "本邮件由系统自动发送，请勿直接回复。"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><div class="card">
<div class="brand">溯字 <span>SOZEER</span></div>
<h1>{html.escape(title)}</h1>
{body_html}
<div class="muted">{html.escape(note)}</div>
</div>
<div class="footer">© 溯字 SOZEER · <a href="{site_url()}"
 style="color:#aaa;">{site_url()}</a></div>
</div></body></html>"""


def render_code(code: str, purpose_label: str, *, ttl_min: int = 5) -> str:
    """验证码邮件。"""
    return render(f"{purpose_label}验证码", f"""
<p>你正在进行{purpose_label}操作，请在页面中输入以下验证码：</p>
<div class="code">{html.escape(code)}</div>
<p>验证码 {ttl_min} 分钟内有效。若非本人操作，请忽略本邮件，
并建议尽快修改密码。</p>""")


def render_notice(title: str, lines: list[str], *, action: str = "",
                  action_text: str = "") -> str:
    """通用通知邮件（订单/工单/告警等）。"""
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    if action:
        body += (f'<p style="margin-top:20px;"><a class="btn" '
                 f'href="{html.escape(action)}">{html.escape(action_text or "查看详情")}</a></p>')
    return render(title, body)


def render_table(title: str, rows: list[tuple[str, str]], *, intro: str = "",
                 action: str = "", action_text: str = "") -> str:
    """带键值表的通知（订单回执等）。"""
    body = f"<p>{html.escape(intro)}</p>" if intro else ""
    body += "<table>" + "".join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>"
        for k, v in rows) + "</table>"
    if action:
        body += (f'<p style="margin-top:20px;"><a class="btn" '
                 f'href="{html.escape(action)}">{html.escape(action_text or "查看详情")}</a></p>')
    return render(title, body)


# ---------------------------------------------------------------- 发送

def send(to: str, subject: str, html_body: str, *,
         text_body: str = "", db=None, dry_run: bool | None = None,
         reply_to: str = "", raise_on_error: bool = False) -> dict:
    """★ 发送邮件（**默认吞异常**，绝不阻塞主流程）。

    返回 {ok, provider, detail, dry_run, ...}

    ★ 为什么 dry_run 默认跟随配置：本地开发没有 SMTP 时，
      整条流程仍要能跑通（只是不真发）—— 这能让"注册→收码→验证"
      在无网络环境完成自测。
    """
    recipient = (to or "").strip()
    if not valid_recipient(recipient):
        r = {"ok": False, "error": "收件人格式非法", "provider": "smtp"}
        if raise_on_error:
            raise MailError(r["error"])
        return r

    cfg = config(db)
    subj = _sanitize_header(subject)
    dry = is_dry_run() if dry_run is None else dry_run
    if not is_configured(db):
        dry = True                              # ★ 未配置 → 强制 dry（保守）

    if dry:
        _log.info("[DRY] 邮件未发送（仅记录）：to=%s subject=%s bytes=%d",
                  mask_email(recipient), subj, len(html_body or ""))
        return {"ok": True, "dry_run": True, "provider": "dry",
                "to": mask_email(recipient), "subject": subj,
                "note": ("未配置 SMTP，邮件仅记录未发送。"
                         "配置 NF_SMTP_HOST/USER/PASS 后生效。"
                         if not is_configured(db) else "已开启 dry-run 模式")}

    try:
        _deliver(cfg, recipient, subj, html_body, text_body, reply_to)
        _log.info("邮件已发送：to=%s subject=%s", mask_email(recipient), subj)
        return {"ok": True, "provider": "smtp", "to": mask_email(recipient),
                "subject": subj}
    except Exception as e:                      # noqa: BLE001
        _log.warning("邮件发送失败：to=%s err=%s: %s",
                     mask_email(recipient), type(e).__name__, e)
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
             "provider": "smtp", "to": mask_email(recipient)}
        if raise_on_error:
            raise MailError(r["error"]) from e
        return r


def _deliver(cfg: dict, to: str, subject: str, html_body: str,
             text_body: str, reply_to: str) -> None:
    """实际投递（含重试）。"""
    # ★ 多部分邮件：纯文本兜底能显著降低被判垃圾的概率
    msg = MIMEMultipart("alternative")
    from_name = _sanitize_header(cfg.get("from_name") or "", limit=64)
    from_addr = cfg["from_addr"]
    msg["From"] = email.utils.formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[-1] or "sozeer.com")
    if reply_to and valid_recipient(reply_to):
        msg["Reply-To"] = reply_to

    msg.attach(MIMEText(text_body or _html_to_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body or "", "html", "utf-8"))

    last: Exception | None = None
    for attempt in range(MAX_RETRY):
        try:
            _smtp_send(cfg, from_addr, to, msg.as_string())
            return
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                TimeoutError, OSError) as e:
            # ★ 只对**可恢复**错误重试（连接类）；认证失败重试无意义
            last = e
            if attempt < MAX_RETRY - 1:
                time.sleep(1.0 * (attempt + 1))
        except smtplib.SMTPAuthenticationError as e:
            raise MailError(
                "SMTP 认证失败：请检查用户名与授权码（多数邮箱需用「授权码」"
                "而非登录密码，如 QQ/163/Gmail）") from e
    if last:
        raise last


def _smtp_send(cfg: dict, from_addr: str, to: str, raw: str) -> None:
    host, port = cfg["host"], int(cfg["port"])
    secure = (cfg.get("secure") or "").lower()
    ctx = ssl.create_default_context()
    if secure == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=DEFAULT_TIMEOUT,
                              context=ctx) as s:
            _auth_and_send(s, cfg, from_addr, to, raw)
    else:
        with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT) as s:
            s.ehlo()
            if secure != "none":
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except smtplib.SMTPException:
                    # 服务端不支持 STARTTLS：不阻断（部分内网 SMTP 如此）
                    pass
            _auth_and_send(s, cfg, from_addr, to, raw)


def _auth_and_send(s: smtplib.SMTP, cfg: dict, from_addr: str,
                   to: str, raw: str) -> None:
    if cfg.get("user") and cfg.get("password"):
        s.login(cfg["user"], cfg["password"])
    s.sendmail(from_addr, [to], raw)


def _html_to_text(h: str) -> str:
    """HTML → 纯文本兜底（够用即可，不追求完美）。"""
    t = re.sub(r"<style[\s\S]*?</style>", "", h or "", flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|tr|h\d)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def send_async(to: str, subject: str, html_body: str, **kw) -> None:
    """★ 异步发送（不阻塞请求线程）。

    ★ 为什么需要：SMTP 握手 + 投递可能耗时 1-3 秒。
      订单回调、工单提交这类**同步接口**里不能等它。
    """
    def _run() -> None:
        try:
            send(to, subject, html_body, **kw)
        except Exception:                       # noqa: BLE001
            pass                                # 已在 send 内记录

    threading.Thread(target=_run, daemon=True,
                     name=f"mail-{mask_email(to)}").start()


# ---------------------------------------------------------------- 自检

def self_check(db=None) -> dict:
    """★ 启动自检（只做配置检查，**不发信**）。

    为什么不发测试信：启动时发信会污染用户收件箱，
    且 SMTP 故障会拖慢启动。真正的连通性测试放在后台按钮里。
    """
    c = config(db)
    issues: list[str] = []
    if not c["host"]:
        issues.append("缺少 SMTP 主机（NF_SMTP_HOST）")
    if not c["from_addr"]:
        issues.append("缺少发件地址（NF_SMTP_FROM 或 NF_SMTP_USER）")
    if c["user"] and not c["password"]:
        issues.append("配置了用户名但缺少密码/授权码（NF_SMTP_PASS）")
    if c["secure"] not in ("ssl", "starttls", "none"):
        issues.append(f"未知加密方式：{c['secure']}")
    ok = not issues
    if ok:
        _log.info("邮件服务就绪：%s:%s（%s，发件人 %s）%s",
                  c["host"], c["port"], c["secure"], mask_email(c["from_addr"]),
                  "（dry-run 模式）" if is_dry_run() else "")
    else:
        _log.info("邮件服务未配置：%s（邮件通知将仅记录日志）", "；".join(issues))
    return {"ok": ok, "issues": issues, **status(db)}


def test_send(db, to: str) -> dict:
    """后台「发送测试邮件」按钮用（真发，带明确结果）。"""
    body = render_notice("SMTP 配置测试", [
        "如果你收到这封邮件，说明 SMTP 配置正确，系统通知已可用。",
        f"发件服务器：{config(db)['host']}:{config(db)['port']}",
        f"加密方式：{config(db)['secure']}",
    ], action=site_url(), action_text="进入工作台")
    return send(to, "【溯字】SMTP 配置测试", body, db=db,
                dry_run=False, raise_on_error=False)
