import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/export'

type ExportTask = {
  id: number
  任务编号: string
  面板编号: string
  面板名称: string
  快照面板版本: string
  图表集合: string
  导出格式: string
  任务状态: string
  导出快照: { 图表编号: string; 图表名称: string; 版本标记: string; 筛选条件: string }[]
}
const columns = [
  "模板编号",
  "模板名称",
  "数据源绑定",
  "导出格式",
  "字段选择",
  "筛选参数",
  "导出上限",
  "是否启用"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "导出模板总数",
    "value": 0
  },
  {
    "label": "待校验导出模板",
    "value": 0
  },
  {
    "label": "已启用导出模板",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function ExportView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [tasks, setTasks] = useState<ExportTask[]>([])
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('导出模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据导出配置列表读取失败')
    }
  }, [filters])

  const reloadTasks = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/tasks?size=100`)
      if (!response.ok) return
      const payload = await response.json()
      setTasks(payload.items ?? [])
    } catch {
      // 导出任务快照读取失败不影响模板列表
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadTasks()
  }, [reload, reloadTasks])

  async function syncTasks() {
    try {
      const response = await request(`${ENDPOINT}/tasks/sync`, { method: 'POST' })
      const payload = await response.json()
      setNotice(payload.message)
      await reloadTasks()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '导出任务对齐失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('数据导出配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据导出配置操作失败')
    }
  }

  return (
    <section className="page" data-module="export">
      <header className="page-head">
        <div>
          <h2>数据导出配置管理</h2>
          <p className="page-desc">维护分析结果的导出模板、格式配置与行数控制，实现分析数据灵活输出。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('导出模板登记入口尚未接入审批流')}>
            登记导出模板
          </button>
          <button className="btn" type="button" onClick={() => void syncTasks()}>
            对齐导出任务快照
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据导出配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据导出配置数据，可先登记导出模板</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="layout-panel">
        <h3>导出任务清单</h3>
        <p className="page-desc">导出快照与面板当前布局版本同步；布局回滚后快照版本随之更新。</p>
        <table className="data-table">
          <thead>
            <tr><th>任务编号</th><th>面板</th><th>快照面板版本</th><th>图表快照</th><th>任务状态</th></tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>{task.任务编号}</td>
                <td>{task.面板编号} {task.面板名称}</td>
                <td>{task.快照面板版本}</td>
                <td className="snapshot-cell">
                  {task.导出快照.map((chart) => `${chart.图表编号}@${chart.版本标记}`).join('、') || '—'}
                </td>
                <td>{task.任务状态}</td>
              </tr>
            ))}
            {tasks.length === 0 && (
              <tr><td className="empty-state" colSpan={5}>暂无导出任务，可点击「对齐导出任务快照」补齐</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条数据导出配置记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
