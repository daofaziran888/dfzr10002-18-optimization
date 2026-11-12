# 数据分析工作台

面向企业数据分析师与数据产品经理的一站式数据分析工作台。提供指标口径管理、聚合查询、分析实验设计、数据服务发布与应用集成的完整链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径管理 | `indicator` | 业务指标 | 指标编号、指标名称、计算公式 |
| 分析数据源 | `datasource` | 数据源连接 | 连接编号、连接名称、数据库类型 |
| 聚合查询配置 | `query` | 查询模板 | 模板编号、模板名称、查询语句 |
| 分析实验管理 | `experiment` | 分析实验 | 实验编号、实验名称、分析数据集 |
| 分析数据集 | `dataset` | 数据集 | 数据集编号、数据集名称、来源表名 |
| 可视化图表配置 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 分析报告管理 | `report` | 分析报告 | 报告编号、报告标题、关联数据集 |
| 数据服务发布 | `service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 数据看板 | `dashboard` | 看板面板 | 面板编号、面板名称、布局配置、布局修订号 |
| 分析维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度表名 |
| 全局筛选器 | `filter` | 筛选器 | 筛选器编号、筛选器名称、关联字段 |
| 定时分析任务 | `schedule` | 定时任务 | 任务编号、任务名称、分析类型 |
| 数据导出配置 | `export` | 导出模板 / 导出任务 | 模板编号、模板名称、数据源绑定、快照面板版本 |
| 查询缓存管理 | `cache` | 缓存规则 | 规则编号、缓存目标、缓存时间 |
| 分析模型管理 | `model` | 分析模型 | 模型编号、模型名称、模型类型 |
| 分析操作日志 | `log` | 操作日志 / 审计台账 | 日志编号、关联版本、图表编号、复核状态 |

## 看板布局版本

布局发布把「面板顺序 + 图表集合 + 可见范围」落成一个版本快照，相关规则都在
`backend/app/services/layout.py`：

- 每个面板维护 `布局修订号`。发布/回滚都会递增；发布请求可带 `expected_revision`，
  服务端当前值不一致就判定版本冲突，两个编辑者同时提交时只有一个能成功。
- 整组发布是事务：多条布局一次提交，任一冲突或校验失败则整组不落库，并返回逐条冲突说明。
- 回滚是定向的：只恢复目标版本中「不在当前面板 / 已归档」的图表，图表后来手工调整的
  筛选条件等配置保留，目标版本之后新增的图表也不会被回滚掉。`scope` 可取
  `all` / `layout` / `charts`。回滚前对相关表做快照，失败会整体还原。
- 受影响图表：图表集合变化时是增删的那几张；仅布局/顺序/可见范围变化时是面板内全部图表。
- 发布/回滚同时写入审计台账（`log` 模块，`台账类型=布局审计`）、复核记录与导出任务快照，
  三处都带同一个 `关联版本`（如 `DASH-0001@V2`）；图表行上的 `版本标记` 指向最近影响它的版本。
- 刷新是定向的：`POST /api/dashboard/{id}/refresh` 默认只重算 `版本标记 != 最近刷新版本` 的图表。
- 排查入口：`GET /api/dashboard/{id}/trace` 串起版本链、图表刷新状态、台账、复核与导出任务。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/dashboard/publish` | 整组发布布局 |
| `POST /api/dashboard/rollback` | 定向回滚到指定版本 |
| `GET /api/dashboard/{id}/trace` | 发布 → 图表刷新链路排查 |
| `POST /api/dashboard/{id}/refresh` | 定向刷新待刷新图表 |
| `GET /api/dashboard/versions` | 布局版本列表 |
| `GET /api/layout/ledger` `?version=` | 审计台账（按版本过滤） |
| `GET /api/layout/reviews` `?version=` | 复核记录（状态随版本同步） |
| `GET /api/export/tasks` | 导出任务清单（导出快照与面板版本同步） |

后端回归脚本：

```bash
cd backend
.venv/bin/python tests/test_layout_scenarios.py   # service 层场景
.venv/bin/python tests/test_layout_http.py        # HTTP 端到端场景
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.tsx`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 静态路径（如 `/export`、`/versions`）必须注册在动态路径 `/{entry_id}` 之前，否则会被吞掉。

领域：数据工程
子方向：数据分析与数据应用
