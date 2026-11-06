/* ============================================================================
 * 组件共享类型
 *
 * ★ 为什么单独放一个文件：
 *   `<script setup>` 的 SFC **不能导出类型**（编译后只导出默认组件），
 *   所以 `import Comp, { type X } from './Comp.vue'` 会失败。
 *   共享类型统一放这里。
 * ========================================================================== */

export interface TabItem {
  key: string
  label: string
  badge?: string | number
  disabled?: boolean
}

export interface MenuItem {
  key: string
  label: string
  icon?: string
  danger?: boolean
  disabled?: boolean
}

export interface TableColumn {
  key: string
  title: string
  width?: string
  align?: 'left' | 'center' | 'right'
  /** 是否可排序 */
  sortable?: boolean
}
