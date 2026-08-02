"""Backend tests for Step Solar Lead Capture + CRM API."""
import os
import uuid
import random
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://leads-engine.preview.emergentagent.com").rstrip("/")
ADMIN_TOKEN = "stepsolar-admin-change-me"

REQUIRED_LEAD_KEYS = {
    "id", "code", "full_name", "phone", "email", "state", "city", "pincode",
    "property_type", "monthly_bill", "roof_type", "timeline",
    "stages", "quotation", "invoice", "created_at", "updated_at",
}


def _rand_phone():
    return str(random.choice([6, 7, 8, 9])) + "".join(str(random.randint(0, 9)) for _ in range(9))


def _rand_email():
    return f"TEST_{uuid.uuid4().hex[:10]}@example.com"


def _valid_payload(phone=None, email=None, source="website"):
    return {
        "full_name": "TEST User",
        "phone": phone or _rand_phone(),
        "email": email or _rand_email(),
        "state": "Uttar Pradesh",
        "city": "Ghazipur",
        "pincode": "233001",
        "property_type": "Residential",
        "monthly_bill": 4000,
        "roof_type": "Medium Space (300-500 sq. ft.)",
        "timeline": "Immediately",
        "source": source,
    }


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def admin_s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json", "X-Admin-Token": ADMIN_TOKEN})
    return sess


# ---------- Health -------------------------------------------------
class TestHealth:
    def test_health_ok(self, s):
        r = s.get(f"{BASE_URL}/api/health", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["integrations"]["mongodb"] is True


# ---------- Legacy public POST /api/leads --------------------------
class TestPublicLeads:
    def test_create_success_returns_code(self, s):
        payload = _valid_payload()
        r = s.post(f"{BASE_URL}/api/leads", json=payload, timeout=30)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["ok"] is True
        assert isinstance(data["id"], str)
        # New CRM code should be present in response
        assert "code" in data and isinstance(data["code"], str)
        assert data["code"].startswith("SSE-") and len(data["code"]) == 8

        # Verify the created lead appears in CRM listing with stages initialised
        r2 = s.get(
            f"{BASE_URL}/api/crm/leads",
            headers={"X-Admin-Token": ADMIN_TOKEN},
            timeout=15,
        )
        assert r2.status_code == 200
        match = [x for x in r2.json() if x["id"] == data["id"]]
        assert len(match) == 1
        lead = match[0]
        assert lead["code"] == data["code"]
        assert isinstance(lead["stages"], list) and len(lead["stages"]) == 10
        assert lead["stages"][0]["status"] == "In Progress"

    def test_duplicate_429(self, s):
        p = _valid_payload()
        r1 = s.post(f"{BASE_URL}/api/leads", json=p, timeout=20)
        assert r1.status_code == 201
        r2 = s.post(f"{BASE_URL}/api/leads", json=p, timeout=20)
        assert r2.status_code == 429

    def test_invalid_phone(self, s):
        p = _valid_payload()
        p["phone"] = "12345"
        r = s.post(f"{BASE_URL}/api/leads", json=p, timeout=15)
        assert r.status_code == 422

    def test_legacy_admin_list_has_new_fields(self, s):
        # Ensure at least one lead exists
        s.post(f"{BASE_URL}/api/leads", json=_valid_payload(), timeout=20)
        r = s.get(f"{BASE_URL}/api/leads", params={"token": ADMIN_TOKEN}, timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and len(items) > 0
        first = items[0]
        # Backward compatibility - new CRM fields must be present
        for key in ("code", "stages", "quotation", "invoice"):
            assert key in first, f"Missing key {key} in legacy admin listing"


# ---------- CRM /meta ------------------------------------------------
class TestCRMMeta:
    def test_meta_no_token_401(self, s):
        r = s.get(f"{BASE_URL}/api/crm/meta", timeout=15)
        assert r.status_code == 401

    def test_meta_wrong_token_401(self, s):
        r = s.get(f"{BASE_URL}/api/crm/meta", params={"token": "wrong"}, timeout=15)
        assert r.status_code == 401

    def test_meta_ok_via_query(self, s):
        r = s.get(f"{BASE_URL}/api/crm/meta", params={"token": ADMIN_TOKEN}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["nextLeadNo"], int)
        assert isinstance(data["nextLeadCode"], str)
        assert data["nextLeadCode"] == f"SSE-{data['nextLeadNo']:04d}"

    def test_meta_ok_via_header(self, admin_s):
        r = admin_s.get(f"{BASE_URL}/api/crm/meta", timeout=15)
        assert r.status_code == 200
        assert "nextLeadCode" in r.json()


# ---------- CRM GET /leads shape -------------------------------------
class TestCRMLeadsList:
    def test_list_no_token_401(self, s):
        r = s.get(f"{BASE_URL}/api/crm/leads", timeout=15)
        assert r.status_code == 401

    def test_list_returns_full_shape(self, admin_s):
        r = admin_s.get(f"{BASE_URL}/api/crm/leads", timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert len(items) > 0, "expected at least one lead in CRM"
        for lead in items:
            missing = REQUIRED_LEAD_KEYS - set(lead.keys())
            assert not missing, f"Lead {lead.get('id')} missing keys: {missing}"
            assert isinstance(lead["stages"], list)
            assert len(lead["stages"]) == 10
            # _id must NOT leak from mongo
            assert "_id" not in lead


# ---------- CRM POST /leads -----------------------------------------
class TestCRMCreateLead:
    def test_create_and_verify_counter_advance(self, admin_s):
        meta_before = admin_s.get(f"{BASE_URL}/api/crm/meta", timeout=15).json()
        payload = _valid_payload(source="Admin")
        r = admin_s.post(f"{BASE_URL}/api/crm/leads", json=payload, timeout=20)
        assert r.status_code in (200, 201), r.text
        created = r.json()
        # Response shape
        for key in ("id", "code", "stages", "quotation", "invoice", "created_at"):
            assert key in created
        assert created["stages"][0]["status"] == "In Progress"
        assert created["source"] == "Admin"
        assert "_id" not in created
        # Counter must advance (parallel workers may increment further, so >= before+1)
        meta_after = admin_s.get(f"{BASE_URL}/api/crm/meta", timeout=15).json()
        assert meta_after["nextLeadNo"] >= meta_before["nextLeadNo"] + 1
        # Created code format SSE-#### and its number is > meta_before.nextLeadNo
        assert created["code"].startswith("SSE-")
        code_num = int(created["code"].split("-")[1])
        assert code_num > meta_before["nextLeadNo"]
        assert code_num <= meta_after["nextLeadNo"]

    def test_create_invalid_payload_422(self, admin_s):
        bad = _valid_payload()
        bad["phone"] = "abc"
        r = admin_s.post(f"{BASE_URL}/api/crm/leads", json=bad, timeout=15)
        assert r.status_code == 422
        body = r.json()
        assert body.get("ok") is False
        assert "phone" in body.get("detail", "").lower()


# ---------- CRM PATCH /leads/{id} -----------------------------------
class TestCRMPatch:
    @pytest.fixture(scope="class")
    def a_lead(self, admin_s):
        r = admin_s.post(f"{BASE_URL}/api/crm/leads", json=_valid_payload(source="Admin"), timeout=20)
        assert r.status_code in (200, 201)
        return r.json()

    def test_patch_stages_persists(self, admin_s, a_lead):
        lead = a_lead
        stages = lead["stages"]
        stages[0]["status"] = "Completed"
        stages[0]["notes"] = "TEST_stage0_done"
        r = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/{lead['id']}",
            json={"stages": stages},
            timeout=15,
        )
        assert r.status_code == 200
        updated = r.json()
        assert updated["stages"][0]["status"] == "Completed"
        assert updated["stages"][0]["notes"] == "TEST_stage0_done"

        # Verify via list GET
        r2 = admin_s.get(f"{BASE_URL}/api/crm/leads", timeout=15)
        found = [x for x in r2.json() if x["id"] == lead["id"]][0]
        assert found["stages"][0]["status"] == "Completed"
        assert found["stages"][0]["notes"] == "TEST_stage0_done"

    def test_patch_quotation_then_invoice(self, admin_s, a_lead):
        lead = a_lead
        quotation = {
            "systemSizeKw": 3,
            "items": [{"desc": "TEST_module", "qty": 1, "rate": 100000}],
            "gstPercent": 12,
            "createdAt": "2026-01-01T00:00:00Z",
        }
        r = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/{lead['id']}",
            json={"quotation": quotation},
            timeout=15,
        )
        assert r.status_code == 200
        assert r.json()["quotation"]["items"][0]["desc"] == "TEST_module"

        invoice = {
            "number": "SSE/2025-26/TEST",
            "createdAt": "2026-01-01T00:00:00Z",
            "items": quotation["items"],
            "gstPercent": 12,
            "paymentStatus": "Unpaid",
        }
        r2 = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/{lead['id']}",
            json={"invoice": invoice},
            timeout=15,
        )
        assert r2.status_code == 200
        got = r2.json()
        assert got["invoice"]["number"] == "SSE/2025-26/TEST"
        assert got["quotation"]["items"][0]["desc"] == "TEST_module"  # not overwritten

    def test_patch_empty_body_400(self, admin_s, a_lead):
        r = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/{a_lead['id']}",
            json={},
            timeout=15,
        )
        assert r.status_code == 400

    def test_patch_nonexistent_404(self, admin_s):
        r = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/does-not-exist-{uuid.uuid4().hex}",
            json={"notes": "x"},
            timeout=15,
        )
        assert r.status_code == 404

    def test_patch_ignores_non_whitelist_fields(self, admin_s, a_lead):
        r = admin_s.patch(
            f"{BASE_URL}/api/crm/leads/{a_lead['id']}",
            json={"is_admin": True, "notes": "TEST_only_notes"},
            timeout=15,
        )
        assert r.status_code == 200
        updated = r.json()
        assert updated.get("is_admin") is not True
        assert updated.get("notes") == "TEST_only_notes"

    def test_patch_unauth_401(self, s, a_lead):
        r = s.patch(
            f"{BASE_URL}/api/crm/leads/{a_lead['id']}",
            json={"notes": "TEST_should_fail"},
            timeout=15,
        )
        assert r.status_code == 401


# ---------- CRM DELETE /leads/{id} ----------------------------------
class TestCRMDelete:
    def test_delete_then_double_delete_404(self, admin_s):
        r = admin_s.post(f"{BASE_URL}/api/crm/leads", json=_valid_payload(source="Admin"), timeout=20)
        assert r.status_code in (200, 201)
        lid = r.json()["id"]

        r1 = admin_s.delete(f"{BASE_URL}/api/crm/leads/{lid}", timeout=15)
        assert r1.status_code == 200
        assert r1.json().get("ok") is True

        r2 = admin_s.delete(f"{BASE_URL}/api/crm/leads/{lid}", timeout=15)
        assert r2.status_code == 404

    def test_delete_unauth_401(self, s):
        r = s.delete(f"{BASE_URL}/api/crm/leads/anything", timeout=15)
        assert r.status_code == 401
