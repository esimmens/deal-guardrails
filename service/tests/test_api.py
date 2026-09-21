from .helpers import submission

TOKEN = {"Authorization": "Bearer test-tool-token"}


def test_post_deals_requires_the_tool_token(client):
    assert client.post("/deals", json=submission().model_dump(mode="json")).status_code == 401
    assert client.post("/deals", json=submission().model_dump(mode="json"), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_post_deals_returns_a_server_authored_confirmation(client, admin):
    body = submission().model_dump(mode="json")
    resp = client.post("/deals", json=body, headers=TOKEN)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deal_id"] == "DG-1001" and data["duplicate"] is False and data["status"] == "pending_approval"
    assert data["confirmation_line"] == "Submitted as DG-1001."
    assert data["receipt"]["receipt_url"] == "http://testserver/deals/DG-1001"
    again = client.post("/deals", json=body, headers=TOKEN).json()
    assert again["duplicate"] is True and again["deal_id"] == "DG-1001"
    # dispatch tried and failed fast (nothing listens); the outbox row is deferred, not lost
    n = admin.execute("SELECT status::text s, attempts FROM notifications").fetchone()
    assert n["s"] == "pending" and n["attempts"] == 1


def test_validation_errors_are_400_and_record_nothing(client, admin):
    body = submission().model_dump(mode="json")
    body["discount_percent"] = 140
    resp = client.post("/deals", json=body, headers=TOKEN)
    assert resp.status_code == 400 and resp.json()["error"] == "validation"
    assert admin.execute("SELECT count(*) n FROM deals").fetchone()["n"] == 0


def test_get_deal_json_and_html_and_listing(client):
    client.post("/deals", json=submission().model_dump(mode="json"), headers=TOKEN)
    j = client.get("/deals/DG-1001").json()
    assert j["deal_id"] == "DG-1001" and j["receipt"]["rules_fired"][0]["id"] == "R1b"
    h = client.get("/deals/dg-1001", headers={"Accept": "text/html"})
    assert h.status_code == 200 and "DG-1001" in h.text and "R2" in h.text
    assert client.get("/deals/DG-9999").status_code == 404
    listing = client.get("/deals", params={"status": "pending_approval"}).json()
    assert listing[0]["deal_id"] == "DG-1001" and listing[0]["gate_slack_user_ids"] == ["U_PRIYA"]
    assert client.get("/deals", params={"older_than": "PT4H"}).json() == []
    assert client.get("/deals", params={"older_than": "nonsense"}).status_code == 400


def test_health_reports_policy_and_audit_head(client):
    h = client.get("/health").json()
    assert h["ok"] and h["policy_version"].startswith("sha256:") and h["audit_head"]["seq"] == 0
