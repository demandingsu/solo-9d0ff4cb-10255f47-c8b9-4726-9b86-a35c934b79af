"""端到端：同步接口、链查询、冲突、撤销与失败保留。"""
from __future__ import annotations


def _chains_by_id(payload: dict) -> dict:
    return {c["chain_id"]: c for c in payload["chains"]}


def _orphans_by_no(payload: dict) -> dict:
    return {o["notice_no"]: o for o in payload["orphans"]}


def test_full_sync_and_counts(client, site_base_url):
    c, _ = client
    resp = c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["total_listed"] == 17
    assert body["fetched_ok"] == 17
    assert body["failed_count"] == 0

    result = c.get("/api/v1/chains").json()
    # 6 条链：A、B、C、G-001、G-101、H
    assert len(result["chains"]) == 6
    # 5 条孤立异常：D-001、E-001、E-002、F-001
    assert len(result["orphans"]) == 4


def test_normal_chain_status_and_effective_deadline(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["A-001"]

    assert chain["status"] == "ok"
    # A-002 未给出截止时间 -> 沿用链上此前的截止时间
    # A-003 给出更晚的截止时间 -> 有效截止时间取最新
    assert chain["effective_deadline"] == "2026-11-25 15:00:00"
    assert [a["notice_no"] for a in chain["announcements"]] == [
        "A-001", "A-002", "A-003"
    ]
    # 链上公告都带有来源链接
    assert all(a["source_url"].endswith(f"/notice/{a['notice_no']}")
               for a in chain["announcements"])


def test_revocation_invalidates_chain(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["B-001"]

    assert chain["status"] == "cancelled"
    assert chain["effective_deadline"] is None
    assert [a["notice_no"] for a in chain["announcements"]] == [
        "B-001", "B-002", "B-003"
    ]


def test_parallel_corrections_conflict_without_guessing_deadline(
    client, site_base_url
):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["C-001"]

    assert chain["status"] == "conflict"
    # 冲突时不推测截止时间
    assert chain["effective_deadline"] is None
    for ann in chain["announcements"]:
        assert "parallel_correction" in ann["conflict_reasons"]
        assert ann["effective_deadline"] is None


def test_same_project_first_notices_remain_independent(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chains = _chains_by_id(c.get("/api/v1/chains").json())

    g1 = chains["G-001"]
    g2 = chains["G-101"]
    assert g1["project_no"] == g2["project_no"] == "PRJ-G"
    assert [a["notice_no"] for a in g1["announcements"]] == ["G-001"]
    assert [a["notice_no"] for a in g2["announcements"]] == ["G-101", "G-102"]
    assert g1["status"] == g2["status"] == "ok"
    assert g2["effective_deadline"] == "2026-11-28 09:30:00"


def test_orphans_missing_reference_and_cycle(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    orphans = _orphans_by_no(c.get("/api/v1/chains").json())

    assert "missing_reference" in orphans["D-001"]["conflict_reasons"]
    assert "missing_reference" in orphans["F-001"]["conflict_reasons"]

    for no in ("E-001", "E-002"):
        assert "reference_cycle" in orphans[no]["conflict_reasons"]
        assert orphans[no]["effective_deadline"] is None
        assert orphans[no]["status"] == "orphan"


def test_detail_failure_keeps_last_complete_result(
    client, site_base_url, set_fail
):
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    # 第一次同步：全部成功
    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"
    before = c.get("/api/v1/announcements/H-001").json()
    assert before["deadline"] == "2026-11-30 09:30:00"
    assert before["last_fetch_error"] is None

    # 打开详情页故障后再次同步
    set_fail(True)
    second = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert second["status"] == "partial"
    assert second["failed_count"] == 1
    assert second["kept_last"] == 1

    after = c.get("/api/v1/announcements/H-001").json()
    # 上次完整结果保留
    assert after["deadline"] == "2026-11-30 09:30:00"
    assert after["notice_type"] == "first"
    assert "500" in after["last_fetch_error"]
    # 链 H 仍可正常计算
    chains = _chains_by_id(c.get("/api/v1/chains").json())
    assert chains["H-001"]["status"] == "ok"

    # 故障恢复后同步，错误信息清空
    set_fail(False)
    third = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert third["status"] == "success"
    healed = c.get("/api/v1/announcements/H-001").json()
    assert healed["last_fetch_error"] is None


def test_sync_runs_recorded_and_health(client, site_base_url):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}

    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    runs = c.get("/api/v1/sync-runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["total_listed"] == 17


def test_sync_bad_url_rejected(client):
    c, _ = client
    resp = c.post("/api/v1/sync", json={"list_url": "not-a-url"})
    assert resp.status_code == 400


def test_long_chain_catalog_syncs_and_returns_deadline(client, site_base_url):
    """按新到旧排列的 1100 条连续引用目录可正常同步，返回有效截止时间。"""
    c, _ = client
    resp = c.post(
        "/api/v1/sync",
        json={"list_url": f"{site_base_url}/announcements/long-chain"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["total_listed"] == 1100
    assert body["fetched_ok"] == 1100

    result = c.get("/api/v1/chains").json()
    assert len(result["chains"]) == 1
    assert result["orphans"] == []
    chain = result["chains"][0]
    assert chain["chain_id"] == "L-0001"
    assert chain["status"] == "ok"
    assert chain["effective_deadline"] == "2027-01-15 15:00:00"
    assert len(chain["announcements"]) == 1100
    # 列表按新到旧，但链内成员仍按引用关系完整收录
    assert {a["notice_no"] for a in chain["announcements"]} == {
        f"L-{i:04d}" for i in range(1, 1101)
    }


def test_cycle_catalog_shows_conflict_reasons(client, site_base_url):
    """成环目录不报错，全部按既有规则显示 reference_cycle 冲突原因。"""
    c, _ = client
    resp = c.post(
        "/api/v1/sync",
        json={"list_url": f"{site_base_url}/announcements/cycle"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    result = c.get("/api/v1/chains").json()
    assert result["chains"] == []
    assert len(result["orphans"]) == 1100
    for orphan in result["orphans"]:
        assert orphan["status"] == "orphan"
        assert "reference_cycle" in orphan["conflict_reasons"]
        assert orphan["effective_deadline"] is None


def test_merge_failure_rolls_back_details_and_records_failure(
    client, site_base_url, monkeypatch
):
    """归并失败：撤销本轮详情改动、保留旧链、记录失败；故障排除后重试恢复。"""
    c, sample = client
    list_url = f"{site_base_url}/announcements"

    # 第一次同步：正常落库
    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"
    before = c.get("/api/v1/chains").json()
    before_h = c.get("/api/v1/announcements/H-001").json()
    assert before_h["deadline"] == "2026-11-30 09:30:00"

    # 假设站点本轮详情已变化：归并失败时这一改动不得落库
    changed = dict(sample.NOTICE_BY_NO["H-001"])
    changed["deadline"] = "2026-12-31 23:59:59"
    monkeypatch.setitem(sample.NOTICE_BY_NO, "H-001", changed)

    import app.chains as chains_mod

    original_recompute = chains_mod.recompute

    def boom(_conn, *, commit=True):
        raise RuntimeError("模拟链归并故障")

    monkeypatch.setattr(chains_mod, "recompute", boom)

    second = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert second["status"] == "failed"
    assert "归并失败" in second["message"]
    # 本轮详情改动被撤销
    after_h = c.get("/api/v1/announcements/H-001").json()
    assert after_h["deadline"] == "2026-11-30 09:30:00"
    # 旧链结果保留
    assert c.get("/api/v1/chains").json() == before

    # 失败被记录，且不会停在 running
    runs = c.get("/api/v1/sync-runs").json()["runs"]
    assert runs[0]["status"] == "failed"
    assert runs[0]["finished_at"] is not None
    assert "归并失败" in runs[0]["message"]

    # 故障排除后重试恢复（只还原归并函数，不影响夹具的 DB_PATH 隔离）
    monkeypatch.setattr(chains_mod, "recompute", original_recompute)
    third = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert third["status"] == "success"
    healed = c.get("/api/v1/announcements/H-001").json()
    assert healed["deadline"] == "2026-12-31 23:59:59"


def test_detail_failure_keeps_last_complete_result_within_merged_round(
    client, site_base_url, set_fail
):
    """回归：单条详情抓取失败仍沿用旧记录，不影响同轮其它公告落库。"""
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"

    set_fail(True)
    second = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert second["status"] == "partial"
    assert second["failed_count"] == 1
    assert second["kept_last"] == 1

    # H-001 沿用旧记录，同轮其它公告照常更新
    assert c.get("/api/v1/announcements/H-001").json()["deadline"] == (
        "2026-11-30 09:30:00"
    )
    assert c.get("/api/v1/announcements/A-001").json()["deadline"] == (
        "2026-11-20 09:30:00"
    )
    set_fail(False)


def test_sync_list_page_unreachable(client):
    c, _ = client
    # 监听一个保证无人监听的端口
    resp = c.post(
        "/api/v1/sync",
        json={"list_url": "http://127.0.0.1:1/announcements"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["total_listed"] == 0


def test_sync_list_page_unreachable(client):
    c, _ = client
    # 监听一个保证无人监听的端口
    resp = c.post(
        "/api/v1/sync",
        json={"list_url": "http://127.0.0.1:1/announcements"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["total_listed"] == 0
