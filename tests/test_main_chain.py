async def test_breaking_apply_reconcile(authed):
    items = (await (await authed.get("/api/contracts")).json())["items"]
    draft = next(c for c in items if c["status"] == "草稿")
    check = await (await authed.post(f"/api/contracts/{draft['id']}/breaking-check", json={})).json()
    assert check["check"]["breaking"] is True
    assert "amount" in check["check"]["removed_fields"]

    applied = await (await authed.post(f"/api/contracts/{draft['id']}/apply", json={})).json()
    assert applied["contract"]["status"] == "现行"

    again = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert again.status == 409

    rep = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-18"})).json()
    assert rep["report"]["matched"] >= 1
    assert "对账" in rep["report"]["summary"]
