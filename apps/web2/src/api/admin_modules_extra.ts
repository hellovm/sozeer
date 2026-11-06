/**
 * admin_modules_extra.ts · 后台业务 API（第二批 · v0.26.3 G0-6 续）
 *
 * ★ 覆盖剩余 10 页：health / audit / risk / queue / content / ads / models /
 *   config / instances / rbac
 *
 * ★★ 类型策略（沿用 MEMORY §97「列表返回风格没有规律」的教训）：
 *   · 有明确契约的（读过后端源码）→ 写精确类型
 *   · 后端返回结构自由/易变的（如 config 值、queue stats）→ 用 `Record<string, unknown>`
 *     并在页面做**通用渲染**，避免字段一改就白屏
 */
import { adminApi, adminDownload } from '@/api/admin'

/* ================================================================ 系统状态 */

export interface HealthDisk {
  total_gb?: number
  used_gb?: number
  free_gb?: number
  used_pct?: number
}
export interface HealthMemory {
  total_mb?: number
  available_mb?: number
  used_pct?: number
}
export interface HealthResult {
  version: string
  time: string
  db: { ok: boolean; url_scheme: string; latency: string }
  disk: HealthDisk
  memory: HealthMemory
  load: { '1m': number; '5m': number; '15m': number }
}

export const healthApi = {
  get: () => adminApi.get<HealthResult>('/api/admin/health'),
}

/* ================================================================ 审计日志 */

export interface AuditRow {
  id: string
  operator_id: string
  operator_name: string
  action: string
  target_type: string
  target_id: string
  detail: string
  reason: string
  ip: string
  result: string
  created_at: string
}

export interface AuditQuery {
  action?: string
  operator_id?: string
  target_id?: string
  limit?: number
  offset?: number
}

export const auditApi = {
  list: (q: AuditQuery = {}) =>
    adminApi.get<{ total: number; items: AuditRow[] }>('/api/admin/audit', {
      action: q.action, operator_id: q.operator_id, target_id: q.target_id,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),
}

/* ================================================================ 风控中心 */

export interface RiskOverview {
  window_hours: number
  alerts_total: number
  by_level: Record<string, number>
  by_rule: Record<string, number>
  logins: { success: number; failed: number; fail_rate: number }
}

export interface RiskAlert {
  id?: string
  rule?: string
  level?: string
  kind?: string
  detail?: string
  ip?: string
  user_id?: string
  created_at?: string
}

export interface RatelimitStatus {
  backend?: string
  [k: string]: unknown
}

export const riskApi = {
  overview: (hours = 24) =>
    adminApi.get<RiskOverview>('/api/admin/risk/overview', { hours }),

  alerts: (hours = 24, limit = 100) =>
    adminApi.get<{ items: RiskAlert[]; total?: number }>('/api/admin/risk/alerts',
      { hours, limit }),

  ratelimitStatus: () =>
    adminApi.get<RatelimitStatus>('/api/admin/ratelimit/status'),

  setRatelimitBackend: (backend: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/ratelimit/backend', { backend }),
}

/* ================================================================ 任务队列 */

/** ★ 队列 stats 结构易变（随 J 组演进）→ 用宽松类型 + 通用渲染 */
export interface QueueStats {
  lanes?: Record<string, number>
  queued?: number
  running?: number
  done?: number
  failed?: number
  dlq?: number
  [k: string]: unknown
}

export interface QueueDashboard extends QueueStats {
  peek?: Array<Record<string, unknown>>
  limits?: Record<string, number>
  dlq_items?: Array<Record<string, unknown>>
}

export interface QueueStatus {
  backend?: string
  redis?: Record<string, unknown>
  stats?: QueueStats
  [k: string]: unknown
}

export interface QueueAlertRow {
  kind?: string
  level?: string
  message?: string
  count?: number
  last_at?: string
  [k: string]: unknown
}

export const queueApi = {
  dashboard: (limit = 30) =>
    adminApi.get<QueueDashboard>('/api/admin/queue/dashboard', { limit }),

  status: () => adminApi.get<QueueStatus>('/api/admin/queue/status'),

  /** 队列分道（哪条道堵了） */
  lanes: () => adminApi.get<Record<string, unknown>>('/api/admin/queue/lanes'),

  /** 并发闸门当前占用 */
  concurrency: () =>
    adminApi.get<Record<string, unknown>>('/api/admin/queue/concurrency'),

  /** 同步准入状态（同步请求占位情况） */
  admission: () =>
    adminApi.get<Record<string, unknown>>('/api/admin/queue/admission'),

  /** ★ 告警（必须节流展示 —— 刷屏会让人关掉通知） */
  alerts: () =>
    adminApi.get<{ items: QueueAlertRow[]; total?: number }>('/api/admin/queue/alerts'),

  dlq: (limit = 50) =>
    adminApi.get<{ items: Array<Record<string, unknown>>; total?: number }>(
      '/api/admin/queue/dlq', { limit }),

  peek: (limit = 20) =>
    adminApi.get<{ items: Array<Record<string, unknown>> }>(
      '/api/admin/queue/peek', { limit }),

  /* ── 破坏性维护操作（全部需要原因） ── */
  /** ★ 重排僵尸任务（释放被幽灵任务占满的配额） */
  reap: (reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/queue/reap', { reason }),
  /** ★ 从 Redis 重建队列（Redis 数据丢失后的恢复手段） */
  rebuild: (reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/queue/rebuild', { reason }),
  /** ★ 清空队列（危险：会丢任务） */
  reset: (reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/queue/reset', { reason }),
  /** 清空死信队列 */
  clearDlq: (reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/queue/clear-dlq', { reason }),
  /** 重置告警计数 */
  resetAlerts: () =>
    adminApi.post<Record<string, unknown>>('/api/admin/queue/alerts/reset', {}),
}

/* ================================================================ 内容治理 */

export interface AdminBookRow {
  id: string
  title: string
  author?: string
  owner_id: string
  owner_name?: string
  content_status: string
  chapters?: number
  words?: number
  created_at: string
  [k: string]: unknown
}

export interface ContentFlagRow {
  id: string
  book_id: string
  category: string
  severity: string
  note: string
  status: string
  source: string
  handled_by: string
  handled_at: string
  created_at: string
}

export const contentApi = {
  books: (q: { keyword?: string; content_status?: string; limit?: number; offset?: number } = {}) =>
    adminApi.get<{ total: number; items: AdminBookRow[] }>('/api/admin/books', {
      keyword: q.keyword, content_status: q.content_status,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  /**
   * ★★ 内容标记列表：端点是 **`/api/admin/flags`**（不是 `/api/admin/moderation/flags`！）
   *
   *   ★ 线上验证时发现我按"语义推测"写成了 `moderation/flags` → 404 → 页面空白。
   *     实际存在的是：
   *       · `GET  /api/admin/flags`           ← 本方法
   *       · `POST /api/admin/books/flag`      ← 人工标记（见下）
   *       · `GET  /api/admin/moderation/stats`
   *       · `POST /api/admin/moderation/test`
   *   ★ 教训：**路径不能按语义推测**，必须对照 openapi 逐条核对。
   */
  flags: (q: { status?: string; book_id?: string; limit?: number; offset?: number } = {}) =>
    adminApi.get<{ total?: number; items: ContentFlagRow[] }>('/api/admin/flags', {
      status: q.status, book_id: q.book_id,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  moderationStats: () =>
    adminApi.get<Record<string, unknown>>('/api/admin/moderation/stats'),

  /** ★ 处置内容（隐藏/恢复）—— 必须填原因 */
  setContentStatus: (bookId: string, status: string, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/books/content-status',
      { book_id: bookId, content_status: status, reason }),

  /** ★ 人工标记（自动判定会误判，必须留人工口子） */
  flag: (bookId: string, category: string, note: string, severity = 'medium') =>
    adminApi.post<Record<string, unknown>>('/api/admin/books/flag',
      { book_id: bookId, category, note, severity }),

  /**
   * ★★ v1.0-E4.13 处置内容标记（审核队列核心动作）。
   *   状态机：open → hidden（隐藏书）| cleared（澄清+恢复）| banned（隐藏+封禁作者）| open（重开）。
   *   hidden/banned 有联动副作用 —— 前端必须在确认弹窗里写明。
   */
  resolveFlag: (flagId: string, status: string, reason: string, note = '') =>
    adminApi.patch<{ ok: boolean; flag_id: string; status: string; unchanged?: boolean }>(
      `/api/admin/flags/${encodeURIComponent(flagId)}`,
      { status, reason, note }),
}

/* ================================================================ 公告广播 */

export interface BroadcastResult {
  ok: boolean
  /** 服务端回传统计（成功/跳过数等，字段宽松处理） */
  sent?: number
  skipped?: number
  total?: number
  note?: string
}

export const notifyApi = {
  /**
   * ★★ 系统公告（批量站内信）。user_ids 留空 = 全体活跃用户。
   *   ★ 邮件通道默认关闭（防垃圾邮件/SMTP 配额），服务端固定只发站内信。
   */
  broadcast: (title: string, body: string, link = '', userIds: string[] = []) =>
    adminApi.post<BroadcastResult>('/api/admin/notify/broadcast',
      { title, body, link, user_ids: userIds }),
}


/* ================================================================ 模型配置 */

export interface ModelProviderRow {
  id?: string
  name: string
  base_url: string
  /** ★ 后端从不返回明文 key，只返回是否已设置 */
  api_key_set?: boolean
  api_key?: string
  weight: number
  enabled?: number
  fail_count?: number
  open_until?: string | null
  last_ok_at?: string | null
  last_err?: string
}

export interface ModelProfileRow {
  id: string
  name: string
  role: string
  provider: string
  base_url: string
  model_id: string
  /** ★ 是否已配置 key（不回传明文） */
  api_key_set?: boolean
  /** ★ 多供应商端点（负载分流 / 故障转移） */
  providers?: ModelProviderRow[]
  enabled?: number
  note?: string
  [k: string]: unknown
}

export interface ComfyTemplate {
  key: string
  name: string
  kind?: string
  desc?: string
  placeholders?: string[]
}

export interface ModelRoutingRow {
  plan_key: string
  role: string
  profile_ids: string[]
}

export const modelsApi = {
  overview: () =>
    adminApi.get<{
      items: ModelProfileRow[]
      roles: Array<{ key: string; label: string; desc?: string }>
      provider_kinds: Array<{ key: string; label: string; desc?: string }>
      plans?: Array<{ plan_key: string; name?: string }>
      routes?: ModelRoutingRow[]
      [k: string]: unknown
    }>('/api/admin/models2'),

  /** 新增/更新模型槽（含多个供应商端点） */
  upsertProfile: (payload: Record<string, unknown>) =>
    adminApi.post<{ ok: boolean; id: string }>('/api/admin/models2/profile', payload),

  removeProfile: (id: string) =>
    adminApi.del<Record<string, unknown>>(
      `/api/admin/models2/profile/${encodeURIComponent(id)}`),

  /** ★ 套餐 → 模型路由（不同套餐用不同模型） */
  setRoute: (planKey: string, role: string, profileIds: string[]) =>
    adminApi.post<Record<string, unknown>>('/api/admin/models2/route',
      { plan_key: planKey, role, profile_ids: profileIds }),

  /** 内置 ComfyUI 工作流模板清单 */
  templates: (kind = '') =>
    adminApi.get<{ items: ComfyTemplate[] }>('/api/admin/models2/templates', { kind }),

  template: (key: string) =>
    adminApi.get<{ key: string; workflow: Record<string, unknown> }>(
      `/api/admin/models2/template/${encodeURIComponent(key)}`),

  /** ★ 测试 ComfyUI 连接（返回可执行建议，不只报错误码） */
  testComfyui: (baseUrl: string, token = '') =>
    adminApi.post<{
      ok: boolean
      message?: string
      advice?: string
      checks?: Array<{ ok: boolean; name: string; detail: string }>
      system?: Record<string, unknown>
      [k: string]: unknown
    }>('/api/admin/models2/test-comfyui', { base_url: baseUrl, token }),

  /** ★ 通用模型连通性测试（按 provider 分支：comfyui 走 /system_stats，
   *    其余 OpenAI 兼容类走 /models 探针；靠 profile_id 还原真实密钥） */
  testModel: (payload: {
    provider: string
    baseUrl: string
    profileId?: string
    modelId?: string
  }) =>
    adminApi.post<{
      ok: boolean
      checks?: Array<{ ok: boolean; name: string; detail: string }>
      hint?: string
      message?: string
      [k: string]: unknown
    }>('/api/admin/models2/test-model', {
      provider: payload.provider,
      base_url: payload.baseUrl,
      profile_id: payload.profileId || '',
      model_id: payload.modelId || '',
    }),

  plans: () =>
    adminApi.get<{ items: Array<{ plan_key: string; name?: string; [k: string]: unknown }> }>(
      '/api/admin/plans'),
}

/* ================================================================ 配置中心 */

export interface ConfigItem {
  key: string
  value: unknown
  default?: unknown
  type?: string
  group?: string
  risk?: string
  label?: string
  desc?: string
  source?: string
  updated_at?: string
  /**
   * ★★ v1.0-E4.15：类型化渲染的数据源。
   *
   * 后端 list_all 已下发这些字段（对照 config_center.py list_all）：
   *   · choices  → type=choice 时的合法值列表（渲染 <select>）
   *   · min/max  → 数值项的范围（渲染 number 输入 + 范围提示）
   *   · unit     → 数值单位（显示用）
   *   · readonly → 系统维护项（只读展示）
   */
  choices?: string[]
  min?: number | null
  max?: number | null
  unit?: string
  readonly?: boolean
  /**
   * ★★ 高级项（v1.0-E4.4）：界面**默认折叠**。
   *
   * 由后端注册表声明（`Spec.advanced`）—— 因为"哪些算高级"是
   * 配置本身的属性，不是界面的属性。新增配置时声明一次即可。
   *
   * ★ 例：`model.role_writer`（档位名）需要先懂"档位"概念 → advanced
   *       `model.temperature_writer`（正文温度）运营能理解 → 不折叠
   */
  advanced?: boolean
  [k: string]: unknown
}

export interface ConfigMeta {
  groups: Array<{ group: string; name: string; icon: string; desc: string }>
  risk_label: Record<string, string>
  total: number
  cache: Record<string, unknown>
  env_prefix: string
  /** ★ 优先级说明：DB（界面）> 环境变量 > 注册表默认值 */
  priority: string[]
}

export interface ConfigHistoryRow {
  id?: string
  key: string
  old_value?: string
  new_value?: string
  operator_name?: string
  reason?: string
  created_at?: string
  [k: string]: unknown
}

export const configApi = {
  /**
   * ★★ 配置项列表：后端返回 **`{groups: [{group, name, icon, items, count}], total, all_total, cache}`**
   *   —— 注意 items 是**嵌在 groups 里**的，不是顶层数组！（对照 config_center.list_all）
   *
   *   ★ 我在第一版按 `{items}` 实现 → 页面空白。这是本项目第 N 次踩同一个坑：
   *     列表返回风格**必须逐个对照后端源码**，不能靠"项目惯例"推测（MEMORY §97）。
   *
   *   → 这里在 API 层**拍平成 items**，让页面只处理扁平数组。
   *     同时保留 groups 供分组展示使用。
   */
  list: async (group = '') => {
    const d = await adminApi.get<{
      groups: Array<{
        group: string
        name: string
        icon: string
        desc: string
        items: ConfigItem[]
        count: number
      }>
      total: number
      all_total: number
      cache: Record<string, unknown>
    }>('/api/admin/config', { group })
    const items: ConfigItem[] = (d.groups || []).flatMap((g) => g.items || [])
    return { items, groups: d.groups || [], total: d.total ?? items.length }
  },

  meta: () => adminApi.get<ConfigMeta>('/api/admin/config/meta'),

  /** ★ 保存前先 diff（让管理员看清"会改什么"）
   *
   * ★★ 后端契约：请求体字段是 **`values`**（`SetIn.values`），响应字段是
   *   **`changes`**（`diff_preview` 返回 `{"changes": [...]}`）—— 不是 items。
   *   历史 bug：此前请求发 `{changes}` + 响应读 `r.items`，双向错位 →
   *   后端拿到空 values → 恒 0 项变更（用户截图实锤）。
   */
  diff: (changes: Record<string, unknown>) =>
    adminApi.post<{
      changes: Array<{
        key: string; old: unknown; new: unknown;
        risk?: string; changed: boolean; label?: string
      }>
      changed_count?: number
      [k: string]: unknown
    }>('/api/admin/config/diff', { values: changes }),

  save: (changes: Record<string, unknown>, reason: string) =>
    adminApi.post<{ ok: boolean; applied?: number; [k: string]: unknown }>(
      '/api/admin/config', { values: changes, reason }),

  setOne: (key: string, value: unknown, reason: string) =>
    adminApi.post<Record<string, unknown>>(
      `/api/admin/config/one?key=${encodeURIComponent(key)}`, { value, reason }),

  history: (key = '', limit = 100) =>
    adminApi.get<{ items: ConfigHistoryRow[]; [k: string]: unknown }>(
      '/api/admin/config/history', { key, limit }),

  rollback: (historyId: string, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/config/rollback',
      { history_id: historyId, reason }),

  clearCache: () => adminApi.post<Record<string, unknown>>('/api/admin/config/cache/clear', {}),

  doc: () => adminApi.get<Record<string, unknown>>('/api/admin/config/doc'),

  exportCfg: () =>
    adminDownload('/api/admin/config/export', `config_${today()}.json`),

  importCfg: (payload: Record<string, unknown>, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/config/import',
      { ...payload, reason }),

  /* ────────────── ★ 邮件配置（v1.0-E4.2）──────────────
   *
   * ★ 为什么不复用通用配置端点：
   *   SMTP 配置存的是**凭据**（密码），
   *   ① 不能明文回显（通用配置表会直接显示 value）
   *   ② 需要组合校验（host/port/secure 相互约束）
   *   ③ 需要"测试发信"验证连通性
   *   → 走独立的 `/api/admin/mail/*`（后端加密存储）。
   */
  mailStatus: () =>
    adminApi.get<{
      configured: boolean; dry_run: boolean; host: string; port: number
      user: string; from_addr: string; from_name: string; secure: string
      has_password: boolean; hint: string
    }>('/api/admin/mail/status'),

  /** ★ 保存 SMTP —— `password` 留空表示"不修改"（避免误清空） */
  mailSave: (cfg: {
    host: string; port: number; user: string; password: string
    from_addr: string; from_name: string; secure: string
  }) => adminApi.post<{ ok: boolean; saved: string[]; status: unknown }>(
    '/api/admin/mail/config', cfg),

  mailTest: (to: string) =>
    adminApi.post<{ ok: boolean; [k: string]: unknown }>(
      '/api/admin/mail/test', { to }),
}

/* ================================================================ 开源实例 */

export interface InstanceRow {
  id: string
  name?: string
  online?: boolean
  version?: string
  last_seen_at?: string
  created_at?: string
  suspect?: boolean
  [k: string]: unknown
}

export interface InstanceOverview {
  total?: number
  online?: number
  suspect?: number
  [k: string]: unknown
}

export const instancesApi = {
  overview: () => adminApi.get<InstanceOverview>('/api/admin/instances/overview'),

  list: (q: { online_only?: boolean; suspect_only?: boolean; limit?: number; offset?: number } = {}) =>
    adminApi.get<{ total?: number; items: InstanceRow[] }>('/api/admin/instances', {
      online_only: q.online_only, suspect_only: q.suspect_only,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  insights: () =>
    adminApi.get<Record<string, unknown>>('/api/admin/instances/insights'),

  rules: () => adminApi.get<Record<string, unknown>>('/api/admin/instances/rules'),

  recompute: () =>
    adminApi.post<Record<string, unknown>>('/api/admin/instances/recompute', {}),

  /** ★ 人工复核（自动判定会误判 —— 人工结果不应被重算覆盖） */
  review: (instanceId: string, payload: Record<string, unknown>) =>
    adminApi.post<Record<string, unknown>>(
      `/api/admin/instances/${encodeURIComponent(instanceId)}/review`, payload),
}

/* ================================================================ 权限管理 */

export interface RbacRoleInfo {
  label: string
  desc?: string
  patterns: string[]
  permissions: string[]
  /** ★ db = 表配置覆盖；builtin = 代码内置兜底 */
  source?: 'db' | 'builtin'
}

export interface RbacMatrix {
  roles: Record<string, RbacRoleInfo>
  groups?: Record<string, string[]>
  permissions?: Record<string, string>
  [k: string]: unknown
}

export interface RbacMemberRow {
  id: string
  username: string
  nickname?: string
  role: string
  role_label?: string
  is_active?: number
  [k: string]: unknown
}

export const rbacApi = {
  /** ★ 角色 × 权限矩阵（读 DB 实际生效值，非代码定义） */
  matrix: () => adminApi.get<RbacMatrix>('/api/admin/rbac/matrix'),

  members: (q: { keyword?: string; role?: string; limit?: number; offset?: number } = {}) =>
    adminApi.get<{
      total: number
      items: RbacMemberRow[]
      roles: Array<{ key: string; label: string; desc?: string }>
    }>('/api/admin/rbac/members', {
      keyword: q.keyword, role: q.role,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  /** ★ 改成员角色（防同级提权由后端把关） */
  setMemberRole: (userId: string, role: string, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/rbac/members/role',
      { user_id: userId, role, reason }),

  /** ★ 改角色权限（高危：会立即影响所有该角色成员） */
  setRolePermissions: (role: string, patterns: string[], reason: string) =>
    adminApi.post<Record<string, unknown>>(
      `/api/admin/rbac/roles/${encodeURIComponent(role)}/permissions`,
      { patterns, reason }),

  /** ★ 重置为内置定义（表被改坏时的救命手段） */
  reset: (reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/rbac/reset', { reason }),

  check: (perm: string) =>
    adminApi.get<{ allowed: boolean; [k: string]: unknown }>(
      '/api/admin/rbac/check', { permission: perm }),
}

/* ================================================================ 套餐管理 */

export interface FeatureSpec {
  key: string
  label: string
  type: 'int' | 'bool' | 'text'
  desc: string
  unit?: string
  default?: unknown
}

export interface PeriodSpec {
  key: string
  label: string
  suffix: string
}

export interface PlanRow {
  key: string
  name: string
  /** ★ 单位是「分」（¥39 → 3900），避免浮点误差 */
  price_cents: number
  period: string
  credits: number
  features: Record<string, unknown>
  sort_order: number
  enabled: number
  updated_at?: string
}

export interface PlanPayload {
  key: string
  name: string
  price_cents: number
  period: string
  credits: number
  features: Record<string, unknown>
  sort_order: number
  enabled: boolean
}

export const plansApi = {
  list: () => adminApi.get<{ items: PlanRow[] }>('/api/admin/plans'),

  /** ★ 字段元数据（表单与对比表都从它派生） */
  meta: () => adminApi.get<{
    features: FeatureSpec[]
    periods: PeriodSpec[]
    key_rule: string
  }>('/api/admin/plans/meta'),

  create: (p: PlanPayload) =>
    adminApi.post<{ ok: boolean; item: PlanRow }>('/api/admin/plans', p),

  /** ★ 套餐标识不可改（订单与模型路由依赖它），故 key 在路径里 */
  update: (key: string, p: PlanPayload) =>
    adminApi.put<{ ok: boolean; item: PlanRow }>(
      `/api/admin/plans/${encodeURIComponent(key)}`, p),

  toggle: (key: string) =>
    adminApi.post<{ ok: boolean; enabled: number }>(
      `/api/admin/plans/${encodeURIComponent(key)}/toggle`, {}),

  remove: (key: string) =>
    adminApi.del<{ ok: boolean; deleted: string }>(
      `/api/admin/plans/${encodeURIComponent(key)}`),
}

/* ================================================================ 支付渠道 */

/**
 * 支付渠道凭据字段（来自后端 `PAYMENT_CRED_SPECS`）。
 *
 * ★ 与 `FeatureSpec` 同理念：**后端声明、前端渲染**。
 *   新增渠道只需改后端一处，前端表单自动出现对应字段。
 */
export interface PayCredField {
  key: string
  label: string
  type: string
  desc: string
  /** ★ true = 密钥类，界面**不回显**（只显示"已设置"） */
  secret: boolean
}

export interface PayChannel {
  name: string
  enabled: boolean
  fields: PayCredField[]
  /** ★ 每个字段只回布尔（已设置/未设置），**绝不回值** */
  cred_state: Record<string, boolean>
  /** 所有字段都设置了才算 complete */
  complete: boolean
}

export interface PayChannelTestResult {
  ok: boolean
  name: string
  checks: Array<{ item: string; ok: boolean; hint: string }>
  note: string
}

export const payChannelsApi = {
  list: () => adminApi.get<{
    items: PayChannel[]
    notify_base_url: string
    currency: string
    skip_signature_check: boolean
  }>('/api/admin/pay/channels'),

  /**
   * ★ 保存渠道配置。`creds` 里**留空的字段 = 不修改**（不回显也不清空）。
   * ★ `allow_unsafe` 用于危险开关（跳过签名校验）的二次确认。
   */
  save: (name: string, payload: {
    enabled: boolean
    creds: Record<string, string>
    allow_unsafe?: boolean
  }) => adminApi.put<{ ok: boolean; name: string; enabled: boolean; saved: string[] }>(
    `/api/admin/pay/channels/${encodeURIComponent(name)}`, payload),

  /** ★ 渠道自检（不只报问题，还给可执行提示） */
  test: (name: string) =>
    adminApi.post<PayChannelTestResult>(
      `/api/admin/pay/channels/${encodeURIComponent(name)}/test`, {}),
}

/* ================================================================ 支付事件（E4.14 P0①） */

export interface PayEventRow {
  id: string
  order_id: string
  provider: string
  event_type: string
  trade_no: string
  amount_cents: number
  signature_ok: number
  handled: number
  note: string
  created_at: string
}

export interface PayEventQuery {
  order_id?: string
  provider?: string
  event_type?: string
  /** 1 = 只看验签失败（伪造签名攻击痕迹） */
  sig_only?: number
  limit?: number
  offset?: number
}

export const payEventsApi = {
  /** ★ E4.14：后端已返回 {total, items}（include_total=True） */
  list: (q: PayEventQuery = {}) =>
    adminApi.get<{ total: number; items: PayEventRow[] }>('/api/admin/pay/events', {
      order_id: q.order_id || '', provider: q.provider || '',
      event_type: q.event_type || '', sig_only: q.sig_only ? 1 : 0,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),
  /** 原始报文（纠纷取证）—— 字段逐个对照后端 admin_event_raw
   * ★ 必须用**模板字符串**（`/api/.../${id}/...`），不要用字符串拼接：
   *   test_g06b_admin_pages 从前端源码正则提取 `/api/` 路径对照 openapi，
   *   拼接写法会被截断成 `/api/admin/pay/event/`（缺 `/raw`）误报"路径写错"。 */
  raw: (id: string) =>
    adminApi.get<PayEventRow & { raw_body: string }>(
      `/api/admin/pay/event/${encodeURIComponent(id)}/raw`),
}

/* ================================================================ 数据洞察 */

/**
 * ★★ 提示词版本对比（v1.0-E4.7）。
 *
 * ★ 数据来自运行时内存计数 —— 服务重启后清零。
 *   界面**必须**显示 `scope`，否则运营会误读为"历史累计"。
 */
export interface PromptVariant {
  fingerprint: string
  role: string
  gen: number
  adopt: number
  adopt_rate: number
  pending: number
}

/** 环节级采纳漏斗 */
export interface FunnelRow {
  role: string
  gen: number
  ok: number
  adopt: number
  ok_rate: number
  adopt_rate: number
  /** 修改幅度分布（分桶） */
  edit: Record<string, number>
  /** 重写次数分布（分桶） */
  regen: Record<string, number>
}

export interface PromptAbResult {
  variants: PromptVariant[]
  total_variants: number
  note: string
  /** ★ 数据范围说明（必须展示给用户） */
  scope: string
  funnel: FunnelRow[]
  suggestion?: string
}

/** 避坑记录条目（#315：用户「指出问题 → 系统修正」的沉淀） */
export interface LessonRow {
  id: string
  book_id: string
  kind: string
  /** 用户指出/要求的内容（原文截断） */
  trigger: string
  /** 系统实际修正方式（摘要） */
  fix: string
  /** 本轮涉及的变更类工具（逗号分隔） */
  tools: string
  /** active|muted */
  status: string
  /** 被注入提示词的次数 */
  hits: number
  created_at: string
}

export interface LessonsResult {
  range_days: number
  count: number
  active: number
  muted: number
  injected_hits: number
  items: LessonRow[]
  note: string
}

export const insightApi = {
  /** ★ 提示词版本对比（回答"改了 prompt 有没有变好"） */
  promptAb: (top = 10) =>
    adminApi.get<PromptAbResult>(`/api/admin/insight/prompt-ab?top=${top}`),

  /** ★ 避坑记录（#315）：用户在聊天里反复指出并要求修正的问题 */
  lessons: (days = 90) =>
    adminApi.get<LessonsResult>(`/api/admin/insight/lessons?days=${days}`),
}

/* ================================================================ 小工具 */

function today(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
