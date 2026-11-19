import asyncio

import pytest

from app.db import get_db
from app.services.errors import ServiceError
from app.services.masking import (
    MASK_SENTINEL,
    compliance_state,
    emit_outbound,
    outbound,
    outbound_card,
    refresh_masking,
)


async def _mask(authed):
    return await (await authed.get("/api/mask")).json()


async def test_stale_rule_is_red_and_outbound_never_plaintext(client):
    # 初始现场：契约已标敏感、页面标记也开着，但落库规则还是明文 → 合规挂红。
    await client.post("/api/login", json={"username": "admin", "password": "123456"})
    meta = await (await client.get("/api/meta")).json()
    assert meta["phone_masked"] is True
    assert meta["outbound_phone"] == MASK_SENTINEL  # 清洗侧不看陈旧规则，按契约脱敏
    assert meta["compliance"]["ok"] is False
    assert any("明文" in p for p in meta["compliance"]["problems"])


async def test_page_and_contract_flags_must_agree(authed):
    # 页面把契约敏感列的脱敏关掉 = 假明文，刷新必须拒绝，且一条规则都不落。
    r = await authed.post("/api/mask/page", json={"field": "phone", "masked": False})
    assert r.status == 200
    body = await r.json()
    assert body["sensitive"] is True
    assert "拒绝" in body["message"] or "被拒" in body["message"]

    before = await _stored_rules(authed)
    ref = await authed.post("/api/mask/refresh", json={})
    assert ref.status == 409
    err = await ref.json()
    assert "被拒" in err["error"] and "明文" in err["error"]
    assert await _stored_rules(authed) == before  # 被拒不写半套规则
    assert (await _mask(authed))["compliance"]["ok"] is False

    # 打开后重算才能通过。
    await authed.post("/api/mask/page", json={"field": "phone", "masked": True})
    ok = await authed.post("/api/mask/refresh", json={})
    assert ok.status == 200
    assert (await ok.json())["compliance"]["ok"] is True


async def test_refresh_recomputes_and_unblocks_compliance(authed):
    rec = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-18"})).json()
    assert rec["report"]["compliance_ok"] == 0  # 未刷新 → 合规水线挂红

    ref = await authed.post("/api/mask/refresh", json={})
    assert ref.status == 200
    body = await ref.json()
    assert body["decision"] == "提交时重算"
    assert body["chip"]["label"] == "已重算"
    assert len(body["chip"]["digest_short"]) == 12
    assert body["compliance"]["ok"] is True
    assert body["changed"] >= 1

    status = await _mask(authed)
    phone = next(f for f in status["fields"] if f["field"] == "phone")
    assert phone["rule_masked"] is True
    assert phone["outbound_mode"] == "masked"
    assert phone["rule_matches"] is True
    # 脱敏列表亮重算芯片 / 快照摘要。
    assert status["chip"]["label"] == "已重算"
    assert status["chip"]["digest"] == status["compliance"]["digest"]
    assert any(r["status"] == "成功" for r in status["runs"])

    rec2 = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-18"})).json()
    assert rec2["report"]["compliance_ok"] == 1
    assert rec2["report"]["compliance_digest"] == status["compliance"]["digest"]
    assert "合规通过" in rec2["report"]["compliance_detail"]

    meta = await (await authed.get("/api/meta")).json()
    assert meta["chip"]["label"] == "已重算"
    assert meta["outbound_phone"] == MASK_SENTINEL


async def test_plaintext_outbound_is_refused_and_history_reclaimed(client):
    await client.post("/api/login", json={"username": "admin", "password": "123456"})
    db = client.server.app["db_path"]

    card = outbound_card("phone", "13800138000", db)
    assert card["masked"] is True
    assert card["outbound"] == MASK_SENTINEL
    assert card["violations"]  # 未刷新时卡片要讲清楚违规点

    emitted = emit_outbound("phone", "13800138000", db)
    assert emitted["outbound"] == MASK_SENTINEL
    assert outbound("phone", "13800138000", db) == MASK_SENTINEL

    with get_db(db) as conn:
        new_row = conn.execute(
            "SELECT payload, masked FROM outbound_log WHERE id=?", (emitted["log_id"],)
        ).fetchone()
        # 脏规则还在时刷新前也照样只落脱敏值：清洗侧自验 + 合规侧复验都不放明文。
        assert new_row["payload"] == MASK_SENTINEL
        assert new_row["masked"] == 1
        assert compliance_state(conn)["ok"] is False  # 历史明文没收回前合规仍挂红

    refresh_masking(db)
    with get_db(db) as conn:
        comp = compliance_state(conn)
        rows = conn.execute(
            "SELECT id, payload, masked, quarantined FROM outbound_log ORDER BY id"
        ).fetchall()
    assert comp["ok"] is True
    assert rows and all(r["payload"] == MASK_SENTINEL for r in rows)
    # 刷新前那条明文被收回并隔离；刷新后正常脱敏出站的不隔离。
    assert rows[0]["quarantined"] == 1
    assert rows[-1]["id"] == emitted["log_id"]
    assert rows[-1]["quarantined"] == 0


async def test_second_refresh_is_blocked_by_lock(client):
    await client.post("/api/login", json={"username": "admin", "password": "123456"})
    db = client.server.app["db_path"]
    with get_db(db) as conn:
        conn.execute(
            "UPDATE mask_lock SET active=1, holder='合规员A', acquired_at=datetime('now') WHERE id=1"
        )
    with pytest.raises(ServiceError) as exc:
        refresh_masking(db)
    assert exc.value.status_code == 409
    assert "另一名合规员" in exc.value.message


async def test_page_flag_change_without_refresh_stays_red(authed):
    assert (await authed.post("/api/mask/refresh", json={})).status == 200
    before = (await _mask(authed))["chip"]
    assert before["label"] == "已重算"

    # 只动页面标记、不刷新：芯片显示已过期，合规水线仍读同一标记并挂红。
    await authed.post("/api/mask/page", json={"field": "amount", "masked": True})
    status = await _mask(authed)
    assert status["chip"]["label"] == "已过期·待刷新"
    assert status["chip"]["stale"] is True
    assert status["compliance"]["ok"] is False
    assert any("没刷新" in p for p in status["compliance"]["problems"])

    # 刷新后规则落库，芯片摘要与合规摘要一致。
    ref = await authed.post("/api/mask/refresh", json={})
    assert ref.status == 200
    status = await _mask(authed)
    assert status["compliance"]["ok"] is True
    assert status["chip"]["digest"] == status["compliance"]["digest"]
    assert status["chip"]["digest"] != before["digest"]


async def test_two_refreshes_do_not_double_write(authed):
    r1, r2 = await asyncio.gather(
        authed.post("/api/mask/refresh", json={}),
        authed.post("/api/mask/refresh", json={}),
    )
    assert r1.status == 200 and r2.status == 200
    status = await _mask(authed)
    # 锁把两次动作串行化：两条成功记录、摘要一致、规则只一套。
    successes = [r for r in status["runs"] if r["status"] == "成功"]
    assert len(successes) >= 2
    assert len({r["digest"] for r in successes}) == 1
    assert sum(1 for f in status["fields"] if f["field"] == "phone") == 1


async def test_failure_message_is_plain_language(authed, monkeypatch):
    import app.services.masking as masking

    def boom(conn):
        raise RuntimeError("磁盘打嗝")

    monkeypatch.setattr(masking, "derive_rules", boom)
    r = await authed.post("/api/mask/refresh", json={})
    assert r.status == 500
    body = await r.json()
    assert "脱敏刷新失败" in body["error"]
    assert "本次没有写入任何规则" in body["error"]


async def _stored_rules(authed):
    status = await _mask(authed)
    return [
        (f["field"], f["rule_masked"], f["outbound_mode"]) for f in status["fields"]
    ]
