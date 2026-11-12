"""数据导出配置业务规则：状态流转、字段校验与筛选口径都收在这里。

数据导出配置行是导出模板；看板布局发布/回滚还会往 ``export_task`` 表
维护每个面板的导出任务快照，快照里的面板版本与当前布局版本保持一致。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "export"
EXPORT_TASK = "export_task"
REQUIRED_FIELDS = ["模板编号", "模板名称", "数据源绑定"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ExportService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"导出模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据导出配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"导出模板已{action}"

    # ------------------------------------------------------- 导出任务快照
    def list_tasks(
        self,
        *,
        keyword: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """导出任务清单：导出快照里的面板版本与当前布局版本保持一致。"""
        rows = list(store.rows(EXPORT_TASK))
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("任务编号", ""))
                or keyword in str(row.get("面板编号", ""))
                or keyword in str(row.get("任务名称", ""))
            ]
        if version:
            rows = [row for row in rows if str(row.get("快照面板版本")) == version]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def sync_tasks(self) -> list[dict[str, Any]]:
        """给还没有导出任务的面板补一条快照任务，返回全部任务清单。"""
        existing = {
            int(row.get("dashboard_id", 0)) for row in store.rows(EXPORT_TASK)
        }
        for panel in store.rows("dashboard"):
            if int(panel.get("id", 0)) in existing:
                continue
            if not str(panel.get("最近发布版本") or ""):
                continue
            panel_no = str(panel.get("面板编号") or "")
            version_key = f"{panel_no}@{panel.get('最近发布版本')}"
            charts = [
                chart
                for chart in store.rows("chart")
                if str(chart.get("所属面板")) == panel_no
            ]
            store.rows(EXPORT_TASK).append({
                "id": max(
                    (int(row.get("id", 0)) for row in store.rows(EXPORT_TASK)), default=0
                ) + 1,
                "任务编号": f"EXPT-{int(panel.get('id', 0)):04d}",
                "dashboard_id": int(panel.get("id", 0)),
                "面板编号": panel_no,
                "面板名称": panel.get("面板名称"),
                "任务名称": f"{panel.get('面板名称')} 导出任务",
                "导出格式": "PDF",
                "任务状态": "待导出",
                "关联版本": version_key,
                "快照面板版本": version_key,
                "版本标记": version_key,
                "图表集合": ",".join(str(chart.get("图表编号")) for chart in charts),
                "导出快照": [
                    {
                        "图表编号": chart.get("图表编号"),
                        "图表名称": chart.get("图表名称"),
                        "图表类型": chart.get("图表类型"),
                        "筛选条件": chart.get("筛选条件"),
                        "版本标记": chart.get("版本标记"),
                    }
                    for chart in charts
                ],
                "创建人": "值班管理员",
                "创建时间": "",
                "更新时间": "",
                "更新人": "值班管理员",
                "status": "待导出",
                "pending": True,
                "abnormal": False,
            })
        return list(store.rows(EXPORT_TASK))
