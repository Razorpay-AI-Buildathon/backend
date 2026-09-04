import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
import uuid
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, PaymentEvent, Customer, AuditEvent

client = TestClient(app)
auth_headers = {"X-API-KEY": "RECOVERAI-TESTKEY-12345"}

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_ready_check(monkeypatch):
    import httpx
    from app.services.redis_cache import RedisCache

    class MockResponse:
        status_code = 200
        def json(self):
            return {"status": "ok"}

    def mock_get(*args, **kwargs):
        return MockResponse()

    def mock_redis_set(*args, **kwargs):
        return True

    monkeypatch.setattr(httpx, "get", mock_get)
    monkeypatch.setattr(RedisCache, "set", mock_redis_set)
    
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_operator_admin_controls(setup_db):
    db_session = setup_db
    
    # 1. Setup customer and case
    customer = Customer(id="cust_admin", email="operator@example.com")
    event = PaymentEvent(id="evt_admin", event_type="FAILED_PAYMENT", amount=150.0)
    case = RecoveryCase(id="case_admin", event_id="evt_admin", customer_id="cust_admin", status=CaseStatus.IDENTIFIED)
    db_session.add(customer)
    db_session.add(event)
    db_session.add(case)
    db_session.commit()
    
    # 2. Test PAUSE control
    payload = {"action": "PAUSE", "operator_id": "OP-123", "notes": "Hold payment recovery"}
    resp = client.post("/api/cases/case_admin/admin-control", json=payload, headers=auth_headers)
    assert resp.status_code == 200
    
    db_session.expire_all()
    c = db_session.query(RecoveryCase).filter(RecoveryCase.id == "case_admin").first()
    assert c.status == CaseStatus.HUMAN_REVIEW
    
    # Check AuditEvent
    evt = db_session.query(AuditEvent).filter(AuditEvent.event_type == "OPERATOR_PAUSE").first()
    assert evt is not None
    assert evt.actor == "OP-123"
    assert evt.metadata_json["notes"] == "Hold payment recovery"

    # 3. Test RETRY control
    payload = {"action": "RETRY", "operator_id": "OP-123", "notes": "Retry execution"}
    resp = client.post("/api/cases/case_admin/admin-control", json=payload, headers=auth_headers)
    assert resp.status_code == 200
    
    db_session.expire_all()
    c = db_session.query(RecoveryCase).filter(RecoveryCase.id == "case_admin").first()
    assert c.status == CaseStatus.ANALYZING
    assert c.current_recovery_attempt == 0


def test_data_retention_pii_cleanup(setup_db):
    db_session = setup_db
    
    # 1. Setup old closed case
    customer = Customer(id="cust_old", email="sensitive@example.com", phone="999-888-7777")
    event = PaymentEvent(id="evt_old", event_type="FAILED_PAYMENT", amount=500.0, payload_metadata={"card": "1234-5678-8901"})
    case = RecoveryCase(
        id="case_old", 
        event_id="evt_old", 
        customer_id="cust_old", 
        status=CaseStatus.CLOSED,
        closed_at=datetime.utcnow() - timedelta(days=40)
    )
    db_session.add(customer)
    db_session.add(event)
    db_session.add(case)
    db_session.commit()
    
    # 2. Trigger cleanup for cases older than 30 days
    cleanup_payload = {"retention_days": 30}
    resp = client.post("/api/admin/cleanup-pii", json=cleanup_payload, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["redacted_cases_count"] == 1
    
    db_session.expire_all()
    cust = db_session.query(Customer).filter(Customer.id == "cust_old").first()
    assert cust.email == "redacted@example.com"
    assert cust.phone == "redacted"
    
    evt = db_session.query(PaymentEvent).filter(PaymentEvent.id == "evt_old").first()
    assert "card" not in evt.payload_metadata
    assert evt.payload_metadata["status"] == "pii_redacted"
