"""看板布局版本服务：布局快照、整组发布、定向回滚与联动台账。

设计要点：
- 每个看板面板维护 ``布局修订号``，每次发布/回滚都会递增；发布时提交的
  ``expected_revision`` 与当前修订号不一致即视为版本冲突（CAS）。
- 发布是「整组」事务：一批面板里任意一条校验失败或冲突，所有面板都不落库。
- 版本快照保存布局配置、面板顺序、图表集合、可见范围以及每张图表的配置副本，
  回滚时只按「目标版本里受影响的图表」做定向恢复，图表后来手工调整的筛选条件
  等配置不会被旧快照覆盖；目标版本之后新增的图表也保留。
- 发布/回滚会同步写审计台账、复核记录与导出任务快照，三处都带同一个版本编号，
  图表行上的 ``版本标记`` 指向最近一次影响它的布局版本。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any, Iterable

from app.store import store

DASHBOARD = "dashboard"
CHART = "chart"
VERSION = "layout_version"
AUDIT = "log"
REVIEW = "review"
EXPORT_TASK = "export_task"

TRANSACTION_MODULES = [DASHBOARD, CHART, VERSION, AUDIT, REVIEW, EXPORT_TASK]

# 发布、回滚会同时改多张表；用可重入锁把「校验 + 落库」放在一个临界区里，
# 并发提交时后到的请求只能看到先到请求提交后的修订号，从而判定为版本冲突。
_PUBLISH_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _version_label(seq: int) -> str:
    return f"V{seq}"


def _panel_no(panel: dict[str, Any]) -> str:
    return str(panel.get("面板编号") or "")


def _version_key(panel_no: str, label: str) -> str:
    return f"{panel_no}@{label}"


def _split_refs(value: Any) -> list[str]:
    """图表集合既可能是逗号/顿号分隔的字符串，也可能是列表，这里统一成编号列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        normalized = str(value).replace("，", ",").replace("、", ",").replace(";", ",")
        items = [item.strip() for item in normalized.split(",")]
    return [item for item in items if item]


def _chart_no(chart: dict[str, Any]) -> str:
    return str(chart.get("图表编号") or "")


def _resolve_chart(ref: Any) -> dict[str, Any] | None:
    """发布参数里的图表既可以传主键 id，也可以传图表编号。"""
    text = str(ref).strip()
    if not text:
        return None
    if text.isdigit():
        found = store.find(CHART, int(text))
        if found is not None:
            return found
    for row in store.rows(CHART):
        if _chart_no(row) == text:
            return row
    return None


def _panel_chart_rows(panel: dict[str, Any]) -> list[dict[str, Any]]:
    chart_refs = set(_split_refs(panel.get("图表集合")))
    order = _split_refs(panel.get("面板顺序")) or _split_refs(panel.get("图表集合"))
    rows: list[dict[str, Any]] = []
    for ref in order:
        chart = _resolve_chart(ref)
        if chart is not None and _chart_no(chart) in chart_refs and chart not in rows:
            rows.append(chart)
    for chart in store.rows(CHART):
        if _chart_no(chart) in chart_refs and chart not in rows:
            rows.append(chart)
    return rows


def _chart_snapshot(chart: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(chart)


class LayoutService:
    """看板布局版本的核心规则，全部状态变更都收敛在这里。"""

    # ------------------------------------------------------------------ 查询
    def list_versions(
        self,
        *,
        dashboard_id: int | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(VERSION))
        if dashboard_id is not None:
            rows = [row for row in rows if int(row.get("dashboard_id", 0)) == dashboard_id]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("版本编号", "")) or keyword in str(row.get("面板编号", ""))
            ]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        return store.find(VERSION, version_id)

    def find_version(self, dashboard_id: int, label: str | None = None, version_id: int | None = None) -> dict[str, Any] | None:
        if version_id is not None:
            found = self.get_version(version_id)
            if found is not None and int(found.get("dashboard_id", 0)) == dashboard_id:
                return found
            return None
        rows = [
            row for row in store.rows(VERSION)
            if int(row.get("dashboard_id", 0)) == dashboard_id
        ]
        if label:
            wanted = str(label).strip().lstrip("@").upper()
            for row in rows:
                if str(row.get("版本号", "")).upper() == wanted or str(row.get("版本编号", "")).upper() == wanted:
                    return row
            return None
        current = [row for row in rows if row.get("是当前版本")]
        if current:
            return max(current, key=lambda row: int(row.get("修订号", 0)))
        return max(rows, key=lambda row: int(row.get("修订号", 0)), default=None)

    def ledger(
        self,
        *,
        version_key: str | None = None,
        dashboard_id: int | None = None,
        page: int = 1,
        size: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(AUDIT))
        if version_key:
            rows = [row for row in rows if str(row.get("关联版本")) == version_key]
        if dashboard_id is not None:
            rows = [row for row in rows if int(row.get("dashboard_id", 0)) == dashboard_id]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def reviews(
        self,
        *,
        version_key: str | None = None,
        page: int = 1,
        size: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(REVIEW))
        if version_key:
            rows = [row for row in rows if str(row.get("关联版本")) == version_key]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def export_tasks(
        self,
        *,
        dashboard_id: int | None = None,
        page: int = 1,
        size: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(EXPORT_TASK))
        if dashboard_id is not None:
            rows = [row for row in rows if int(row.get("dashboard_id", 0)) == dashboard_id]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def trace(self, dashboard_id: int) -> dict[str, Any] | None:
        """发布 → 图表刷新链路：把版本、台账、复核、导出快照和图表刷新状态串起来。"""
        panel = store.find(DASHBOARD, dashboard_id)
        if panel is None:
            return None
        panel_no = _panel_no(panel)
        versions, _ = self.list_versions(dashboard_id=dashboard_id, page=1, size=1000)
        current = next((row for row in versions if row.get("是当前版本")), None)
        current_key = str(current.get("版本编号")) if current else ""

        charts = _panel_chart_rows(panel)
        chart_lines = []
        for chart in charts:
            marker = str(chart.get("版本标记") or "")
            refreshed = str(chart.get("最近刷新版本") or "")
            chart_lines.append({
                "图表编号": _chart_no(chart),
                "图表名称": chart.get("图表名称"),
                "版本标记": marker,
                "最近刷新版本": refreshed,
                "刷新状态": "已刷新" if marker and marker == refreshed else "待刷新",
                "是否归档": bool(chart.get("是否归档")),
            })

        ledger_rows, _ = self.ledger(dashboard_id=dashboard_id, page=1, size=1000)
        review_rows, _ = self.reviews(page=1, size=1000)
        review_rows = [row for row in review_rows if int(row.get("dashboard_id", 0)) == dashboard_id]
        export_rows, _ = self.export_tasks(dashboard_id=dashboard_id, page=1, size=1000)
        export_rows = [
            row for row in export_rows
            if str(row.get("快照面板版本")) == current_key or not current_key
        ]

        return {
            "dashboard_id": dashboard_id,
            "面板编号": panel_no,
            "面板名称": panel.get("面板名称"),
            "当前版本": current_key,
            "布局修订号": int(panel.get("布局修订号", 0)),
            "版本链": versions,
            "图表刷新": chart_lines,
            "待刷新图表": [line["图表编号"] for line in chart_lines if line["刷新状态"] == "待刷新"],
            "审计台账": ledger_rows,
            "复核记录": review_rows,
            "导出任务": export_rows,
        }

    # ------------------------------------------------------------- 并发控制
    def _current_revision(self, dashboard_id: int) -> int | None:
        panel = store.find(DASHBOARD, dashboard_id)
        if panel is None:
            return None
        return int(panel.get("布局修订号", 0) or 0)

    def _resolve_items(self, items: Iterable[Any]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for item in items:
            if hasattr(item, "model_dump"):
                data = item.model_dump()
            else:
                data = dict(item)
            resolved.append(data)
        return resolved

    def _base_revisions(self, items: list[dict[str, Any]]) -> dict[int, int | None]:
        """在进入临界区之前抓取基准修订号：同时提交的两个编辑者会拿到同一个旧值。"""
        bases: dict[int, int | None] = {}
        for item in items:
            dashboard_id = int(item.get("dashboard_id") or 0)
            expected = item.get("expected_revision")
            if expected is None:
                bases[dashboard_id] = self._current_revision(dashboard_id)
            else:
                bases[dashboard_id] = int(expected)
        return bases

    # ------------------------------------------------------------------ 发布
    def publish(
        self,
        items: Iterable[Any],
        *,
        operator: str = "值班管理员",
        remark: str | None = None,
    ) -> dict[str, Any]:
        raw_items = self._resolve_items(items)
        if not raw_items:
            return {"ok": False, "message": "整组发布至少需要一个面板", "conflicts": [], "versions": []}

        base = self._base_revisions(raw_items)

        with _PUBLISH_LOCK:
            conflicts, normalized = self._validate_publish(raw_items, base)
            if conflicts:
                return {
                    "ok": False,
                    "message": "整组发布未生效：存在版本冲突或字段校验失败，所有面板均保持原状态",
                    "conflicts": conflicts,
                    "versions": [],
                }
            snapshot = store.snapshot(TRANSACTION_MODULES)
            try:
                versions = [
                    self._apply_publish(panel_item, operator=operator, remark=remark)
                    for panel_item in normalized
                ]
            except Exception as exc:  # noqa: BLE001 - 任何异常都整组回滚
                store.restore(snapshot)
                return {
                    "ok": False,
                    "message": f"整组发布失败，已回到发布前状态：{exc}",
                    "conflicts": [],
                    "versions": [],
                }
            return {
                "ok": True,
                "message": f"已整组发布 {len(versions)} 个面板布局",
                "conflicts": [],
                "versions": versions,
                "results": [
                    {
                        "dashboard_id": version["dashboard_id"],
                        "面板编号": version["面板编号"],
                        "版本编号": version["版本编号"],
                        "受影响图表": version["受影响图表"],
                    }
                    for version in versions
                ],
            }

    def _validate_publish(
        self,
        items: list[dict[str, Any]],
        base: dict[int, int | None],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        conflicts: list[dict[str, Any]] = []
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()

        for index, item in enumerate(items):
            dashboard_id = int(item.get("dashboard_id") or 0)
            panel = store.find(DASHBOARD, dashboard_id)
            if panel is None:
                conflicts.append({
                    "dashboard_id": dashboard_id,
                    "面板编号": "",
                    "reason": "面板不存在或已归档",
                    "expected_revision": base.get(dashboard_id),
                    "current_revision": None,
                    "index": index,
                })
                continue
            panel_no = _panel_no(panel)
            if dashboard_id in seen:
                conflicts.append({
                    "dashboard_id": dashboard_id,
                    "面板编号": panel_no,
                    "reason": "同一批次里重复提交了同一个面板",
                    "expected_revision": base.get(dashboard_id),
                    "current_revision": self._current_revision(dashboard_id),
                    "index": index,
                })
                continue
            seen.add(dashboard_id)

            current_revision = self._current_revision(dashboard_id)
            expected = base.get(dashboard_id)
            if expected is not None and current_revision != int(expected):
                conflicts.append({
                    "dashboard_id": dashboard_id,
                    "面板编号": panel_no,
                    "reason": (
                        f"版本冲突：编辑基于第 {expected} 次布局修订，"
                        f"服务端已变为第 {current_revision} 次；其他编辑者已先发布成功"
                    ),
                    "expected_revision": expected,
                    "current_revision": current_revision,
                    "index": index,
                })
                continue

            charts_raw = item.get("charts")
            if charts_raw is None:
                charts_raw = item.get("chart_ids") or []
            chart_rows: list[dict[str, Any]] = []
            bad_refs: list[str] = []
            for ref in _split_refs(charts_raw):
                chart = _resolve_chart(ref)
                if chart is None:
                    bad_refs.append(str(ref))
                elif chart not in chart_rows:
                    chart_rows.append(chart)
            if bad_refs:
                conflicts.append({
                    "dashboard_id": dashboard_id,
                    "面板编号": panel_no,
                    "reason": f"图表不存在或已归档：{'、'.join(bad_refs)}",
                    "expected_revision": expected,
                    "current_revision": current_revision,
                    "index": index,
                })
                continue

            visible_raw = item.get("visible_teams")
            if visible_raw is None:
                visible_raw = _split_refs(panel.get("可见范围"))
            if isinstance(visible_raw, str):
                visible_teams = _split_refs(visible_raw)
            else:
                visible_teams = [str(team).strip() for team in visible_raw if str(team).strip()]
            if not visible_teams:
                visible_teams = _split_refs(panel.get("可见范围"))

            panel_order_raw = item.get("panel_order")
            if panel_order_raw is None:
                panel_order = [_chart_no(chart) for chart in chart_rows]
            else:
                panel_order = _split_refs(panel_order_raw)

            normalized.append({
                "panel": panel,
                "dashboard_id": dashboard_id,
                "charts": chart_rows,
                "layout": item.get("layout"),
                "panel_order": panel_order,
                "visible_teams": visible_teams,
                "name": item.get("name") or panel.get("面板名称"),
                "remark": item.get("remark"),
            })
        return conflicts, normalized

    def _apply_publish(
        self,
        panel_item: dict[str, Any],
        *,
        operator: str,
        remark: str | None,
    ) -> dict[str, Any]:
        panel = panel_item["panel"]
        panel_no = _panel_no(panel)
        chart_rows: list[dict[str, Any]] = panel_item["charts"]
        chart_set = [_chart_no(chart) for chart in chart_rows]

        previous_set = _split_refs(panel.get("图表集合"))
        previous_order = _split_refs(panel.get("面板顺序"))
        previous_layout = str(panel.get("布局配置") or "")
        previous_teams = _split_refs(panel.get("可见范围"))

        layout = panel_item["layout"]
        if layout is None:
            layout = previous_layout
        order_changed = list(panel_item["panel_order"]) != previous_order
        layout_changed = str(layout) != previous_layout
        visibility_changed = list(panel_item["visible_teams"]) != previous_teams
        set_changed = set(chart_set) != set(previous_set)

        if set_changed:
            affected = sorted(set(chart_set) ^ set(previous_set))
        elif layout_changed or order_changed or visibility_changed:
            # 布局、顺序或可见范围变化时，面板内所有图表的呈现都会变，都算受影响。
            affected = list(chart_set)
        else:
            affected = []

        revision = int(panel.get("布局修订号", 0) or 0) + 1
        # 版本号直接由修订号推导：并发的两个编辑者只有一个能递增成功，
        # 因此不会出现两个面板批次共用同一个版本号的情况。
        label = _version_label(revision)
        version_key = _version_key(panel_no, label)

        for chart in chart_rows:
            chart["是否归档"] = False
            chart["所属面板"] = panel_no
            chart["版本标记"] = version_key
            if not chart.get("最近刷新版本"):
                chart["最近刷新版本"] = ""
        # 被移出图表的行保留下来，标记归档并记录影响它的版本，方便审计定位。
        for row in store.rows(CHART):
            if _chart_no(row) in previous_set and _chart_no(row) not in chart_set:
                row["是否归档"] = True
                row["所属面板"] = ""
                row["版本标记"] = version_key

        panel.update({
            "布局配置": layout,
            "面板顺序": ",".join(panel_item["panel_order"]),
            "图表集合": ",".join(chart_set),
            "可见范围": ",".join(panel_item["visible_teams"]),
            "布局修订号": revision,
            "最近发布版本": label,
            "版本状态": "已发布",
            "是否展示": True,
        })
        if panel_item.get("name"):
            panel["面板名称"] = panel_item["name"]

        version_row: dict[str, Any] = {
            "id": _next_id(VERSION),
            "版本编号": version_key,
            "版本号": label,
            "dashboard_id": panel["id"],
            "面板编号": panel_no,
            "修订号": revision,
            "布局配置": layout,
            "面板顺序": ",".join(panel_item["panel_order"]),
            "图表集合": ",".join(chart_set),
            "可见范围": ",".join(panel_item["visible_teams"]),
            "图表快照": [_chart_snapshot(chart) for chart in chart_rows],
            "受影响图表": affected,
            "状态": "生效中",
            "是当前版本": True,
            "回滚自": "",
            "发布人": operator,
            "发布时间": _now(),
            "备注": remark or "",
        }
        for row in store.rows(VERSION):
            if int(row.get("dashboard_id", 0)) == panel["id"]:
                row["是当前版本"] = False
                if str(row.get("状态")) == "生效中":
                    row["状态"] = "历史版本"
        store.rows(VERSION).append(version_row)

        self._write_followups(
            panel=panel,
            version_row=version_row,
            affected_charts=affected,
            chart_rows=chart_rows,
            event="发布",
            operator=operator,
            detail="布局发布",
        )
        self._sync_review_states()
        return version_row

    # ------------------------------------------------------------------ 回滚
    def rollback(
        self,
        *,
        dashboard_id: int | None = None,
        version_id: int | None = None,
        target_version: str | None = None,
        scope: str = "all",
        operator: str = "值班管理员",
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if scope not in {"all", "layout", "charts"}:
            return {"ok": False, "message": f"不支持的回滚范围「{scope}」", "affected": []}
        if dashboard_id is None and version_id is None:
            return {"ok": False, "message": "回滚需要指定面板或布局版本", "affected": []}

        if dashboard_id is None:
            version = self.get_version(int(version_id))
            if version is None:
                return {"ok": False, "message": f"布局版本 {version_id} 不存在", "affected": []}
            dashboard_id = int(version.get("dashboard_id", 0))

        base = expected_revision if expected_revision is not None else self._current_revision(dashboard_id)

        with _PUBLISH_LOCK:
            panel = store.find(DASHBOARD, int(dashboard_id))
            if panel is None:
                return {"ok": False, "message": f"看板面板 {dashboard_id} 不存在或已归档", "affected": []}
            target = self.find_version(int(dashboard_id), label=target_version, version_id=version_id)
            if target is None:
                return {
                    "ok": False,
                    "message": "找不到要回滚到的布局版本，未做任何修改",
                    "affected": [],
                }
            current_revision = self._current_revision(int(dashboard_id))
            if base is not None and current_revision != int(base):
                return {
                    "ok": False,
                    "message": (
                        f"版本冲突：回滚基于第 {base} 次布局修订，"
                        f"服务端已变为第 {current_revision} 次，请刷新后重试"
                    ),
                    "affected": [],
                    "conflict": {
                        "dashboard_id": int(dashboard_id),
                        "expected_revision": base,
                        "current_revision": current_revision,
                    },
                }

            plan = self._rollback_plan(panel, target, scope)
            if dry_run:
                return {
                    "ok": True,
                    "message": f"回滚预检完成，将定向恢复 {len(plan['affected'])} 张图表",
                    "affected": plan["affected"],
                    "plan": plan,
                    "dry_run": True,
                }

            snapshot = store.snapshot(TRANSACTION_MODULES)
            try:
                result = self._apply_rollback(panel, target, plan, operator=operator, scope=scope)
            except Exception as exc:  # noqa: BLE001 - 回滚失败必须回到原状态
                store.restore(snapshot)
                return {
                    "ok": False,
                    "message": f"回滚失败，已回到回滚前状态：{exc}",
                    "affected": [],
                    "rolled_back": False,
                }
            self._sync_review_states()
            return {
                "ok": True,
                "message": (
                    f"已回滚到 {target.get('版本编号')}，定向恢复 {len(result['affected'])} 张图表，"
                    "之后的无关改动保持不变"
                ),
                "affected": result["affected"],
                "preserved": result["preserved"],
                "target_version": target.get("版本编号"),
                "versions": result["versions"],
                "rolled_back": True,
            }

    def _rollback_plan(
        self,
        panel: dict[str, Any],
        target: dict[str, Any],
        scope: str,
    ) -> dict[str, Any]:
        panel_no = _panel_no(panel)
        current_set = _split_refs(panel.get("图表集合"))
        target_set = _split_refs(target.get("图表集合"))
        snapshot_by_no = {
            _chart_no(chart): chart
            for chart in target.get("图表快照", [])
            if _chart_no(chart)
        }

        restored: list[str] = []
        preserved: list[str] = []
        recreate: list[dict[str, Any]] = []

        for chart_no in target_set:
            if scope == "layout":
                # 只回滚布局字段时不动图表集合。
                continue
            current_chart = next(
                (row for row in store.rows(CHART) if _chart_no(row) == chart_no),
                None,
            )
            if current_chart is None:
                snap = snapshot_by_no.get(chart_no)
                if snap is not None:
                    recreate.append(copy.deepcopy(snap))
                    restored.append(chart_no)
                continue
            if _chart_no(current_chart) not in current_set or bool(current_chart.get("是否归档")):
                restored.append(chart_no)
            else:
                # 图表还在面板里：保留它后来手工调整的筛选条件等配置。
                preserved.append(chart_no)

        later_added = [chart_no for chart_no in current_set if chart_no not in target_set]
        return {
            "panel_no": panel_no,
            "current_set": current_set,
            "target_set": target_set,
            "affected": restored,
            "preserved": preserved,
            "later_added": later_added,
            "recreate": recreate,
            "target": target,
        }

    def _apply_rollback(
        self,
        panel: dict[str, Any],
        target: dict[str, Any],
        plan: dict[str, Any],
        *,
        operator: str,
        scope: str,
    ) -> dict[str, Any]:
        panel_no = _panel_no(panel)
        target_key = str(target.get("版本编号"))
        target_label = str(target.get("版本号"))
        current_set = plan["current_set"]
        target_set = plan["target_set"]
        affected: list[str] = []

        if scope in {"all", "charts"}:
            snapshot_by_no = {
                _chart_no(chart): chart
                for chart in target.get("图表快照", [])
                if _chart_no(chart)
            }
            for chart_no in target_set:
                current_chart = next(
                    (row for row in store.rows(CHART) if _chart_no(row) == chart_no),
                    None,
                )
                if current_chart is None:
                    snap = snapshot_by_no.get(chart_no)
                    if snap is None:
                        continue
                    new_row = copy.deepcopy(snap)
                    new_row["id"] = _next_id(CHART)
                    new_row["是否归档"] = False
                    new_row["所属面板"] = panel_no
                    new_row["版本标记"] = target_key
                    new_row["最近刷新版本"] = ""
                    store.rows(CHART).append(new_row)
                    affected.append(chart_no)
                    continue
                restore_membership = _chart_no(current_chart) not in current_set or bool(
                    current_chart.get("是否归档")
                )
                if restore_membership:
                    current_chart["是否归档"] = False
                    current_chart["所属面板"] = panel_no
                    affected.append(chart_no)
                # 无论是否恢复成员关系，都只更新版本标记；配置字段保持现状，
                # 这样之后新增的筛选条件不会被目标版本的旧快照覆盖。
                current_chart["版本标记"] = target_key

        resulting_set = list(current_set)
        if scope in {"all", "charts"}:
            for chart_no in target_set:
                if chart_no not in resulting_set:
                    # 同时保留目标版本之后新增的图表，不因为回滚而消失。
                    resulting_set.append(chart_no)
            for chart_no in plan["later_added"]:
                if chart_no not in resulting_set:
                    resulting_set.append(chart_no)

        revision = int(panel.get("布局修订号", 0) or 0) + 1
        panel["布局修订号"] = revision

        if scope in {"all", "layout"}:
            panel["布局配置"] = target.get("布局配置")
            panel["面板顺序"] = target.get("面板顺序")
            panel["可见范围"] = target.get("可见范围")
        if scope in {"all", "charts"}:
            panel["图表集合"] = ",".join(resulting_set)
            if not panel.get("面板顺序"):
                panel["面板顺序"] = target.get("面板顺序")
        panel["最近发布版本"] = target_label
        panel["版本状态"] = "已回滚"
        panel["是否展示"] = True

        # 版本状态：目标版本重新成为当前版本，中间版本标记为已被回滚。
        for row in store.rows(VERSION):
            if int(row.get("dashboard_id", 0)) != panel["id"]:
                continue
            if str(row.get("版本编号")) == target_key:
                row["是当前版本"] = True
                row["状态"] = "回滚生效"
            else:
                row["是当前版本"] = False
                if str(row.get("状态")) in {"生效中", "回滚生效"}:
                    row["状态"] = "已被回滚"
                elif str(row.get("状态")) == "历史版本":
                    row["状态"] = "已被回滚"

        self._write_followups(
            panel=panel,
            version_row=target,
            affected_charts=affected,
            chart_rows=[chart for chart in _panel_chart_rows(panel)],
            event="回滚",
            operator=operator,
            detail="布局回滚",
        )

        return {"affected": affected, "preserved": plan["preserved"], "versions": self.list_versions(dashboard_id=panel["id"], page=1, size=1000)[0]}

    # --------------------------------------------------------- 联动台账/复核
    def _write_followups(
        self,
        *,
        panel: dict[str, Any],
        version_row: dict[str, Any],
        affected_charts: list[str],
        chart_rows: list[dict[str, Any]],
        event: str,
        operator: str,
        detail: str,
    ) -> None:
        version_key = str(version_row.get("版本编号"))
        panel_no = _panel_no(panel)
        dashboard_id = int(panel.get("id", 0))
        event_time = _now()

        # 台账按「本次版本涉及的图表」逐条落账：集合变化、新增图表、回滚恢复的图表
        # 都落一条；集合没变化时，仍为本次发布涉及的每张图留下版本条目，便于定位。
        audit_refs = list(affected_charts) or [
            _chart_no(chart) for chart in chart_rows
        ]
        for chart_no in audit_refs:
            chart = next(
                (row for row in store.rows(CHART) if _chart_no(row) == chart_no),
                None,
            )
            is_affected = chart_no in affected_charts
            store.rows(AUDIT).append({
                "id": _next_id(AUDIT),
                "台账编号": f"LEDG-{dashboard_id:04d}-{len(store.rows(AUDIT)) + 1:04d}",
                "dashboard_id": dashboard_id,
                "面板编号": panel_no,
                "图表编号": chart_no,
                "图表名称": (chart or {}).get("图表名称", ""),
                "关联版本": version_key,
                "版本标记": version_key,
                "事件类型": event,
                "变更说明": detail,
                "是否受影响": is_affected,
                "操作人": operator,
                "操作时间": event_time,
                "status": "已记录",
                "pending": False,
                "abnormal": False,
                "复核状态": "待复核" if event == "发布" else "已复核",
            })
            store.rows(REVIEW).append({
                "id": _next_id(REVIEW),
                "复核编号": f"REVW-{dashboard_id:04d}-{len(store.rows(REVIEW)) + 1:04d}",
                "dashboard_id": dashboard_id,
                "面板编号": panel_no,
                "图表编号": chart_no,
                "关联版本": version_key,
                "版本标记": version_key,
                "复核状态": "待复核" if event == "发布" else "已复核",
                "布局版本状态": "已回滚" if event == "回滚" else "已发布",
                "复核人": "" if event == "发布" else operator,
                "复核时间": "" if event == "发布" else event_time,
                "说明": detail,
                "status": "待复核" if event == "发布" else "已复核",
                "pending": event == "发布",
                "abnormal": False,
            })

        self._sync_export_task(
            panel=panel,
            version_key=version_key,
            chart_rows=chart_rows if event == "发布" else _panel_chart_rows(panel),
            operator=operator,
        )

    def _sync_export_task(
        self,
        *,
        panel: dict[str, Any],
        version_key: str,
        chart_rows: list[dict[str, Any]],
        operator: str,
    ) -> None:
        """导出任务清单跟着布局版本走：同一面板的活跃任务更新为当前版本快照。"""
        panel_no = _panel_no(panel)
        row = next(
            (item for item in store.rows(EXPORT_TASK) if int(item.get("dashboard_id", 0)) == panel["id"]),
            None,
        )
        if row is None:
            row = {
                "id": _next_id(EXPORT_TASK),
                "任务编号": f"EXPT-{panel['id']:04d}",
                "dashboard_id": panel["id"],
                "面板编号": panel_no,
                "面板名称": panel.get("面板名称"),
                "任务名称": f"{panel.get('面板名称')} 导出任务",
                "导出格式": "PDF",
                "任务状态": "待导出",
                "创建人": operator,
                "创建时间": _now(),
                "status": "待导出",
                "pending": True,
                "abnormal": False,
            }
            store.rows(EXPORT_TASK).append(row)
        row.update({
            "面板名称": panel.get("面板名称"),
            "关联版本": version_key,
            "快照面板版本": version_key,
            "版本标记": version_key,
            "图表集合": ",".join(_chart_no(chart) for chart in chart_rows),
            "导出快照": [
                {
                    "图表编号": _chart_no(chart),
                    "图表名称": chart.get("图表名称"),
                    "图表类型": chart.get("图表类型"),
                    "筛选条件": chart.get("筛选条件"),
                    "版本标记": chart.get("版本标记") or version_key,
                }
                for chart in chart_rows
            ],
            "更新时间": _now(),
            "更新人": operator,
        })

    def _sync_review_states(self) -> None:
        """复核状态与布局版本保持一致：版本被回滚后，对应复核记录同步调整。"""
        status_by_key = {
            str(row.get("版本编号")): str(row.get("状态"))
            for row in store.rows(VERSION)
        }
        for review in store.rows(REVIEW):
            version_key = str(review.get("关联版本") or "")
            version_state = status_by_key.get(version_key)
            if version_state is None:
                continue
            review["布局版本状态"] = version_state
            if version_state in {"已被回滚", "历史版本"}:
                review["复核状态"] = "已复核"
                review["复核人"] = review.get("复核人") or "值班管理员"
                review["复核时间"] = review.get("复核时间") or _now()
                review["status"] = "已复核"
                review["pending"] = False

    # ------------------------------------------------------------------ 刷新
    def refresh(
        self,
        dashboard_id: int,
        *,
        charts: Iterable[Any] | None = None,
        all_charts: bool = False,
        operator: str = "值班管理员",
    ) -> dict[str, Any] | None:
        """定向刷新：默认只刷新版本标记落后于当前布局版本的图表。"""
        panel = store.find(DASHBOARD, dashboard_id)
        if panel is None:
            return None
        panel_no = _panel_no(panel)
        member_rows = _panel_chart_rows(panel)
        if charts:
            refs = {str(ref).strip() for ref in charts if str(ref).strip()}
            selected = [
                chart for chart in member_rows
                if _chart_no(chart) in refs or str(chart.get("id")) in refs
            ]
        elif all_charts:
            selected = member_rows
        else:
            selected = [
                chart for chart in member_rows
                if str(chart.get("最近刷新版本") or "") != str(chart.get("版本标记") or "")
            ]
        refreshed = []
        for chart in selected:
            chart["最近刷新版本"] = str(chart.get("版本标记") or "")
            refreshed.append(_chart_no(chart))
        versions, _ = self.list_versions(dashboard_id=dashboard_id, page=1, size=1000)
        current = next((row for row in versions if row.get("是当前版本")), None)
        return {
            "ok": True,
            "message": f"已定向刷新 {len(refreshed)} 张图表，未触及无版本变化的图表",
            "refreshed": refreshed,
            "skipped": [
                _chart_no(chart) for chart in member_rows if _chart_no(chart) not in refreshed
            ],
            "当前版本": current.get("版本编号") if current else "",
            "操作人": operator,
        }

    def publish_single(
        self,
        dashboard_id: int,
        *,
        charts: Iterable[Any] | None = None,
        layout: str | None = None,
        panel_order: Iterable[Any] | None = None,
        visible_teams: Iterable[str] | None = None,
        expected_revision: int | None = None,
        operator: str = "值班管理员",
        remark: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "dashboard_id": dashboard_id,
            "charts": list(charts or []),
            "layout": layout,
            "panel_order": list(panel_order) if panel_order is not None else None,
            "visible_teams": list(visible_teams) if visible_teams is not None else None,
            "expected_revision": expected_revision,
        }
        return self.publish([item], operator=operator, remark=remark)


def _bootstrap_baseline_versions() -> None:
    """种子数据里的面板可能已有『最近发布版本』但没有版本行，先补一条基线记录。"""
    existing = {
        int(row.get("dashboard_id", 0)) for row in store.rows(VERSION)
    }
    for panel in store.rows(DASHBOARD):
        dashboard_id = int(panel.get("id", 0))
        if dashboard_id in existing:
            continue
        label = str(panel.get("最近发布版本") or "")
        revision = int(panel.get("布局修订号", 0) or 0)
        if not label or revision <= 0:
            continue
        panel_no = _panel_no(panel)
        charts = _panel_chart_rows(panel)
        store.rows(VERSION).append({
            "id": _next_id(VERSION),
            "版本编号": _version_key(panel_no, label),
            "版本号": label,
            "dashboard_id": dashboard_id,
            "面板编号": panel_no,
            "修订号": revision,
            "布局配置": panel.get("布局配置"),
            "面板顺序": panel.get("面板顺序"),
            "图表集合": panel.get("图表集合"),
            "可见范围": panel.get("可见范围"),
            "图表快照": [_chart_snapshot(chart) for chart in charts],
            "受影响图表": [],
            "状态": "生效中",
            "是当前版本": True,
            "回滚自": "",
            "发布人": panel.get("创建人") or "值班管理员",
            "发布时间": "基线版本",
            "备注": "由种子数据补齐的基线版本",
        })


_bootstrap_baseline_versions()

layout_service = LayoutService()
