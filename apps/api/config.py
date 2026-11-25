"""全局配置。P0 用 SQLite，切 Postgres 只改 NOVELFORGE_DB_URL。"""
import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("NOVELFORGE_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = Path(os.environ.get("NOVELFORGE_DATA", BASE_DIR / "data"))
BOOKS_DIR = DATA_DIR / "books"
DB_PATH = DATA_DIR / "novelforge.db"

DB_URL = os.environ.get("NOVELFORGE_DB_URL", f"sqlite:///{DB_PATH}")

# 预留多租户：当前只有 default 一个 workspace
DEFAULT_WORKSPACE = os.environ.get("NOVELFORGE_WORKSPACE", "default")

HOST = os.environ.get("NOVELFORGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOVELFORGE_PORT", "8000"))

# 应用签名密钥：用于签发登录令牌。生产务必通过环境变量覆盖。
SECRET_KEY = os.environ.get("NOVELFORGE_SECRET", "dev-secret-change-me-in-prod")
# 令牌有效期（秒），默认 14 天
TOKEN_TTL = int(os.environ.get("NOVELFORGE_TOKEN_TTL", str(14 * 24 * 3600)))
# 是否允许开放注册（关闭后仅管理员可创建账号）
ALLOW_REGISTER = os.environ.get("NOVELFORGE_ALLOW_REGISTER", "1") == "1"
# 全局 CORS 允许来源（逗号分隔）；留空则仅同源
CORS_ORIGINS = [o.strip() for o in os.environ.get("NOVELFORGE_CORS", "").split(",") if o.strip()]
# 是否开放 /docs、/redoc、/openapi.json。默认开着方便本地调试；
# 生产建议设 NOVELFORGE_DOCS=0 —— 这三个路径不带 /api 前缀，鉴权中间件根本不经过，
# 一旦开放等于免令牌交出全部路由、参数与模型结构。
DOCS_ENABLED = os.environ.get("NOVELFORGE_DOCS", "1") == "1"

# ----------------------------------------------------------------- 对话历史窗口（#62）
# 长会话下 agent 对话历史的 token 预算（估算口径，见 history_window.estimate_tokens）。
# 从「最新」往旧累加，超过预算就截掉更早的消息，防 token 膨胀 + 历史理解衰减。
# 0 = 关闭窗口（保持全量透传的旧行为）。默认 4000（约 8-10 轮中文对话）。
CHAT_HISTORY_TOKENS = int(os.environ.get("NOVELFORGE_CHAT_HISTORY_TOKENS", "4000"))


# ----------------------------------------------------------------- 应用版本
# v0.18 T1：版本号集中在此，main.py 的 FastAPI(version=...) 与
# routers/system.py 的 /api/health 都引用它，避免两处各写一份而漂移。
APP_VERSION = "1.3.3"


def ensure_dirs() -> None:
    for p in (DATA_DIR, BOOKS_DIR):
        p.mkdir(parents=True, exist_ok=True)
