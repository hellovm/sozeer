"""NovelForge MCP Server（#14 · v1.4.2）。

把**确定性、只读、无副作用**的核心能力暴露为 MCP 工具，
供外部 agent（Claude / WorkBuddy / 其他 MCP 兼容客户端）调用。

传输模式：
  · stdio（默认）：本地 agent 起子进程
      python -m apps.api.mcp_server
  · streamable-http：远程部署
      python -m apps.api.mcp_server --http

★ 设计原则（MCP 教科书 + 本项目铁律）：
  · 确定性：同样输入 → 同样输出（无随机性、无时间戳、无模型调用）
  · 只读：不写 DB、不触网（不调 LLM）、不落盘
  · 快速：< 500ms（纯规则引擎 + 配置查询）
  · 写操作（代笔/保存人物/采纳）**绝不**经 MCP 暴露（安全红线）
  · mcp 是可选依赖：没装时 main.py /api/mcp/info 给安装提示而非 500（fail-open）

★ 测试友好（血泪："写了这行≠能跑"）：
  4 个工具核心逻辑抽成**模块级纯函数**（run_diagnose / run_gates_tool /
  run_list_genres / run_style_hints），pytest 直接 import 验证，不依赖
  mcp 客户端 API；@tool 只是把纯函数注册进 MCP server。
"""
from __future__ import annotations

import json
import types

# ---------------------------------------------------------------- MCP 导入
# ★ 延迟导入 + 守卫：mcp 是可选依赖，没装时本模块仍可被 main.py import
#   （_MCP_AVAILABLE=False），/api/mcp/info 端点返回安装提示而非 500。
try:
    from mcp.server.mcpserver import MCPServer
    _MCP_AVAILABLE = True
except ImportError:
    MCPServer = None  # type: ignore[assignment]
    _MCP_AVAILABLE = False


# ============================================================ 工具核心（纯函数）

def run_diagnose(text: str) -> str:
    """对章节文本跑全部质量技能检测（AI 味 / Show-Don't-Tell / 对话 / 节奏 / 章末钩子）。

    纯规则引擎，不调模型，< 100ms。
    返回 JSON：{"findings": [...], "stats": {...}, "count": N}。
    """
    from apps.api import quality
    d = quality.diagnose(text, None, [])  # book=None, bible_chars=[] → 跳过连续性
    return json.dumps(d.to_dict(), ensure_ascii=False, default=str)


def run_gates_tool(text: str, platform: str = "") -> str:
    """对章节文本跑质量闸门（禁用词 / 段落 / 无源梗 / 境界 / 平台适配 等 7 维）。

    纯规则引擎。platform 可选（"番茄小说" / "起点" 等，不传 = 通用）。
    ★ 字数不卡（MCP 调用方未必设过单章字数，0=跳过字数闸门，与"立项前不卡字数"口径一致）。
    返回 JSON：{"score": 0-100, "issues": [...], "stats": {...}}。
    """
    from apps.api.orchestrator import gates
    book = types.SimpleNamespace(
        platform=platform, id="mcp_tool",
        chapter_min=0, chapter_max=0,  # 0 = 不卡字数（check_length 直接读这俩属性）
    )
    rep = gates.run_gates(text, book, None)
    issues = [
        {"rule": getattr(i, "rule", ""),
         "level": getattr(i, "level", ""),
         "message": getattr(i, "message", ""),
         "span": getattr(i, "span", "")}
        for i in rep.issues
    ]
    return json.dumps(
        {"score": rep.score, "issues": issues, "stats": rep.stats},
        ensure_ascii=False, default=str,
    )


def run_list_genres() -> str:
    """列出全部可用题材包（xianxia / urban / urban_suspense / scifi / game 等）。

    返回 JSON 数组，每项含 key / name / description / metrics。
    """
    from apps.api import genres
    return json.dumps(genres.catalog(), ensure_ascii=False, default=str)


def run_style_hints(theme: str) -> str:
    """获取指定题材的风格提示词（可注入代笔 prompt 的 style_dna 段）。

    theme：题材名（如 "xianxia" / "修仙"、"urban" / "都市"）。
    返回 JSON 数组（风格提示词列表）。
    """
    from apps.api import genres
    return json.dumps(genres.style_hints_for(theme), ensure_ascii=False)


# ============================================================ MCP 工具注册

def _server() -> "MCPServer":
    """构建并注册全部 MCP 工具的 MCPServer。

    ★ 延迟构建（不在模块顶层）：
      ① 避免没装 mcp 时模块 import 就炸；
      ② 避免在 main.py 启动时注册 MCP 工具（MCP 是独立进程/连接，
         主 FastAPI 服务不需要它们）。
    """
    if not _MCP_AVAILABLE:
        raise ImportError(
            "mcp 未安装。pip install 'mcp[cli]>=2.0' 后重试。"
            "（MCP 是可选依赖，不影响主服务运行。）"
        )

    server = MCPServer("novelforge")

    @server.tool()
    def diagnose_chapter(text: str) -> str:
        """对章节文本跑全部质量技能检测（AI 味 / Show-Don't-Tell / 对话 / 节奏 / 章末钩子）。

        纯规则引擎，不调模型，< 100ms。
        返回 JSON：{"findings": [...], "stats": {...}, "count": N}。
        """
        return run_diagnose(text)

    @server.tool()
    def check_quality_gates(text: str, platform: str = "") -> str:
        """对章节文本跑质量闸门（禁用词 / 段落 / 无源梗 / 境界 / 平台适配 等 7 维）。

        纯规则引擎。platform 可选（"番茄小说" / "起点" 等，不传 = 通用）。
        ★ 字数不卡（MCP 调用方未必设过单章字数，0=跳过字数闸门）。
        返回 JSON：{"score": 0-100, "issues": [...], "stats": {...}}。
        """
        return run_gates_tool(text, platform)

    @server.tool()
    def list_genres() -> str:
        """列出全部可用题材包（xianxia / urban / urban_suspense / scifi / game 等）。

        返回 JSON 数组，每项含 key / name / description / metrics。
        """
        return run_list_genres()

    @server.tool()
    def get_genre_style_hints(theme: str) -> str:
        """获取指定题材的风格提示词（可注入代笔 prompt 的 style_dna 段）。

        theme：题材名（如 "xianxia" / "修仙"、"urban" / "都市"）。
        返回 JSON 数组（风格提示词列表）。
        """
        return run_style_hints(theme)

    return server


def list_tool_names() -> list[str]:
    """MCP 工具清单（main.py /api/mcp/info 用，不依赖 mcp 是否安装）。"""
    return [
        "diagnose_chapter",
        "check_quality_gates",
        "list_genres",
        "get_genre_style_hints",
    ]


def main() -> None:
    """CLI 入口：python -m apps.api.mcp_server [--http]"""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="NovelForge MCP Server")
    ap.add_argument("--http", action="store_true",
                   help="Streamable HTTP 模式（默认 stdio）")
    args = ap.parse_args()

    server = _server()
    transport = "streamable-http" if args.http else "stdio"
    print(f"NovelForge MCP Server · transport={transport} · "
          f"tools={len(list_tool_names())}", file=sys.stderr, flush=True)
    server.run(transport)


if __name__ == "__main__":
    main()
