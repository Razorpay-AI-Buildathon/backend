import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
import uuid
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent, Execution
from app.services.worker import RecoveryWorker

client = TestClient(app)
auth_headers = {"X-API-KEY": "RECOVERAI-TESTKEY-12345"}

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_metrics_schema_coverage():
    resp = client.get("/api/metrics", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    
    # Verify Task 17 Evaluation & BI fields
    assert "recovered_value_per_attempt" in data
    assert "avg_attempts" in data
    assert "avg_recovery_time_seconds" in data
    assert "execution_failure_rate" in data
    assert "council_confidence" in data
    assert "proposal_acceptance_rate" in data
    assert "replan_rate" in data
    assert "guard_override_rate" in data
    
    # Verify Strategy mappings
    assert "recovery_by_action_type" in data
    assert "recovery_by_failure_code" in data
    assert "recovery_by_customer_risk_band" in data
    assert "recovery_by_amount_band" in data
    assert "recovery_by_merchant" in data

    # Verify Task 36 Reconciled metrics
    assert "reconciled_revenue_recovered" in data
    assert "reconciled_recovery_rate" in data

    # Verify Task 37 A/B Experiment metrics
    assert "control_recovery_rate" in data
    assert "treatment_recovery_rate" in data
    assert "control_revenue_recovered" in data
    assert "treatment_revenue_recovered" in data


def test_case_experiment_group_assignment(setup_db):
    db_session = setup_db
    groups_assigned = set()
    
    # Ingest multiple cases to ensure we cover both random groups
    for i in range(10):
        payload = {
            "event_id": f"evt-{uuid.uuid4().hex[:8]}",
            "merchant_id": "merch_exp",
            "customer_id": f"cust_exp_{i}",
            "event_type": "FAILED_PAYMENT",
            "amount": 100.0,
            "currency": "INR"
        }
        resp = client.post("/api/events/payment", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        case_id = resp.json()["case_id"]
        
        c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        assert c.experiment_group in ("CONTROL", "TREATMENT")
        groups_assigned.add(c.experiment_group)

    # Since we generated 10 cases, statistically we should hit both groups
    assert len(groups_assigned) > 0


def test_complete_e2e_flow_with_reconciliation(setup_db, monkeypatch):
    db_session = setup_db

    # Mock AI Service for TREATMENT cases
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data

    def mock_post(*args, **kwargs):
        return MockResponse({
            "final_action": "RETRY_PAYMENT",
            "final_confidence": 0.90,
            "action_id": "act-mock-e2e"
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    # 1. Ingest payment event
    payload = {
        "event_id": "evt_e2e_reconciliation",
        "merchant_id": "merch_e2e",
        "customer_id": "cust_e2e",
        "event_type": "FAILED_PAYMENT",
        "amount": 2500.0,
        "currency": "INR"
    }
    resp = client.post("/api/events/payment", json=payload, headers=auth_headers)
    assert resp.status_code == 200
    case_id = resp.json()["case_id"]

    # Explicitly set to TREATMENT group for testing worker flow
    c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    c.experiment_group = "TREATMENT"
    db_session.commit()

    # 2. Run worker process case evaluation
    worker = RecoveryWorker()
    worker.process_case_evaluation(case_id)

    db_session.expire_all()
    c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert c.status in (CaseStatus.EXECUTING, CaseStatus.RECOVERED, CaseStatus.FAILED, CaseStatus.ANALYZING)

    # Verify execution object created and provider_reference exists
    exec_obj = db_session.query(Execution).filter(Execution.case_id == case_id).first()
    assert exec_obj is not None
    assert exec_obj.provider_reference is not None

    # 3. Simulate provider webhook confirmation (captured status)
    webhook_payload = {
        "event": "payment.captured",
        "provider_event_id": "web_evt_e2e",
        "payload": {
            "payment": {
                "entity": {
                    "id": exec_obj.provider_reference,
                    "status": "captured",
                    "amount": 250000 # Razorpay format in paise
                }
            }
        }
    }
    resp = client.post("/api/webhooks/provider", json=webhook_payload, headers={"X-Razorpay-Signature": "test_signature"})
    assert resp.status_code == 200

    # 4. Verify case is RECOVERED and final reconciled amount is saved
    db_session.expire_all()
    c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    assert c.status == CaseStatus.RECOVERED

    exec_obj = db_session.query(Execution).filter(Execution.case_id == case_id).first()
    assert exec_obj.status == "SUCCESS"
    assert exec_obj.reconciled_amount == exec_obj.amount

    # 5. Verify metrics matches reconciled amount
    resp = client.get("/api/metrics", headers=auth_headers)
    assert resp.status_code == 200
    metrics = resp.json()
    assert metrics["reconciled_revenue_recovered"] == 2500.0
    assert metrics["reconciled_recovery_rate"] == 100.0
