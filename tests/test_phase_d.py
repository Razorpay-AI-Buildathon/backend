import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import uuid
import time

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent, Execution

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_multi_tenant_isolation(setup_db):
    db_session = setup_db
    
    # 1. Create Merchants A and B
    merch_a = Merchant(id="merch_a", name="Merchant A")
    merch_b = Merchant(id="merch_b", name="Merchant B")
    
    cust_a = Customer(id="cust_a", merchant_id="merch_a", email="a@tenant.com")
    cust_b = Customer(id="cust_b", merchant_id="merch_b", email="b@tenant.com")
    
    db_session.add_all([merch_a, merch_b, cust_a, cust_b])
    db_session.flush()

    # 2. Ingest payment event for Merchant A
    evt_a = PaymentEvent(
        id="evt_a",
        merchant_id="merch_a",
        customer_id="cust_a",
        event_type="FAILED_PAYMENT",
        amount=100.0,
        currency="INR"
    )
    db_session.add(evt_a)
    db_session.flush()

    # 3. Create case for Merchant A
    case_a = RecoveryCase(
        id="case_a",
        event_id=evt_a.id,
        merchant_id="merch_a",
        customer_id="cust_a",
        status=CaseStatus.IDENTIFIED
    )
    db_session.add(case_a)
    db_session.commit()

    # 4. Attempt to retrieve case_a with Merchant B credentials (403 expected)
    headers_b = {"X-API-KEY": "RECOVERAI-KEY-merch_b"}
    resp = client.get("/api/cases/case_a", headers=headers_b)
    assert resp.status_code == 403
    assert "belongs to another merchant" in resp.json()["detail"]

    # 5. Retrieve case_a with Merchant A credentials (200 expected)
    headers_a = {"X-API-KEY": "RECOVERAI-KEY-merch_a"}
    resp = client.get("/api/cases/case_a", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["case_id"] == "case_a"

    # 6. List cases for Merchant B and verify Merchant A's case is isolated
    resp = client.get("/api/cases", headers=headers_b)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 0

    # 7. Attempt to review Merchant A's case using Merchant B's token (403 expected)
    review_payload = {
        "action": "CLOSE",
        "operator_id": "op_test",
        "notes": "unauthorized review"
    }
    resp = client.post("/api/cases/case_a/review", json=review_payload, headers=headers_b)
    assert resp.status_code == 403


def test_rate_limiting(setup_db):
    headers = {"X-API-KEY": "RECOVERAI-KEY-rate_limit_merchant"}
    
    # Send a burst of requests to exceed the limit (limit is 20 requests per minute)
    # We will verify that requests are processed and then get throttled
    throttled = False
    for _ in range(30):
        payload = {
            "event_id": f"evt-{uuid.uuid4().hex[:10]}",
            "merchant_id": "rate_limit_merchant",
            "customer_id": "cust_rl",
            "event_type": "FAILED_PAYMENT",
            "amount": 1000.0,
            "currency": "INR",
            "failure_code": "insufficient_funds",
            "provider": "razorpay"
        }
        resp = client.post("/api/events/payment", json=payload, headers=headers)
        if resp.status_code == 429:
            throttled = True
            break
            
    assert throttled is True
