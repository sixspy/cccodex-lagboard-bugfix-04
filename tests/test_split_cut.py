"""分裂切断验收测：等级牌（sources.level / arrivals.level）与输出态（sources.output_mode）必须同笔连坐。

本组用例先于实现落地（红），实现后全绿：
- 连坐失败测：任一侧写失败，两侧同撤，不留半截状态。
- 双路抢级失败测：值班与策略席同时改同一到达的等级，须占分裂锁，后交只记“等级分裂争用”。
- 五处同态：源目录徽标、到达卡片、策略附页、水位读数、审计流水。
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app as appmod

MODE_OF = {"正常": "可写", "告警": "降级只读", "违约": "冻结"}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "t.db")
    appmod.init_db()
    yield


def _client():
    return TestClient(appmod.app, raise_server_exceptions=False)


def _headers(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _source(c, h, name):
    items = c.get("/api/sources", headers=h).json()["items"]
    return next(s for s in items if s["name"] == name)


def _db_snapshot():
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.row_factory = sqlite3.Row
    srcs = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]
    n_arr = conn.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
    n_aud = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    conn.close()
    return srcs, n_arr, n_aud


def _assert_five_surfaces(c, h):
    """源目录徽标 / 到达卡片 / 策略附页 / 水位读数 / 审计流水 必须同态。"""
    srcs = c.get("/api/sources", headers=h).json()["items"]
    arrivals = c.get("/api/arrivals", headers=h).json()["items"]
    overview = c.get("/api/overview", headers=h).json()
    audit = c.get("/api/audit", headers=h).json()["items"]

    by_name = {s["name"]: s for s in srcs}

    # 1) 源目录徽标：等级牌与输出牌一一对应
    for s in srcs:
        assert s["output_mode"] == MODE_OF[s["level"]], s

    # 2) 到达卡片：每张卡片的当前等级/输出态与其源一致
    for a in arrivals:
        src = by_name[a["source"]]
        assert (a["current_level"], a["current_output_mode"]) == (src["level"], src["output_mode"]), a

    # 3) 策略附页：同一份源状态、同一套等级
    assert [g["name"] for g in overview["grading"]] == [s["name"] for s in srcs]
    for g in overview["grading"]:
        src = by_name[g["name"]]
        assert (g["level"], g["output_mode"]) == (src["level"], src["output_mode"])

    # 4) 水位读数：由同一快照计数
    expected = {
        "sources": len(srcs),
        "writable": sum(1 for s in srcs if s["output_mode"] == "可写"),
        "degraded": sum(1 for s in srcs if s["output_mode"] == "降级只读"),
        "frozen": sum(1 for s in srcs if s["output_mode"] == "冻结"),
        "normal": sum(1 for s in srcs if s["level"] == "正常"),
        "alert": sum(1 for s in srcs if s["level"] == "告警"),
        "breach": sum(1 for s in srcs if s["level"] == "违约"),
    }
    assert overview["water"] == expected

    # 5) 审计流水：每个源最后一条流水记录的状态等于当前状态
    last = {}
    for row in audit:  # 已按 id DESC，先见者为最新
        if row["source"] and row["source"] not in last:
            last[row["source"]] = row
    for name, row in last.items():
        src = by_name[name]
        assert (row["state_level"], row["state_mode"]) == (src["level"], src["output_mode"]), row

    assert overview["homogeneous"] is True


def test_breach_keeps_source_frozen_and_output_write_rejected():
    """复现口径：违约判定后目录不得仍可写；徽标与目录同态。"""
    c = _client()
    h = _headers(c)

    src = _source(c, h, "物流源")
    assert (src["level"], src["output_mode"]) == ("违约", "冻结")

    # 违约源：输出写必须被拒（423），不得“徽标只读、目录仍可写”
    r = c.post("/api/output", headers=h, json={"source": "物流源", "payload": "write-probe"})
    assert r.status_code == 423, r.text
    assert (src["level"], src["output_mode"]) == ("违约", "冻结")

    # 一条准点到达不得绕过补齐直接解锁
    r = c.post(
        "/api/arrivals",
        headers=h,
        json={
            "source": "物流源",
            "source_clock": "2026-11-16T10:00:00",
            "ingest_clock": "2026-11-16T10:00:00",
        },
    )
    assert r.status_code == 200, r.text
    # 冻结源仅补齐可解冻：新到达不得越过补齐把等级牌改回正常（否则又是分裂）
    assert r.json()["level"] == "违约"
    assert r.json()["output_mode"] == "冻结"

    src = _source(c, h, "物流源")
    assert (src["level"], src["output_mode"]) == ("违约", "冻结")
    _assert_five_surfaces(c, h)


def test_joint_failure_rolls_back_both_sides(monkeypatch):
    """连坐失败测：等级牌已写、审计落库失败时，等级牌与输出牌两侧同撤。"""
    c = _client()
    h = _headers(c)
    before = _db_snapshot()

    orig = appmod.log_audit

    def boom(conn, event, **kw):
        if event == "等级变更":
            raise RuntimeError("审计落库失败（注入）")
        return orig(conn, event, **kw)

    monkeypatch.setattr(appmod, "log_audit", boom)

    r = c.post(
        "/api/arrivals",
        headers=h,
        json={
            "source": "订单源",
            "source_clock": "2026-11-16T10:00:00",
            "ingest_clock": "2026-11-16T11:00:00",
        },
    )
    assert r.status_code == 500, r.text

    after = _db_snapshot()
    assert after[0] == before[0], "等级牌与输出牌必须同撤"
    assert after[1] == before[1], "到达卡片不得落半截"
    assert after[2] == before[2], "审计流水不得落半截"


def test_dual_path_level_conflict_records_contention():
    """双路抢级失败测：值班与策略席同抢同一到达的等级，后交只记“等级分裂争用”。"""
    c = _client()
    h = _headers(c)

    # 值班先登记一条告警到达（订单源 SLA 15，滞后 20 分）
    r = c.post(
        "/api/arrivals",
        headers=h,
        json={
            "source": "订单源",
            "source_clock": "2026-11-16T10:00:00",
            "ingest_clock": "2026-11-16T10:20:00",
        },
    )
    assert r.status_code == 200, r.text
    arrival_id = r.json()["arrival_id"]

    src = _source(c, h, "订单源")
    assert (src["level"], src["output_mode"]) == ("告警", "降级只读")
    lock_version = src["version"]

    def duty():
        with TestClient(appmod.app, raise_server_exceptions=False) as tc:
            return tc.post(
                "/api/arrivals",
                headers=h,
                json={
                    "source": "订单源",
                    "source_clock": "2026-11-16T10:00:00",
                    "ingest_clock": "2026-11-16T11:00:00",
                    "expected_version": lock_version,
                },
            )

    def strategy():
        with TestClient(appmod.app, raise_server_exceptions=False) as tc:
            return tc.post(
                "/api/levels/订单源",
                headers=h,
                json={"level": "正常", "arrival_id": arrival_id, "expected_version": lock_version},
            )

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [f.result() for f in (ex.submit(duty), ex.submit(strategy))]

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [(r.status_code, r.text) for r in results]

    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "等级分裂争用"

    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["event"] == "等级分裂争用" for a in audit), audit

    _assert_five_surfaces(c, h)


def test_catchup_restores_and_surfaces_agree():
    """补齐恢复后五处同态，且输出写恢复放行。"""
    c = _client()
    h = _headers(c)

    before = _source(c, h, "物流源")
    assert (before["level"], before["output_mode"]) == ("违约", "冻结")

    r = c.post("/api/catchup/物流源", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["output_mode"] == "可写"

    after = _source(c, h, "物流源")
    assert (after["level"], after["output_mode"]) == ("正常", "可写")
    assert after["version"] > before["version"]

    r = c.post("/api/output", headers=h, json={"source": "物流源", "payload": "ok"})
    assert r.status_code == 200, r.text

    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["event"] == "补齐恢复" and a["source"] == "物流源" for a in audit)

    _assert_five_surfaces(c, h)


def test_cut_path_tamper_is_rejected_and_recorded():
    """分裂当日改口：库内切断路径被改，init_db 须留“分裂偏了”并拒绝改口。"""
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.execute("UPDATE split_meta SET value='乙' WHERE key='cut_path'")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="分裂偏了"):
        appmod.init_db()

    conn = sqlite3.connect(appmod.DB_PATH)
    rows = [r[0] for r in conn.execute("SELECT reason FROM split_divergence")]
    conn.close()
    assert any("分裂偏了" in r and "乙" in r for r in rows), rows
