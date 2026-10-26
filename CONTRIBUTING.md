# 贡献指南（Contributing）

感谢你有兴趣参与！在提 PR 之前，请花 5 分钟读完本文 ——
它能帮你省下大量返工时间。

---

## 〇、先确认：你的改动适合回馈上游吗

| 你想做的事 | 建议 |
|---|---|
| 修 Bug、加测试、改文档、优化性能 | ✅ 非常欢迎，直接提 PR |
| 加一个大功能（如新的改编格式） | ⚠️ **先开 Issue 讨论** —— 避免你写完我们说不合适 |
| 改架构（换框架 / 换数据库） | ⚠️ 先讨论，这类改动通常不会被接受 |
| 商业定制需求 | 💰 见 [COMMERCIAL.md](./COMMERCIAL.md) |

★ **判据**：**改动越大，越要先聊**。小修复直接提；大功能先对齐设计。

---

## 一、开发环境

### 环境要求

```
Python 3.11+          （生产用 3.14 venv，本地 3.11+ 即可）
Node.js 18+           （仅前端构建需要）
PostgreSQL 14+        （生产）；SQLite（本地测试，零配置）
Redis 6+              （可选；不装则自动降级为同步执行）
```

### 跑起来

```bash
git clone <repo-url> && cd novelforge

# 1. 依赖
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（复制模板，按需改）
cp .env.example .env
# ★ .env 已在 .gitignore 里，**绝不会**被提交

# 3. 启动
python -m uvicorn apps.api.main:app --reload --port 8000
# 打开 http://localhost:8000 → 注册首个账号（自动成为管理员）
```

### 前端（改了 `apps/web2/` 才需要）

```bash
cd apps/web2
npm install
npm run dev              # 开发服务器（热更新）
npm run build:only       # 只构建，产物输出到 apps/api/static/app2/
npx vue-tsc --noEmit     # ★ 类型检查（提 PR 前必须跑）
```

---

## 二、提 PR 前的检查清单

★ **这一节是本文最重要的部分**。以下每一项都对应一次真实的返工。

```bash
# ① 全量测试（必须全绿）
pytest tests/ --ignore=tests/test_import.py

# ② 前端类型检查（改了前端才需要）
cd apps/web2 && npx vue-tsc --noEmit

# ③ 如果你加了/删了 API 端点
#    → 必须同步更新路由数断言
#      文件：tests/test_v180_t1_split.py 的 EXPECTED_OPS
```

### 代码规范

| 项 | 要求 |
|---|---|
| **注释语言** | 中文 |
| **注释内容** | ★ 关键逻辑必须说明**"为什么"**，不只是"做了什么" |
| **类型标注** | 新增函数必须有类型标注（Python + TypeScript） |
| **测试** | 新功能必须有测试，**含边界与异常分支** |
| **不要硬编码** | 配置项走 `settings_registry`（见下） |

★ **关于注释的额外要求**：本项目鼓励在注释里写**踩坑记录**，例如：

```python
# ★★ 为什么必须用 ZPOPMIN 而不是 ZRANGE + ZREM：
#   后者有竞态 —— 两个 worker 可能取到同一个任务。
#   （v0.25.7 实测：并发 50 时重复执行率 12%）
```

这类注释比"取队列头部"有用一百倍。

---

## 三、项目核心约定（★ 不看会踩坑）

这些都是**花了代价换来的**，请务必遵守。

### ① 配置项必须登记到注册表

```python
# apps/api/settings_registry.py —— 这里是**唯一声明处**
SPECS["system.my_option"] = Spec(
    key="system.my_option", group="system", label="我的选项",
    type="bool", default=True, risk="low", desc="说明用途",
)
```

★ **为什么**：注册表驱动后台界面、校验、文档。
   不登记 = 后台看不到 = 运营改不了（等于没做）。

### ② 前端展示字段要从后端派生，不能硬编码

```typescript
// ❌ 错误：前端写死
const COMPARE = ['books', 'drama', 'concurrency']

// ✅ 正确：从接口拿
const open = site.register_open   // 后端声明 → 前端渲染
```

★ **为什么**：写死会与后端不同步 → 界面出现"—"这类"半成品感"。

### ③ 路由布局必须用 `<RouterView>`，不是 `<slot />`

```vue
<!-- ❌ 路由布局用 slot → 内容区永远空白，且控制台零报错 -->
<main><slot /></main>

<!-- ✅ -->
<main><RouterView /></main>
```

★ **区分**：路由布局（router `component`）用 `<RouterView>`；
   普通包裹组件用 `<slot>`。两者长得像，用错就是整页空白。

### ④ 破坏性操作必须"两阶段"

```typescript
// 阶段① 展示"会改什么"（用真实数据列出影响范围）
// 阶段② 用户二次确认（手打名称，不是再点一次）
```

★ **为什么**：连续两次点击是同一个肌肉记忆动作，防不住误操作。

### ⑤ 旁路功能必须 fail-open

```python
try:
    send_notification(...)
except Exception:
    log.warning(...)   # ★ 至少要有日志！否则故障静默
```

★ **为什么**：通知/埋点失败绝不能影响核心业务。
   但**宽泛 except 里必须有日志** —— 没有日志的 except 是最难查的坑。

### ⑥ 时间统一用 naive UTC

```python
# ★ 项目全站约定：DB 存 naive UTC
#   混用 aware/naive 会抛 TypeError，且本地（UTC 时区）不会暴露
```

---

## 四、提交信息规范

采用 Conventional Commits：

```
<type>(<scope>): <简短描述>

<可选：详细说明，重点写"为什么">

<可选：关联 Issue>
```

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 仅文档 |
| `test` | 仅测试 |
| `refactor` | 重构（不改行为） |
| `perf` | 性能优化 |
| `chore` | 构建/依赖/杂项 |

**示例**：

```
fix(queue): 出队用 ZPOPMIN 替代 ZRANGE+ZREM

ZRANGE + ZREM 有竞态：两个 worker 可能取到同一任务。
实测并发 50 时重复执行率 12%。

Closes #42
```

---

## 五、安全相关改动

★ **涉及以下内容时，请额外注意**：

- 认证 / 授权 / 会话
- 密码 / 密钥 / 加密
- 用户输入的处理（尤其是拼进 SQL / 命令 / 模板的）
- 文件上传 / 下载 / 路径拼接

**要求**：

1. **必须**有对应的安全测试（含"攻击者视角"的负向用例）
2. **绝不**在测试里写真实凭据（用假数据）
3. 如发现**已有**漏洞，请走 [SECURITY.md](./SECURITY.md) 的私密渠道，
   **不要**在 PR 里公开

---

## 六、许可证与贡献授权

本项目采用 **[BSL 1.1](./LICENSE)**（源码开放，非 OSI 开源）。

★ **提交 PR 即表示你同意**：

1. 你的贡献以相同的 BSL 1.1 许可提供
2. 你有权提交这些代码（原创，或已获得原作者许可）
3. 项目所有者可将你的贡献用于商业授权（BSL 允许）

★ **如果你的贡献包含第三方代码**，请在 PR 描述里注明来源与许可证
   —— 许可证不兼容的代码无法合并。

---

## 七、评审流程

```
你提 PR
  ↓
CI 自动跑（测试 + 类型检查 + 凭据扫描）
  ↓
维护者评审（通常 3~7 天）
  ↓
可能要求修改 → 你在同一分支追加提交即可
  ↓
合并 🎉
```

★ **如果 7 天没回应**，欢迎在 PR 里 ping 一次 —— 不要重复开新 PR。

### 什么样的 PR 会被快速合并

- ✅ 小而专注（一个 PR 只做一件事）
- ✅ 带测试
- ✅ 描述清楚"为什么这么改"
- ✅ 遵循本文的约定

### 什么样的 PR 会被要求返工

- ❌ 一个 PR 混了多个不相关的改动
- ❌ 没有测试
- ❌ 硬编码了配置 / 展示字段
- ❌ 大改动没有预先讨论

---

## 八、文档贡献

文档同样是重要贡献，而且**门槛最低**：

- 修正错别字 / 表述不清
- 补充"踩坑记录"（你遇到的问题很可能别人也会遇到）
- 改进示例代码
- 翻译

★ **不需要**为文档改动跑全量测试，但请确保 Markdown 渲染正常。

---

## 九、有问题？

- 💬 一般问题 → 开 Discussion 或 Issue
- 🐛 Bug → 用 Bug Issue 模板
- ✨ 功能建议 → 用 Feature Request 模板
- 🔒 安全漏洞 → **私密报告**（见 [SECURITY.md](./SECURITY.md)）

感谢你的贡献！
