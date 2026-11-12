"""布局版本接口：发布、回滚、版本链、刷新链路与联动台账查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import EntryPayload, LayoutPublishPayload, LayoutRollbackPayload
from app.services.layout import layout_service

router = APIRouter(prefix="/api/layout", tags=["看板布局版本"])


@router.get("/versions")
def list_versions(
    dashboard_id: int | None = Query(default=None, description="只看某个看板面板的版本"),
    keyword: str | None = Query(default=None, description="按版本编号或面板编号检索"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """布局版本列表；没有版本时返回空页。"""
    items, total = layout_service.list_versions(
        dashboard_id=dashboard_id, keyword=keyword, page=page, size=size
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/versions/{version_id}")
def get_version(version_id: int) -> dict[str, Any]:
    """读取单个布局版本，包含图表配置快照。"""
    version = layout_service.get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"布局版本 {version_id} 不存在")
    return version


@router.post("/publish")
def publish(payload: LayoutPublishPayload) -> dict[str, Any]:
    """整组发布：一次提交多个面板，全部成功或全部保持原状态。"""
    return layout_service.publish(
        payload.items, operator=payload.operator, remark=payload.remark
    )


@router.post("/rollback")
def rollback(payload: LayoutRollbackPayload) -> dict[str, Any]:
    """定向回滚到指定布局版本；只恢复目标版本里受影响的图表，保留之后的无关改动。"""
    values = payload.model_dump()
    return layout_service.rollback(
        dashboard_id=values.get("dashboard_id"),
        version_id=values.get("version_id"),
        target_version=values.get("target_version"),
        scope=payload.scope,
        operator=payload.operator,
        expected_revision=payload.expected_revision,
        dry_run=payload.dry_run,
    )


@router.get("/ledger")
def ledger(
    version_key: str | None = Query(default=None, alias="version", description="按版本编号过滤，如 DASH-0001@V1"),
    dashboard_id: int | None = Query(default=None, description="按面板过滤"),
    page: int = 1,
    size: int = 100,
) -> dict[str, Any]:
    """审计台账：布局发布/回滚产生的图表版本条目。"""
    items, total = layout_service.ledger(version_key=version_key, dashboard_id=dashboard_id, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/reviews")
def reviews(
    version_key: str | None = Query(default=None, alias="version", description="按版本编号过滤"),
    page: int = 1,
    size: int = 100,
) -> dict[str, Any]:
    """复核记录：复核状态与布局版本状态保持一致。"""
    items, total = layout_service.reviews(version_key=version_key, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/export-tasks")
def export_tasks(
    dashboard_id: int | None = Query(default=None, description="按面板过滤"),
    page: int = 1,
    size: int = 100,
) -> dict[str, Any]:
    """导出任务清单：导出快照与当前面板版本同步。"""
    items, total = layout_service.export_tasks(dashboard_id=dashboard_id, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/trace/{dashboard_id}")
def trace(dashboard_id: int) -> dict[str, Any]:
    """排查链路：面板发布 → 版本 → 图表刷新 → 台账/复核/导出任务。"""
    payload = layout_service.trace(dashboard_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"看板面板 {dashboard_id} 不存在或已归档")
    return payload


@router.post("/refresh/{dashboard_id}")
def refresh(dashboard_id: int, payload: EntryPayload | None = None) -> dict[str, Any]:
    """定向刷新：没传图表时只刷新版本标记落后的图表；all=true 才整板重算。"""
    values = payload.values if payload else {}
    charts = values.get("charts") or None
    mode_all = bool(values.get("all"))
    result = layout_service.refresh(
        dashboard_id,
        charts=charts,
        all_charts=mode_all,
        operator=str(values.get("operator") or "值班管理员"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"看板面板 {dashboard_id} 不存在或已归档")
    return result
