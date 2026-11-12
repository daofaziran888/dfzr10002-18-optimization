"""可视化图表配置业务规则：状态流转、字段校验与筛选口径都收在这里。

图表行上的 ``版本标记`` 由看板布局发布/回滚写入，指向最近一次影响它的布局版本；
``最近刷新版本`` 记录图表实际重算时对应的版本，两者不一致即表示图表待刷新。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "chart"
REQUIRED_FIELDS = ["图表编号", "图表名称", "图表类型"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ChartService:
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
            rows = [row for row in rows if keyword in str(row.get("图表编号", ""))]
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
            return None, f"图表配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于可视化图表配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"图表配置已{action}"

    def update_config(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """手工调整图表配置（如筛选条件）；记录调整时间，回滚时用来判断是否保留后续改动。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"图表配置 {entry_id} 不存在或已归档"
        editable = ["图表名称", "图表类型", "数据源绑定", "横轴字段", "纵轴字段", "筛选条件", "刷新间隔"]
        changed: list[str] = []
        for field in editable:
            if field in values and values[field] is not None:
                entry[field] = values[field]
                changed.append(field)
        if not changed:
            return None, "没有提供可调整的图表配置字段"
        entry["手工调整时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return entry, f"图表配置已更新：{'、'.join(changed)}"
