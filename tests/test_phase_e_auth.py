import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db.session import SessionLocal, Base, engine

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_auth_boundary_mutations_blocked_without_key():
    # 1. Mutation routes must return 401 when no X-API-KEY is provided
    endpoints_to_test = [
        ("POST", "/api/events/payment", {"event_id": "test", "merchant_id": "m", "customer_id": "c", "event_type": "FAILED_PAYMENT", "amount": 100, "currency": "INR"}),
        ("POST", "/api/execute", {"action_type": "RETRY_PAYMENT", "amount": 100, "currency": "INR", "authorization_token": "tok", "guard_approved": True, "case_id": "case", "event_id": "evt", "action_id": "act"}),
        ("POST", "/api/cases/some-case/review", {"action": "APPROVE", "operator_id": "op", "notes": "note"}),
        ("POST", "/api/policies", {"merchant_id": "m", "max_attempts": 3, "retry_backoff": 60, "amount_threshold": 5000, "allowed_actions": ["RETRY"], "human_review_threshold": 80, "risk_threshold": 70, "cooldown": 300, "enabled": True}),
        ("GET", "/api/policies/some-merchant", None),
        ("POST", "/api/cases/detect-timeouts", None),
    ]

    for method, path, payload in endpoints_to_test:
        if method == "POST":
            resp = client.post(path, json=payload)
        else:
            resp = client.get(path)
        assert resp.status_code == 401, f"Expected 401 for {method} {path}, got {resp.status_code}"


def test_auth_boundary_read_only_public_demo():
    # 2. Read-only and status routes should remain public/accessible for demo dashboard mode
    resp = client.get("/api/metrics")
    assert resp.status_code == 200

    resp = client.get("/api/cases")
    assert resp.status_code == 200

    resp = client.get("/health")
    assert resp.status_code == 200
