"""布置版本场景回归：用真实 service 跑用户描述的关键链路。"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.services.chart import ChartService
from app.services.layout import layout_service
from app.store import store


def chart_row(no: str):
    for row in store.rows("chart"):
        if row.get("图表编号") == no:
            return row
    raise AssertionError(no)


def panel_row(panel_no: str):
    for row in store.rows("dashboard"):
        if row.get("面板编号") == panel_no:
            return row
    raise AssertionError(panel_no)


def test_publish_snapshot_and_targeted_rollback():
    panel = panel_row("DASH-0001")
    dashboard_id = panel["id"]
    # 第 1 次发布：两张图 + 可见团队
    result = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0001", "CHAR-0002"],
        "layout": "纵向两栏 · 趋势图在上",
        "visible_teams": ["销售部", "市场部"],
    }], operator="王敏")
    assert result["ok"], result
    v1 = result["versions"][0]["版本编号"]
    assert v1.startswith("DASH-0001@")
    assert panel["可见范围"] == "销售部,市场部"

    # 第 2 次发布：删掉右侧图 CHAR-0002，新增筛选条件调整
    result = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0001"],
        "layout": "单栏 · 趋势图置顶",
        "visible_teams": ["销售部"],
    }], operator="李强")
    assert result["ok"], result
    assert chart_row("CHAR-0002")["是否归档"] is True

    # 图表后来手工调整筛选条件
    chart_service = ChartService()
    _, message = chart_service.update_config(1, {"筛选条件": "region=华东+华南"})
    assert "筛选条件" in message

    # 回滚到上一版：只恢复被删的那张图
    result = layout_service.rollback(
        dashboard_id=dashboard_id, target_version=v1.split("@")[1], scope="all", operator="值班管理员"
    )
    assert result["ok"], result
    assert result["affected"] == ["CHAR-0002"], result
    assert chart_row("CHAR-0002")["是否归档"] is False
    assert "CHAR-0002" in panel["图表集合"].split(",")
    # 后续手工调整的筛选条件不被旧快照覆盖
    assert chart_row("CHAR-0001")["筛选条件"] == "region=华东+华南"


def test_conflict_only_one_version():
    panel = panel_row("DASH-0002")
    dashboard_id = panel["id"]
    base = panel["布局修订号"]
    first = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0003"],
        "visible_teams": ["供应链部"],
        "expected_revision": base,
    }], operator="编辑A")
    assert first["ok"], first
    second = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0003"],
        "visible_teams": ["仓储部"],
        "expected_revision": base,
    }], operator="编辑B")
    assert not second["ok"]
    assert "版本冲突" in second["message"]
    assert second["conflicts"][0]["current_revision"] == base + 1
    # 只有一个成功版本落库
    versions, _ = layout_service.list_versions(dashboard_id=dashboard_id, page=1, size=10)
    assert len(versions) == 2, len(versions)


def test_batch_atomic():
    panel1 = panel_row("DASH-0001")
    panel2 = panel_row("DASH-0002")
    before = (panel1["布局配置"], panel2["布局配置"], len(store.rows("layout_version")))
    bad = layout_service.publish([
        {"dashboard_id": panel1["id"], "charts": ["CHAR-0001"], "layout": "批量A"},
        {"dashboard_id": panel2["id"], "charts": ["CHAR-9999"], "layout": "批量B"},
    ], operator="值班管理员")
    assert not bad["ok"]
    after = (panel1["布局配置"], panel2["布局配置"], len(store.rows("layout_version")))
    assert before == after, (before, after)
    assert bad["conflicts"] and bad["conflicts"][0]["面板编号"] == "DASH-0002"

    good = layout_service.publish([
        {"dashboard_id": panel1["id"], "charts": ["CHAR-0001", "CHAR-0002"], "layout": "批量A"},
        {"dashboard_id": panel2["id"], "charts": ["CHAR-0003"], "layout": "批量B"},
    ], operator="值班管理员")
    assert good["ok"], good
    assert len(good["versions"]) == 2


def test_followups_and_trace():
    target = layout_service.publish([{
        "dashboard_id": panel_row("DASH-0001")["id"],
        "charts": ["CHAR-0001", "CHAR-0002"],
        "layout": "审计测试",
    }], operator="值班管理员")
    version_key = target["versions"][0]["版本编号"]
    ledger, total = layout_service.ledger(version_key=version_key)
    assert total == 2
    assert {row["图表编号"] for row in ledger} == {"CHAR-0001", "CHAR-0002"}
    assert all(row["关联版本"] == version_key for row in ledger)

    reviews, _ = layout_service.reviews(version_key=version_key)
    assert len(reviews) == 2
    assert all(row["版本标记"] == version_key for row in reviews)

    trace = layout_service.trace(panel_row("DASH-0001")["id"])
    assert trace["当前版本"] == version_key
    assert trace["待刷新图表"] == ["CHAR-0001", "CHAR-0002"]

    refreshed = layout_service.refresh(panel_row("DASH-0001")["id"], operator="值班管理员")
    assert set(refreshed["refreshed"]) == {"CHAR-0001", "CHAR-0002"}
    assert refreshed["skipped"] == []
    trace_after = layout_service.trace(panel_row("DASH-0001")["id"])
    assert trace_after["待刷新图表"] == []

    tasks, _ = layout_service.export_tasks(dashboard_id=panel_row("DASH-0001")["id"])
    current = next(row for row in tasks if row["快照面板版本"] == version_key)
    assert current["导出快照"][0]["版本标记"] == version_key


def test_rollback_syncs_reviews_and_tasks():
    panel = panel_row("DASH-0002")
    dashboard_id = panel["id"]
    first = layout_service.publish([{
        "dashboard_id": dashboard_id, "charts": ["CHAR-0003"], "layout": "回滚前",
    }], operator="值班管理员")
    v0 = first["versions"][0]["版本编号"]
    second = layout_service.publish([{
        "dashboard_id": dashboard_id, "charts": ["CHAR-0003"], "layout": "回滚后",
    }], operator="值班管理员")
    v1 = second["versions"][0]["版本编号"]

    rollback = layout_service.rollback(
        dashboard_id=dashboard_id, target_version=v0.split("@")[1], scope="layout"
    )
    assert rollback["ok"], rollback
    reviews, _ = layout_service.reviews(version_key=v1)
    assert all(row["复核状态"] == "已复核" for row in reviews)
    moved = [row for row in reviews if row["布局版本状态"] == "已被回滚"]
    assert moved

    tasks, _ = layout_service.export_tasks(dashboard_id=dashboard_id)
    assert tasks[0]["快照面板版本"] == v0



def test_rollback_keeps_later_added_chart_and_filter():
    panel = panel_row("DASH-0003")
    dashboard_id = panel["id"]
    first = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0001"],
        "layout": "V1 布局",
        "visible_teams": ["产品部"],
    }], operator="周宁")
    assert first["ok"], first
    target = first["versions"][0]["版本编号"].split("@")[1]

    second = layout_service.publish([{
        "dashboard_id": dashboard_id,
        "charts": ["CHAR-0001", "CHAR-0002"],
        "layout": "V2 布局",
        "visible_teams": ["产品部", "运营部"],
    }], operator="周宁")
    assert second["ok"], second
    ChartService().update_config(2, {"筛选条件": "channel=小程序"})

    rollback = layout_service.rollback(
        dashboard_id=dashboard_id, target_version=target, scope="charts"
    )
    assert rollback["ok"], rollback
    assert "CHAR-0002" in panel["图表集合"].split(","), "目标版本之后新增的图表应保留"
    assert chart_row("CHAR-0002")["筛选条件"] == "channel=小程序", "后续筛选条件不能被覆盖"
    assert chart_row("CHAR-0001")["版本标记"] == f"{panel['面板编号']}@{target}"
    # 只回滚 charts 时可见范围保持回滚前的值
    assert panel["可见范围"] == "产品部,运营部"


def test_rollback_failure_restores_previous_state():
    panel = panel_row("DASH-0001")
    dashboard_id = panel["id"]
    layout_service.publish([{
        "dashboard_id": dashboard_id, "charts": ["CHAR-0001", "CHAR-0002"], "layout": "失败测试",
    }], operator="值班管理员")
    before = (
        panel["布局配置"],
        panel["图表集合"],
        panel["布局修订号"],
        chart_row("CHAR-0002")["是否归档"],
        len(store.rows("layout_version")),
    )
    import app.services.layout as layout_module

    original = layout_module.LayoutService._apply_rollback

    def boom(*args, **kwargs):
        raise RuntimeError("模拟回滚时写库失败")

    layout_module.LayoutService._apply_rollback = boom
    try:
        result = layout_service.rollback(dashboard_id=dashboard_id, target_version="V1", scope="all")
    finally:
        layout_module.LayoutService._apply_rollback = original
    assert not result["ok"]
    assert "回到回滚前状态" in result["message"]
    after = (
        panel["布局配置"],
        panel["图表集合"],
        panel["布局修订号"],
        chart_row("CHAR-0002")["是否归档"],
        len(store.rows("layout_version")),
    )
    assert before == after, (before, after)


def test_concurrent_publish_only_one_wins():
    import threading

    panel = panel_row("DASH-0001")
    dashboard_id = panel["id"]
    base = panel["布局修订号"]
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def submit(operator: str):
        barrier.wait()
        results.append(layout_service.publish([{
            "dashboard_id": dashboard_id,
            "charts": ["CHAR-0001", "CHAR-0002"],
            "layout": f"{operator} 的布局",
            "expected_revision": base,
        }], operator=operator))

    threads = [threading.Thread(target=submit, args=(name,)) for name in ("编辑甲", "编辑乙")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [item for item in results if item["ok"]]
    losers = [item for item in results if not item["ok"]]
    assert len(winners) == 1, results
    assert len(losers) == 1
    assert "版本冲突" in losers[0]["message"]
    assert not losers[0]["versions"]


if __name__ == "__main__":
    test_publish_snapshot_and_targeted_rollback()
    test_conflict_only_one_version()
    test_batch_atomic()
    test_followups_and_trace()
    test_rollback_syncs_reviews_and_tasks()
    test_rollback_keeps_later_added_chart_and_filter()
    test_rollback_failure_restores_previous_state()
    test_concurrent_publish_only_one_wins()
    print("all layout scenarios passed")
