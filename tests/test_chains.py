"""链归并与冲突识别的纯单元测试（不经过 HTTP）。"""
from __future__ import annotations

import sqlite3

from app import chains, db


def _conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chains.db"))
    from app import config
    config.DB_PATH = tmp_path / "chains.db"
    conn = db.connect()
    db.init_db(conn)
    return conn


def _insert(conn, no, ntype, ref=None, deadline=None, project="P"):
    conn.execute(
        "INSERT INTO announcements (notice_no, project_no, notice_type, "
        "deadline, ref_notice_no, last_synced_at) VALUES (?, ?, ?, ?, ?, ?)",
        (no, project, ntype, deadline, ref, "2026-11-01 00:00:00"),
    )
    conn.commit()


def _map(conn):
    return {
        r["notice_no"]: dict(r)
        for r in conn.execute("SELECT * FROM announcements")
    }


def test_self_loop_is_cycle_orphan(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "S-1", chains.CORRECTION, ref="S-1")
    chains.recompute(conn)
    row = _map(conn)["S-1"]
    assert row["chain_id"] is None
    assert row["chain_status"] == "orphan"
    assert "reference_cycle" in row["conflict_reasons"]


def test_first_notice_with_reference_flagged(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "R-1", chains.FIRST, ref="R-0", deadline="2026-11-20 09:30:00")
    chains.recompute(conn)
    row = _map(conn)["R-1"]
    # 引用的上级不存在，同时触发两个原因
    assert "first_notice_with_reference" in row["conflict_reasons"]
    assert "missing_reference" in row["conflict_reasons"]
    assert row["chain_status"] == "conflict"
    assert row["effective_deadline"] is None


def test_entry_after_revocation_becomes_orphan(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "V-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "V-2", chains.REVOCATION, ref="V-1")
    _insert(conn, "V-3", chains.CORRECTION, ref="V-2",
            deadline="2026-12-01 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)

    assert rows["V-1"]["chain_id"] == "V-1"
    assert rows["V-2"]["chain_id"] == "V-1"
    assert rows["V-1"]["chain_status"] == "cancelled"
    # 撤销之后挂接的公告不属于该链
    assert rows["V-3"]["chain_id"] is None
    assert "entry_after_revocation" in rows["V-3"]["conflict_reasons"]


def test_missing_deadline_on_correction_inherits(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "I-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "I-2", chains.CORRECTION, ref="I-1")  # 无截止时间
    chains.recompute(conn)
    row = _map(conn)["I-1"]
    assert row["chain_status"] == "ok"
    assert row["effective_deadline"] == "2026-11-20 09:30:00"


def test_missing_reference_marks_chain_conflict(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "M-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "M-2", chains.CORRECTION, ref="GHOST",
            deadline="2026-12-01 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)
    # M-2 无法挂接 -> 孤立；M-1 自身仍是正常链
    assert rows["M-2"]["chain_id"] is None
    assert "missing_reference" in rows["M-2"]["conflict_reasons"]
    assert rows["M-1"]["chain_status"] == "ok"
    assert rows["M-1"]["effective_deadline"] == "2026-11-20 09:30:00"


def test_long_reference_chain_beyond_recursion_limit(tmp_path, monkeypatch):
    """1100 条连续引用不触发 RecursionError，正常归并为一条长链。"""
    conn = _conn(tmp_path, monkeypatch)
    length = 1100
    for i in range(1, length + 1):
        _insert(
            conn, f"L-{i:04d}",
            chains.FIRST if i == 1 else chains.CORRECTION,
            ref=None if i == 1 else f"L-{i - 1:04d}",
            deadline="2027-01-15 15:00:00" if i == length else "",
        )

    chains.recompute(conn)
    rows = _map(conn)

    assert rows["L-0001"]["chain_id"] == "L-0001"
    assert rows["L-0001"]["chain_status"] == "ok"
    # 最新一条给出的截止时间被采纳，链上全部公告归入同一链
    assert rows["L-0001"]["effective_deadline"] == "2027-01-15 15:00:00"
    assert all(r["chain_id"] == "L-0001" for r in rows.values())
    # 链成员按引用关系完整收录，没有遗漏成孤立
    assert len(rows) == length


def test_long_reference_cycle_keeps_conflict_reason(tmp_path, monkeypatch):
    """1100 条公告构成的引用环全部按既有规则成为 reference_cycle 孤立。"""
    conn = _conn(tmp_path, monkeypatch)
    length = 1100
    for i in range(1, length + 1):
        _insert(conn, f"CY-{i:04d}", chains.CORRECTION,
                ref=f"CY-{(i - 2) % length + 1:04d}",
                deadline="2026-12-01 09:30:00")

    chains.recompute(conn)
    rows = _map(conn)

    for row in rows.values():
        assert row["chain_id"] is None
        assert row["chain_status"] == "orphan"
        assert "reference_cycle" in row["conflict_reasons"]
        assert row["effective_deadline"] is None
