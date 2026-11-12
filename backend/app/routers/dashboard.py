"""数据看板接口：维护看板面板，覆盖提交校验、启用配置、停用配置等动作，
并接入布局版本发布、定向回滚与发布链路排查。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, LayoutPublishPayload, LayoutRollbackPayload, PageResult
from app.services.dashboard import DashboardService
from app.services.layout import layout_service

router = APIRouter(prefix="/api/dashboard", tags=["数据看板"])

service = DashboardService()

LIST_FIELDS = ["面板编号", "面板名称", "布局配置", "图表集合", "刷新频率", "可见范围", "创建人", "是否展示"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按面板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按面板编号与状态过滤数据看板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据看板清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}


@router.get("/versions")
def list_versions(
    dashboard_id: int | None = Query(default=None),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """看板面板的布局版本列表。"""
    items, total = layout_service.list_versions(dashboard_id=dashboard_id, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.post("/publish")
def publish(payload: LayoutPublishPayload) -> dict[str, Any]:
    """整组发布布局：面板顺序、图表集合与可见团队一并落成版本快照。"""
    return layout_service.publish(payload.items, operator=payload.operator, remark=payload.remark)


@router.post("/rollback")
def rollback(payload: LayoutRollbackPayload) -> dict[str, Any]:
    """定向回滚布局版本；只恢复目标版本中受影响的图表，保留之后的无关改动。"""
    return layout_service.rollback(
        dashboard_id=payload.dashboard_id,
        version_id=payload.version_id,
        target_version=payload.target_version,
        scope=payload.scope,
        operator=payload.operator,
        expected_revision=payload.expected_revision,
        dry_run=payload.dry_run,
    )


@router.get("/{entry_id}/trace")
def trace(entry_id: int) -> dict[str, Any]:
    """发布到图表刷新链路排查：版本链、刷新状态、台账、复核与导出任务。"""
    payload = layout_service.trace(entry_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"看板面板 {entry_id} 不存在或已归档")
    return payload


@router.post("/{entry_id}/refresh")
def refresh_entry(entry_id: int, payload: EntryPayload | None = None) -> dict[str, Any]:
    """定向刷新看板图表；只重算版本标记落后的图表。"""
    values = payload.values if payload else {}
    result = layout_service.refresh(
        entry_id,
        charts=values.get("charts") or None,
        all_charts=bool(values.get("all")),
        operator=str(values.get("operator") or "值班管理员"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"看板面板 {entry_id} 不存在或已归档")
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条看板面板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板面板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条看板面板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="看板面板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条看板面板执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
