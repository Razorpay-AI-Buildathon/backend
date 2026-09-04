import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import uuid
from datetime import datetime, timedelta

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent, Execution, MerchantRecoveryPolicy, AiDecision, DeadLetterJob
from app.services.worker import RecoveryWorker

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture(scope="function")
def auth_headers():
    return {"X-API-KEY": "RECOVERAI-TESTKEY-12345"}


def test_merchant_policy_rules(setup_db, auth_headers):
    db_session = setup_db
    merchant_id = "merch_policy_test"
    customer_id = "cust_policy_test"
    
    merchant = Merchant(id=merchant_id, name="Policy Merchant")
    # Customer risk score is 85.0 (High Risk)
    customer = Customer(id=customer_id, merchant_id=merchant_id, email="risk@test.com", risk_score=85.0)
    db_session.add_all([merchant, customer])
    db_session.flush()

    # Create Merchant Recovery Policy (risk threshold = 80.0)
    policy = MerchantRecoveryPolicy(
        merchant_id=merchant_id,
        max_attempts=3,
        retry_backoff=300,
        amount_threshold=5000.00,
        allowed_actions=["RETRY_PAYMENT"],
        risk_threshold=80.0,
        enabled=True,
        version=1
    )
    db_session.add(policy)
    db_session.flush()

    # Ingest event which will trigger worker logic
    event = PaymentEvent(
        id="evt_policy",
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
        id="case_policy",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.IDENTIFIED,
        current_recovery_attempt=0
    )
    db_session.add(case)
    db_session.commit()

    # Run case evaluation manually in worker
    worker = RecoveryWorker()
    worker.process_case_evaluation(case.id)

    db_session.expire_all()
    updated_case = db_session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
    
    # Assert case escalated to HUMAN_REVIEW due to customer risk policy violation
    assert updated_case.status == CaseStatus.HUMAN_REVIEW
    assert updated_case.policy_id == policy.id
    assert updated_case.policy_version == 1

    # Verify audit event logged
    audit = db_session.query(AuditEvent).filter(AuditEvent.case_id == case.id, AuditEvent.event_type == "HUMAN_ESCALATION").first()
    assert audit is not None
    assert "exceeds policy threshold" in audit.metadata_json.get("reason", "")


def test_ai_failure_isolation(setup_db):
    db_session = setup_db
    merchant_id = "merch_ai_fail"
    customer_id = "cust_ai_fail"
    
    merchant = Merchant(id=merchant_id, name="AI Fail Merchant")
    customer = Customer(id=customer_id, merchant_id=merchant_id, email="aifail@test.com", risk_score=10.0)
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_ai_fail",
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
        id="case_ai_fail",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.IDENTIFIED,
        current_recovery_attempt=0
    )
    db_session.add(case)
    db_session.commit()

    # Mock AI Service failing by setting bad URL
    os.environ["AI_SERVICE_URL"] = "http://invalid-non-existent-domain:8999"

    worker = RecoveryWorker()
    worker.process_case_evaluation(case.id)

    db_session.expire_all()
    updated_case = db_session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
    
    # Verify case transitions to HUMAN_REVIEW rather than bypassing or failing silently
    assert updated_case.status == CaseStatus.HUMAN_REVIEW

    audit = db_session.query(AuditEvent).filter(AuditEvent.case_id == case.id, AuditEvent.event_type == "HUMAN_ESCALATION").first()
    assert audit is not None
    assert audit.decision_source == "ACTION_GUARD"


def test_dead_letter_queue(setup_db):
    db_session = setup_db
    worker = RecoveryWorker()
    
    # Simulate worker processing failure loop
    task = {
        "job_id": "job_retry_fail",
        "task_name": "evaluate_case",
        "payload": {
            "case_id": "non_existent_case_id",
            "retries": 2 # This is the 3rd attempt
        }
    }
    
    # Execute run logic with the mock task
    worker.move_to_dlq("non_existent_case_id", task, "Case details not found", 3)
    
    # Verify DeadLetterJob logged in DB
    dlq_records = db_session.query(DeadLetterJob).all()
    assert len(dlq_records) == 1
    assert dlq_records[0].case_id == "non_existent_case_id"
    assert dlq_records[0].retry_count == 3
    assert dlq_records[0].failure_reason == "Case details not found"


def test_case_timeout_detection(setup_db, auth_headers):
    db_session = setup_db
    merchant_id = "merch_timeout"
    customer_id = "cust_timeout"
    
    merchant = Merchant(id=merchant_id, name="Timeout Merchant")
    customer = Customer(id=customer_id, merchant_id=merchant_id, email="timeout@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_timeout",
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

    # Case in EXECUTING state
    case = RecoveryCase(
        id="case_timeout",
        event_id=event.id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        status=CaseStatus.EXECUTING,
        current_recovery_attempt=1,
        updated_at=datetime.utcnow() - timedelta(minutes=45) # Stuck for 45 minutes
    )
    db_session.add(case)
    db_session.commit()

    # Trigger timeout detector route
    resp = client.post("/api/cases/detect-timeouts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["processed"] == 1

    db_session.expire_all()
    updated_case = db_session.query(RecoveryCase).filter(RecoveryCase.id == case.id).first()
    
    # Stuck case must transition to HUMAN_REVIEW
    assert updated_case.status == CaseStatus.HUMAN_REVIEW
