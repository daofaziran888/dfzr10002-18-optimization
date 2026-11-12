"""HTTP 端到端回归：直接调用 FastAPI 路由，验证用户描述的三条业务链路。

运行：cd backend && .venv/bin/python tests/test_layout_http.py
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/workspace/backend")


async def _call(app, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """最小 ASGI 调用器：避免引入 httpx/TestClient 依赖。"""
    payload = json.dumps(body or {}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    if "?" in path:
        scope["path"] = path.split("?", 1)[0]
    messages: list[dict] = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = 0
    chunks = b""
    for message in messages:
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks += message.get("body", b"")
    return status, json.loads(chunks or b"{}")


def run(app, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    return asyncio.run(_call(app, method, path, body))


def main() -> None:
    from app.main import app

    status, health = run(app, "GET", "/api/health")
    assert status == 200 and health["ok"]

    # 1. 发布基线 → 删除右侧图再发布 → 回滚只恢复那张图，且不覆盖后续筛选条件
    status, first = run(app, "POST", "/api/dashboard/publish", {
        "operator": "王敏",
        "items": [{
            "dashboard_id": 1,
            "charts": ["CHAR-0001", "CHAR-0002"],
            "layout": "纵向两栏",
            "visible_teams": ["销售部", "市场部"],
        }],
    })
    assert status == 200 and first["ok"], first
    v1 = first["versions"][0]["版本编号"].split("@")[1]

    status, second = run(app, "POST", "/api/dashboard/publish", {
        "operator": "李强",
        "items": [{
            "dashboard_id": 1,
            "charts": ["CHAR-0001"],
            "layout": "单栏",
            "visible_teams": ["销售部"],
        }],
    })
    assert status == 200 and second["ok"], second
    assert second["versions"][0]["受影响图表"] == ["CHAR-0002"]

    status, chart_update = run(app, "POST", "/api/chart/1/config", {
        "values": {"筛选条件": "region=华东+华南"},
    })
    assert status == 200 and chart_update["ok"]

    status, rollback = run(app, "POST", "/api/dashboard/rollback", {
        "dashboard_id": 1, "target_version": v1, "scope": "all",
    })
    assert status == 200 and rollback["ok"], rollback
    assert rollback["affected"] == ["CHAR-0002"]
    _, chart2 = run(app, "GET", "/api/chart/2")
    assert chart2["是否归档"] is False
    _, chart1 = run(app, "GET", "/api/chart/1")
    assert chart1["筛选条件"] == "region=华东+华南"

    # 2. 两个编辑者同基线提交：只有一个成功，另一个得到冲突说明；批量逐条列出冲突
    base = 2
    status, editor_a = run(app, "POST", "/api/layout/publish", {
        "items": [{
            "dashboard_id": 2, "charts": ["CHAR-0003"],
            "visible_teams": ["供应链部"], "expected_revision": base,
        }],
    })
    assert editor_a["ok"], editor_a
    status, editor_b = run(app, "POST", "/api/layout/publish", {
        "items": [{
            "dashboard_id": 2, "charts": ["CHAR-0003"],
            "visible_teams": ["仓储部"], "expected_revision": base,
        }],
    })
    assert status == 200 and not editor_b["ok"]
    assert "版本冲突" in editor_b["message"]
    assert editor_b["conflicts"][0]["current_revision"] == base + 1

    status, batch = run(app, "POST", "/api/layout/publish", {
        "items": [
            {"dashboard_id": 1, "charts": ["CHAR-0001"], "layout": "批量A"},
            {"dashboard_id": 2, "charts": ["CHAR-9999"], "layout": "批量B"},
        ],
    })
    assert status == 200 and not batch["ok"]
    assert batch["conflicts"][0]["面板编号"] == "DASH-0002"
    _, panel1 = run(app, "GET", "/api/dashboard/1")
    assert panel1["布局配置"] != "批量A", "整组失败时不允许部分落库"

    # 3. 发布 → 刷新链路排查：台账/复核/导出任务都带同一版本编号
    status, publish = run(app, "POST", "/api/layout/publish", {
        "items": [{"dashboard_id": 1, "charts": ["CHAR-0001", "CHAR-0002"], "layout": "审计链路"}],
    })
    assert publish["ok"]
    version_key = publish["versions"][0]["版本编号"]

    status, ledger = run(app, "GET", f"/api/layout/ledger?version={version_key}")
    assert ledger["total"] == 2
    assert all(item["关联版本"] == version_key for item in ledger["items"])

    status, reviews = run(app, "GET", f"/api/layout/reviews?version={version_key}")
    assert len(reviews["items"]) == 2
    assert all(item["版本标记"] == version_key for item in reviews["items"])

    status, trace = run(app, "GET", "/api/dashboard/1/trace")
    assert trace["当前版本"] == version_key
    assert set(trace["待刷新图表"]) == {"CHAR-0001", "CHAR-0002"}

    status, refresh = run(app, "POST", "/api/dashboard/1/refresh", {})
    assert refresh["ok"] and set(refresh["refreshed"]) == {"CHAR-0001", "CHAR-0002"}
    status, trace_after = run(app, "GET", "/api/dashboard/1/trace")
    assert trace_after["待刷新图表"] == []

    status, tasks = run(app, "GET", "/api/export/tasks?size=100")
    matching = [t for t in tasks["items"] if t["快照面板版本"] == version_key]
    assert matching and matching[0]["导出快照"][0]["版本标记"] == version_key

    # 4. 回滚后复核状态与版本状态一致
    status, second_publish = run(app, "POST", "/api/layout/publish", {
        "items": [{"dashboard_id": 1, "charts": ["CHAR-0001"], "layout": "回滚同步"}],
    })
    assert second_publish["ok"]
    newer = second_publish["versions"][0]["版本编号"]
    status, rolled = run(app, "POST", "/api/layout/rollback", {
        "dashboard_id": 1, "target_version": version_key.split("@")[1], "scope": "all",
    })
    assert rolled["ok"], rolled
    status, reviews_after = run(app, "GET", f"/api/layout/reviews?version={newer}")
    assert all(item["复核状态"] == "已复核" for item in reviews_after["items"])

    print("all layout http scenarios passed")


if __name__ == "__main__":
    main()
