import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dashboard'
const LAYOUT_ENDPOINT = '/api/layout'
const columns = [
  "面板编号",
  "面板名称",
  "布局配置",
  "图表集合",
  "刷新频率",
  "可见范围",
  "最近发布版本",
  "版本状态"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]

type Row = Record<string, string | number | boolean | null>

type Chart = { id: number; 图表编号: string; 图表名称: string; 版本标记?: string }

type Version = {
  id: number
  版本编号: string
  版本号: string
  dashboard_id: number
  面板编号: string
  修订号: number
  '图表集合': string
  '可见范围': string
  '受影响图表': string[]
  状态: string
  是当前版本: boolean
  发布人: string
  发布时间: string
}

type TraceChart = {
  图表编号: string
  图表名称: string
  版本标记: string
  最近刷新版本: string
  刷新状态: string
}

type Trace = {
  当前版本: string
  布局修订号: number
  图表刷新: TraceChart[]
  待刷新图表: string[]
  审计台账: Row[]
  复核记录: Row[]
}

type ConflictItem = {
  dashboard_id: number
  面板编号: string
  reason: string
  expected_revision: number | null
  current_revision: number | null
}

export default function DashboardView() {
  const [rows, setRows] = useState<Row[]>([])
  const [charts, setCharts] = useState<Chart[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [conflicts, setConflicts] = useState<ConflictItem[]>([])
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 整组发布：每个被选中的面板都保留提交时的修订号，用于服务端做 CAS 冲突检测。
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [draftCharts, setDraftCharts] = useState<Record<number, string[]>>({})
  const [draftTeams, setDraftTeams] = useState<Record<number, string>>({})
  const [draftLayout, setDraftLayout] = useState<Record<number, string>>({})
  const [publishBase, setPublishBase] = useState<Record<number, number>>({})

  // 版本与回滚
  const [versions, setVersions] = useState<Version[]>([])
  const [activePanel, setActivePanel] = useState<number | null>(null)
  const [trace, setTrace] = useState<Trace | null>(null)
  const [rollbackTarget, setRollbackTarget] = useState<string>('')

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, chartResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request('/api/chart?size=200'),
      ])
      if (!listResponse.ok) {
        throw new Error('看板面板列表读取失败')
      }
      const payload = await listResponse.json()
      const chartPayload = chartResponse.ok ? await chartResponse.json() : { items: [] }
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setCharts(chartPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据看板列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadVersions = useCallback(async (dashboardId: number) => {
    setActivePanel(dashboardId)
    setRollbackTarget('')
    try {
      const [versionResponse, traceResponse] = await Promise.all([
        request(`${ENDPOINT}/versions?dashboard_id=${dashboardId}`),
        request(`${ENDPOINT}/${dashboardId}/trace`),
      ])
      const versionPayload = versionResponse.ok ? await versionResponse.json() : { items: [] }
      const tracePayload = traceResponse.ok ? await traceResponse.json() : null
      setVersions(versionPayload.items ?? [])
      setTrace(tracePayload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '布局版本读取失败')
    }
  }, [])

  function toggleSelect(row: Row, checked: boolean) {
    const id = Number(row.id)
    setSelected((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
    if (checked) {
      const chartSet = String(row['图表集合'] ?? '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)
      setDraftCharts((prev) => ({ ...prev, [id]: chartSet }))
      setDraftTeams((prev) => ({ ...prev, [id]: String(row['可见范围'] ?? '') }))
      setDraftLayout((prev) => ({ ...prev, [id]: String(row['布局配置'] ?? '') }))
      setPublishBase((prev) => ({ ...prev, [id]: Number(row['布局修订号'] ?? 0) }))
    }
  }

  function toggleChart(dashboardId: number, chartNo: string) {
    setDraftCharts((prev) => {
      const current = prev[dashboardId] ?? []
      const next = current.includes(chartNo)
        ? current.filter((item) => item !== chartNo)
        : [...current, chartNo]
      return { ...prev, [dashboardId]: next }
    })
  }

  async function submitBatch() {
    setErrorMessage('')
    setNotice('')
    setConflicts([])
    const items = [...selected].map((id) => ({
      dashboard_id: id,
      charts: draftCharts[id] ?? [],
      layout: draftLayout[id] ?? '',
      visible_teams: (draftTeams[id] ?? '')
        .split(',')
        .map((team) => team.trim())
        .filter(Boolean),
      expected_revision: publishBase[id] ?? 0,
    }))
    try {
      const response = await request(`${ENDPOINT}/publish`, {
        method: 'POST',
        body: JSON.stringify({ items, operator: '值班管理员' }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message)
        setConflicts(payload.conflicts ?? [])
        return
      }
      setNotice(payload.message)
      setSelected(new Set())
      await reload()
      if (activePanel !== null) await loadVersions(activePanel)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '整组发布失败')
    }
  }

  async function runRollback(scope: string) {
    if (activePanel === null || !rollbackTarget) {
      setErrorMessage('请先选择一个历史版本再回滚')
      return
    }
    setErrorMessage('')
    setNotice('')
    setConflicts([])
    try {
      const response = await request(`${ENDPOINT}/rollback`, {
        method: 'POST',
        body: JSON.stringify({
          dashboard_id: activePanel,
          target_version: rollbackTarget,
          scope,
          operator: '值班管理员',
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message)
        return
      }
      setNotice(
        `${payload.message}${payload.preserved?.length ? `（保留后续改动：${payload.preserved.join('、')}）` : ''}`,
      )
      await reload()
      await loadVersions(activePanel)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '回滚失败')
    }
  }

  async function refreshCharts() {
    if (activePanel === null) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${activePanel}/refresh`, {
        method: 'POST',
        body: JSON.stringify({ all: false }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.detail ?? '定向刷新失败')
      }
      setNotice(`${payload.message}：${payload.refreshed.join('、') || '无待刷新图表'}`)
      await loadVersions(activePanel)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定向刷新失败')
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
        throw new Error('数据看板动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据看板操作失败')
    }
  }

  const statValues = [
    { label: '看板面板总数', value: total },
    { label: '当前已选面板', value: selected.size },
    { label: '待刷新图表', value: trace?.待刷新图表.length ?? 0 },
  ]

  return (
    <section className="page" data-module="dashboard">
      <header className="page-head">
        <div>
          <h2>数据看板管理</h2>
          <p className="page-desc">
            维护数据看板的面板布局、图表组合与可见范围；布局发布生成版本快照，回滚只恢复目标版本中受影响的图表。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" disabled={selected.size === 0} onClick={() => void submitBatch()}>
            整组发布（{selected.size}）
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据看板清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statValues.map((item) => (
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
            <th>选择</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>
                <input
                  type="checkbox"
                  checked={selected.has(Number(row.id))}
                  onChange={(event) => toggleSelect(row, event.target.checked)}
                />
              </td>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void loadVersions(Number(row.id))}>
                  版本/链路
                </button>
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无数据看板数据，可先登记看板面板</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected.size > 0 && (
        <section className="layout-panel">
          <h3>整组发布草稿</h3>
          <p className="page-desc">勾选要一起发布的面板；提交时带上打开页面时的布局修订号，任一冲突则整组不落库。</p>
          {[...selected].map((id) => {
            const row = rows.find((item) => Number(item.id) === id)
            return (
              <article key={id} className="layout-draft">
                <strong>{row?.['面板编号'] ?? id} {row?.['面板名称'] ?? ''}</strong>
                <label className="filter-item">
                  <span>布局配置</span>
                  <input
                    value={draftLayout[id] ?? ''}
                    onChange={(event) => setDraftLayout((prev) => ({ ...prev, [id]: event.target.value }))}
                  />
                </label>
                <label className="filter-item">
                  <span>可见团队（逗号分隔）</span>
                  <input
                    value={draftTeams[id] ?? ''}
                    onChange={(event) => setDraftTeams((prev) => ({ ...prev, [id]: event.target.value }))}
                  />
                </label>
                <div className="chart-picks">
                  <span>图表集合</span>
                  {charts.map((chart) => (
                    <label key={chart.id} className="chip">
                      <input
                        type="checkbox"
                        checked={(draftCharts[id] ?? []).includes(chart.图表编号)}
                        onChange={() => toggleChart(id, chart.图表编号)}
                      />
                      {chart.图表编号} {chart.图表名称}
                    </label>
                  ))}
                </div>
              </article>
            )
          })}
        </section>
      )}

      {conflicts.length > 0 && (
        <section className="conflict-panel">
          <h3>版本冲突说明</h3>
          <p className="page-desc">以下条目未通过校验，整组发布已回退；未落任何版本。</p>
          <ul>
            {conflicts.map((conflict, index) => (
              <li key={`${conflict.dashboard_id}-${index}`}>
                <strong>{conflict.面板编号 || conflict.dashboard_id}</strong>：{conflict.reason}
              </li>
            ))}
          </ul>
        </section>
      )}

      {notice && (
        <section className="layout-panel">
          <span className="notice-text">{notice}</span>
        </section>
      )}

      {activePanel !== null && (
        <section className="layout-panel">
          <header className="layout-head">
            <h3>布局版本与发布链路</h3>
            <button className="btn ghost" type="button" onClick={() => { setActivePanel(null); setTrace(null); setVersions([]) }}>
              关闭
            </button>
          </header>

          {trace && (
            <div className="trace-grid">
              <article className="stat-card">
                <span className="stat-label">当前版本</span>
                <strong className="stat-value">{trace.当前版本 || '—'}</strong>
              </article>
              <article className="stat-card">
                <span className="stat-label">布局修订号</span>
                <strong className="stat-value">{trace.布局修订号}</strong>
              </article>
              <article className="stat-card">
                <span className="stat-label">待刷新图表</span>
                <strong className="stat-value">{trace.待刷新图表.join('、') || '无'}</strong>
              </article>
            </div>
          )}

          <table className="data-table">
            <thead>
              <tr><th>版本</th><th>修订号</th><th>图表集合</th><th>可见范围</th><th>受影响图表</th><th>状态</th><th>发布人</th><th>发布时间</th></tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={version.id}>
                  <td>{version.版本编号}</td>
                  <td>{version.修订号}</td>
                  <td>{version['图表集合'] || '—'}</td>
                  <td>{version['可见范围'] || '—'}</td>
                  <td>{version['受影响图表']?.join('、') || '无'}</td>
                  <td>
                    {version.状态}
                    {version.是当前版本 && <span className="badge">当前</span>}
                  </td>
                  <td>{version.发布人}</td>
                  <td>{version.发布时间}</td>
                </tr>
              ))}
              {versions.length === 0 && (
                <tr><td className="empty-state" colSpan={8}>该面板还没有布局版本</td></tr>
              )}
            </tbody>
          </table>

          <div className="filter-bar">
            <label className="filter-item">
              <span>回滚到版本</span>
              <select value={rollbackTarget} onChange={(event) => setRollbackTarget(event.target.value)}>
                <option value="">选择历史版本</option>
                {versions.filter((version) => !version.是当前版本).map((version) => (
                  <option key={version.id} value={version.版本号}>{version.版本编号}</option>
                ))}
              </select>
            </label>
            <button className="btn" type="button" onClick={() => void runRollback('all')}>回滚布局与图表</button>
            <button className="btn" type="button" onClick={() => void runRollback('charts')}>只回滚图表集合</button>
            <button className="btn ghost" type="button" onClick={() => void refreshCharts()}>定向刷新待刷新图表</button>
          </div>

          {trace && (
            <table className="data-table">
              <thead>
                <tr><th>图表编号</th><th>图表名称</th><th>版本标记</th><th>最近刷新版本</th><th>刷新状态</th></tr>
              </thead>
              <tbody>
                {trace.图表刷新.map((chart) => (
                  <tr key={chart.图表编号}>
                    <td>{chart.图表编号}</td>
                    <td>{chart.图表名称}</td>
                    <td>{chart.版本标记 || '—'}</td>
                    <td>{chart.最近刷新版本 || '—'}</td>
                    <td>{chart.刷新状态}</td>
                  </tr>
                ))}
                {trace.图表刷新.length === 0 && (
                  <tr><td className="empty-state" colSpan={5}>当前面板没有图表</td></tr>
                )}
              </tbody>
            </table>
          )}

          {trace && (
            <div className="trace-grid">
              <article>
                <h4>审计台账</h4>
                <ul className="trace-list">
                  {trace.审计台账.slice(0, 6).map((item) => (
                    <li key={String(item.id)}>
                      {item['台账编号']} · {item['图表编号'] || '面板'} · {item['关联版本']} · {item['事件类型']}
                    </li>
                  ))}
                  {trace.审计台账.length === 0 && <li>暂无台账条目</li>}
                </ul>
              </article>
              <article>
                <h4>复核记录</h4>
                <ul className="trace-list">
                  {trace.复核记录.slice(0, 6).map((item) => (
                    <li key={String(item.id)}>
                      {item['复核编号']} · {item['关联版本']} · {item['复核状态']}（{item['布局版本状态']}）
                    </li>
                  ))}
                  {trace.复核记录.length === 0 && <li>暂无复核记录</li>}
                </ul>
              </article>
            </div>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据看板记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
