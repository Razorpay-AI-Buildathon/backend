import os
# Set env var before importing app to avoid 500 configuration error
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import uuid

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(AuditEvent).delete()
    db.query(RecoveryAction).delete()
    db.query(RecoveryCase).delete()
    db.query(PaymentEvent).delete()
    db.query(Customer).delete()
    db.query(Merchant).delete()
    db.commit()
    yield db
    db.close()


@pytest.fixture(scope="function")
def auth_headers():
    return {"X-API-KEY": "RECOVERAI-TESTKEY-12345"}


def test_first_class_audit_events(setup_db, auth_headers):
    db_session = setup_db
    # Setup test event and case
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    merchant_id = f"merch-{uuid.uuid4().hex[:6]}"
    customer_id = f"cust-{uuid.uuid4().hex[:6]}"

    payload = {
        "event_id": event_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "event_type": "FAILED_PAYMENT",
        "amount": 2500.00,
        "currency": "INR",
        "failure_code": "insufficient_funds",
        "provider": "razorpay",
        "provider_event_id": f"pay_{uuid.uuid4().hex[:12]}",
        "metadata": {"customer_email": "test@example.com"}
    }

    # Ingest event
    resp = client.post("/api/events/payment", json=payload, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    case_id = data["case_id"]

    # Verify CASE_CREATED audit event exists in DB
    events = db_session.query(AuditEvent).filter(AuditEvent.case_id == case_id).all()
    assert len(events) >= 1
    created_evt = next((e for e in events if e.event_type == "CASE_CREATED"), None)
    assert created_evt is not None
    assert created_evt.actor == "SYSTEM"
    assert created_evt.decision_source == "SYSTEM"

    # Fetch CaseDetail API and verify audit_events in response
    detail_resp = client.get(f"/api/cases/{case_id}", headers=auth_headers)
    assert detail_resp.status_code == 200
    detail_data = detail_resp.json()
    assert "audit_events" in detail_data
    assert len(detail_data["audit_events"]) >= 1
    assert detail_data["audit_events"][0]["event_type"] == "CASE_CREATED"


def test_human_review_queue_and_actions(setup_db, auth_headers):
    db_session = setup_db
    # Create merchant & customer
    merchant_id = f"merch-{uuid.uuid4().hex[:6]}"
    customer_id = f"cust-{uuid.uuid4().hex[:6]}"
    
    merchant = Merchant(id=merchant_id, name="Test Merchant", amount_threshold=5000.00, max_retries=3)
    customer = Customer(id=customer_id, merchant_id=merchant_id, email="cust@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id=f"evt-{uuid.uuid4().hex[:12]}",
        merchant_id=merchant_id,
        customer_id=customer_id,
        event_type="FAILED_PAYMENT",
        amount=1000.00,
        currency="INR",
        failure_code="insufficient_funds",
        provider="razorpay"
    )
    db_session.add(event)
    db_session.flush()

    # Create Case in HUMAN_REVIEW
    case = RecoveryCase(
        id=f"case-{uuid.uuid4().hex[:12]}",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.HUMAN_REVIEW,
        current_recovery_attempt=0
    )
    db_session.add(case)
    
    # Add a proposed action to be approved
    action = RecoveryAction(
        id=f"act-{uuid.uuid4().hex[:12]}",
        case_id=case.id,
        action_type=ActionType.RETRY_PAYMENT,
        proposed_by="AI_PLANNER",
        state=ActionState.PROPOSED
    )
    db_session.add(action)
    db_session.commit()

    # Verify query for human review cases
    list_resp = client.get("/api/cases?status=HUMAN_REVIEW", headers=auth_headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    case_ids = [item["case_id"] for item in list_data["items"]]
    assert case.id in case_ids

    # 1. Test Human Close Decision
    close_payload = {
        "action": "CLOSE",
        "operator_id": "operator_1",
        "notes": "Closing case manually"
    }
    review_resp = client.post(f"/api/cases/{case.id}/review", json=close_payload, headers=auth_headers)
    assert review_resp.status_code == 200
    assert review_resp.json()["resulting_status"] == "CLOSED"

    # Verify audit event for close
    db_session.expire_all()
    events = db_session.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
    close_evt = next((e for e in events if e.event_type == "CASE_CLOSED"), None)
    assert close_evt is not None
    assert close_evt.actor == "operator_1"
    assert close_evt.decision_source == "HUMAN_OPERATOR"

    # 2. Test Human Reject (Blocked) Decision
    # Reset case to HUMAN_REVIEW
    case.status = CaseStatus.HUMAN_REVIEW
    db_session.commit()
    
    reject_payload = {
        "action": "REJECT",
        "operator_id": "operator_2",
        "notes": "Rejecting recovery"
    }
    review_resp = client.post(f"/api/cases/{case.id}/review", json=reject_payload, headers=auth_headers)
    assert review_resp.status_code == 200
    assert review_resp.json()["resulting_status"] == "BLOCKED"

    # Verify audit event for reject
    db_session.expire_all()
    events = db_session.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
    reject_evt = next((e for e in events if e.event_type == "GUARD_BLOCKED"), None)
    assert reject_evt is not None
    assert reject_evt.actor == "operator_2"
    assert reject_evt.decision_source == "HUMAN_OPERATOR"

    # 3. Test Human Approve Decision (Passes ActionGuard since amount 1000 is < threshold 5000)
    # Reset case status and action
    case.status = CaseStatus.HUMAN_REVIEW
    action.state = ActionState.PROPOSED
    db_session.commit()

    approve_payload = {
        "action": "APPROVE",
        "operator_id": "operator_3",
        "notes": "Approving recovery retry"
    }
    review_resp = client.post(f"/api/cases/{case.id}/review", json=approve_payload, headers=auth_headers)
    assert review_resp.status_code == 200
    assert review_resp.json()["resulting_status"] == "APPROVED"

    # Verify audit event for approval
    db_session.expire_all()
    events = db_session.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
    approve_evt = next((e for e in events if e.event_type == "GUARD_APPROVED"), None)
    assert approve_evt is not None
    assert approve_evt.actor == "operator_3"
    assert approve_evt.decision_source == "HUMAN_OPERATOR"

    # 4. Test Human Approve Decision Blocks if ActionGuard Fails (e.g. amount 10000 > threshold 5000)
    # Reset case status and action, increase event amount
    case.status = CaseStatus.HUMAN_REVIEW
    action.state = ActionState.PROPOSED
    event.amount = 10000.00
    db_session.commit()

    review_resp = client.post(f"/api/cases/{case.id}/review", json=approve_payload, headers=auth_headers)
    assert review_resp.status_code == 400
    assert "ActionGuard blocked approval" in review_resp.json()["detail"]
