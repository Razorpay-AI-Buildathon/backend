import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import uuid
import threading
import time

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent, Execution, OutboxEvent, WebhookEvent
from app.services.outbox import OutboxPublisher, create_outbox_event

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(AuditEvent).delete()
    db.query(OutboxEvent).delete()
    db.query(WebhookEvent).delete()
    db.query(Execution).delete()
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


def test_provider_webhook_reconciliation(setup_db, auth_headers):
    db_session = setup_db
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
        amount=2000.00,
        currency="INR",
        failure_code="insufficient_funds",
        provider="razorpay"
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        id=f"case-{uuid.uuid4().hex[:12]}",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.EXECUTING,
        current_recovery_attempt=1
    )
    db_session.add(case)
    
    action = RecoveryAction(
        id=f"act-{uuid.uuid4().hex[:12]}",
        case_id=case.id,
        action_type=ActionType.RETRY_PAYMENT,
        proposed_by="AI_PLANNER",
        state=ActionState.EXECUTED
    )
    db_session.add(action)
    db_session.flush()

    execution = Execution(
        id=f"exec-{uuid.uuid4().hex[:12]}",
        action_id=action.id,
        case_id=case.id,
        status="PROCESSING",
        amount=2000.00,
        currency="INR",
        provider_reference="pay_reconcile_123"
    )
    db_session.add(execution)
    db_session.commit()

    # Ingest SUCCESS webhook
    webhook_payload = {
        "event": "payment.captured",
        "provider_event_id": "wh_evt_success_1",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_reconcile_123",
                    "status": "captured",
                    "amount": 200000
                }
            }
        }
    }
    
    resp = client.post("/api/webhooks/provider", json=webhook_payload, headers={"X-Razorpay-Signature": "test_signature"})
    assert resp.status_code == 200
    assert "Case transitioned to RECOVERED" in resp.json()["message"]

    db_session.expire_all()
    # Check Case status updated to RECOVERED
    updated_case = db_session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
    assert updated_case.status == CaseStatus.RECOVERED

    # Check Execution status updated to SUCCESS
    updated_exec = db_session.query(Execution).filter(Execution.id == execution.id).first()
    assert updated_exec.status == "SUCCESS"


def test_transactional_outbox(setup_db, auth_headers):
    db_session = setup_db
    
    # Ingest payment event which creates a case and writes to the outbox table in one transaction
    payload = {
        "event_id": f"evt-{uuid.uuid4().hex[:12]}",
        "merchant_id": f"merch-{uuid.uuid4().hex[:6]}",
        "customer_id": f"cust-{uuid.uuid4().hex[:6]}",
        "event_type": "FAILED_PAYMENT",
        "amount": 1500.00,
        "currency": "INR",
        "failure_code": "expired_card",
        "provider": "razorpay",
        "provider_event_id": f"pay_{uuid.uuid4().hex[:12]}",
        "metadata": {"customer_email": "test_outbox@example.com"}
    }

    resp = client.post("/api/events/payment", json=payload, headers=auth_headers)
    assert resp.status_code == 200
    case_id = resp.json()["case_id"]

    # Verify OutboxEvent row is inserted in database
    outbox_events = db_session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == case_id).all()
    assert len(outbox_events) == 1
    event = outbox_events[0]
    assert event.event_type == "evaluate_case"
    assert event.published_at is None

    # Call OutboxPublisher to publish
    count = OutboxPublisher.publish_pending_events(db_session)
    assert count == 1

    # Verify updated to published in DB
    db_session.expire_all()
    event_after = db_session.query(OutboxEvent).filter(OutboxEvent.id == event.id).first()
    assert event_after.published_at is not None


def test_durable_idempotency_constraints(setup_db):
    db_session = setup_db

    # Create merchant and customer
    merchant = Merchant(id="m1", name="M1")
    customer = Customer(id="c1", email="c1@t.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    # 1. Unique provider_event_id constraint
    pe1 = PaymentEvent(id="e1", amount=100, provider_event_id="unique_evt_1", merchant_id="m1", customer_id="c1", event_type="FAILED_PAYMENT")
    pe2 = PaymentEvent(id="e2", amount=200, provider_event_id="unique_evt_1", merchant_id="m1", customer_id="c1", event_type="FAILED_PAYMENT")
    
    db_session.add(pe1)
    db_session.flush()
    db_session.add(pe2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_concurrency_locking(setup_db, auth_headers):
    db_session = setup_db
    merchant_id = "m_lock"
    customer_id = "c_lock"
    
    merchant = Merchant(id=merchant_id, name="Lock Merchant", amount_threshold=5000.00, max_retries=3)
    customer = Customer(id=customer_id, merchant_id=merchant_id, email="cust_lock@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_lock",
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

    case = RecoveryCase(
        id="case_lock",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.HUMAN_REVIEW,
        current_recovery_attempt=0
    )
    db_session.add(case)
    
    action = RecoveryAction(
        id="act_lock",
        case_id=case.id,
        action_type=ActionType.RETRY_PAYMENT,
        proposed_by="AI_PLANNER",
        state=ActionState.PROPOSED
    )
    db_session.add(action)
    db_session.commit()

    # Simulate two threads attempting to approve the same case at the same time
    # Both make POST /api/cases/{case_id}/review calls simultaneously
    approve_payload = {
        "action": "APPROVE",
        "operator_id": "operator_lock",
        "notes": "Testing concurrency lock"
    }

    results = []

    def perform_review():
        # Create a new client per thread to avoid socket reuse conflicts in tests
        t_client = TestClient(app)
        resp = t_client.post(f"/api/cases/{case.id}/review", json=approve_payload, headers=auth_headers)
        results.append(resp)

    t1 = threading.Thread(target=perform_review)
    t2 = threading.Thread(target=perform_review)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One should succeed (200), and the other should fail (400) because status transitions away from HUMAN_REVIEW on first success
    status_codes = [r.status_code for r in results]
    assert 200 in status_codes
    assert 400 in status_codes
