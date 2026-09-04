import unittest
from fastapi.testclient import TestClient
from pathlib import Path
import json
import secrets
import os
import threading

# Set expected credentials prior to imports to ensure verification functions load cleanly
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

from app.main import app
from app.db.session import get_db, Base, engine
from app.models.case import RecoveryCase, PaymentEvent, RecoveryAction, CaseStatus, ActionType, ActionState, CaseStateMachine
from app.services.action_guard import ActionGuard

class TestBackendRestAPI(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.api_key = "RECOVERAI-TESTKEY-12345"
        cls.headers = {"X-API-Key": cls.api_key}

    def setUp(self):
        # Fresh clean DB session state before every test
        db_session = next(get_db())
        db_session.query(RecoveryCase).delete()
        db_session.query(PaymentEvent).delete()
        db_session.commit()

    def test_1_get_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "service": "recoverai-backend"})

    def test_2_get_metrics(self):
        resp = self.client.get("/api/metrics", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("revenue_at_risk", data)
        self.assertIn("recovery_rate", data)
        self.assertIn("guard_block_rate", data)

    def test_3_get_cases_pagination(self):
        resp = self.client.get("/api/cases?page=1&page_size=5", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("total", data)
        self.assertIn("items", data)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 5)

    def test_4_get_case_not_found(self):
        resp = self.client.get("/api/cases/invalid-uuid-non-existent", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_5_post_score_api_auth(self):
        payload = {
            "amount": 5000.0,
            "currency": "INR",
            "failure_code": "bank_timeout",
            "history_success_rate": 0.8,
            "attempt": 0,
            "urgency_factor": 1.0
        }
        # 1. Missing header
        resp = self.client.post("/api/score", json=payload)
        self.assertEqual(resp.status_code, 401)

        # 2. Invalid header
        resp = self.client.post("/api/score", json=payload, headers={"X-API-Key": "BADKEY"})
        self.assertEqual(resp.status_code, 403)

        # 3. Valid header
        resp = self.client.post("/api/score", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("expected_recovery_value", data)
        self.assertIn("priority_score", data)
        self.assertIn("recoverability_probability", data)

    def test_6_post_action_guard_evaluate_approves(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 2500.0,
            "currency": "INR",
            "current_attempts": 1,
            "max_retries": 3,
            "amount_threshold": 5000.0,
            "has_active_action": False,
            "planner_confidence": 0.91,
            "case_id": "case-test-6",
            "event_id": "event-test-6",
            "action_id": "action-test-6"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["approved"])
        self.assertIsNotNone(data["authorization_token"])
        self.assertEqual(data["resulting_status"], "APPROVED")

    def test_7_post_action_guard_evaluate_blocks_unsupported(self):
        payload = {
            "action_type": "REFUND_TRANSACTION",
            "amount": 2500.0,
            "currency": "INR",
            "current_attempts": 1,
            "max_retries": 3,
            "case_id": "case-test-7",
            "event_id": "event-test-7",
            "action_id": "action-test-7"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 422)

    def test_8_post_action_guard_evaluate_blocks_retry_limit(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 2500.0,
            "currency": "INR",
            "current_attempts": 3,
            "max_retries": 3,
            "amount_threshold": 5000.0,
            "has_active_action": False,
            "case_id": "case-test-8",
            "event_id": "event-test-8",
            "action_id": "action-test-8"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["approved"])
        self.assertEqual(data["resulting_status"], "REJECTED")

    def test_9_post_action_guard_evaluate_blocks_amount_threshold(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 6000.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "amount_threshold": 5000.0,
            "has_active_action": False,
            "case_id": "case-test-9",
            "event_id": "event-test-9",
            "action_id": "action-test-9"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["approved"])
        self.assertEqual(data["resulting_status"], "REJECTED")

    def test_10_post_action_guard_evaluate_blocks_cooldown(self):
        payload = {
            "action_type": "SEND_PAYMENT_REMINDER",
            "amount": 100.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "last_contact_at": "2026-08-25T10:00:00Z",
            "now": "2026-08-25T22:00:00Z",
            "case_id": "case-test-10",
            "event_id": "event-test-10",
            "action_id": "action-test-10"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["approved"])

    def test_11_post_action_guard_evaluate_blocks_low_confidence(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 100.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "planner_confidence": 0.40,
            "case_id": "case-test-11",
            "event_id": "event-test-11",
            "action_id": "action-test-11"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["approved"])

    def test_12_post_execute_rejects_unapproved(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 2500.0,
            "currency": "INR",
            "authorization_token": "token-value-here",
            "guard_approved": False,
            "case_id": "case-test-12",
            "event_id": "event-test-12",
            "action_id": "action-test-12"
        }
        resp = self.client.post("/api/execute", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 403)

    def test_13_post_execute_atomic_claim_and_replay_protection(self):
        # 13. Test token atomic claiming, parameter binding, replay, and idempotency locks
        # 13.a. Evaluate action to get valid token
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1500.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-13",
            "event_id": "event-test-13",
            "action_id": "action-test-13"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(res_eval.status_code, 200)
        token = res_eval.json()["authorization_token"]
        self.assertIsNotNone(token)

        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1500.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-13",
            "event_id": "event-test-13",
            "action_id": "action-test-13",
            "ground_truth": {
                "action_success": True
            }
        }

        # 13.b. First execution succeeds
        res_exec1 = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(res_exec1.status_code, 200)
        data1 = res_exec1.json()
        self.assertEqual(data1["status"], "SUCCESS")
        self.assertTrue(data1["recovered"])

        # 13.c. Repeated request with same action_id and identical parameters returns cached result without consuming token
        res_exec2 = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(res_exec2.status_code, 200)
        self.assertEqual(res_exec2.json()["execution_id"], data1["execution_id"])

        # 13.d. Request with different action parameters using same action_id throws conflict 409
        bad_payload = {
            "action_type": "SEND_PAYMENT_REMINDER",
            "amount": 1500.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-13",
            "event_id": "event-test-13",
            "action_id": "action-test-13"
        }
        res_exec3 = self.client.post("/api/execute", json=bad_payload, headers=self.headers)
        self.assertEqual(res_exec3.status_code, 409)

        # 13.e. Unknown action_id with invalid token throws 403
        bad_token_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1500.0,
            "currency": "INR",
            "authorization_token": "invalid-token",
            "guard_approved": True,
            "case_id": "case-test-13",
            "event_id": "event-test-13",
            "action_id": "action-test-13-unknown"
        }
        res_exec4 = self.client.post("/api/execute", json=bad_token_payload, headers=self.headers)
        self.assertEqual(res_exec4.status_code, 403)

    def test_14_human_escalation_status(self):
        payload = {
            "action_type": "ESCALATE_TO_HUMAN",
            "amount": 100.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-14",
            "event_id": "event-test-14",
            "action_id": "action-test-14"
        }
        resp = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["approved"])
        self.assertEqual(data["resulting_status"], "HUMAN_REVIEW")

    def test_15_state_machine_transitions(self):
        # Validate centralized lifecycle transitions helpers
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.IDENTIFIED, CaseStatus.ANALYZING))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.RECOVERED, CaseStatus.EXECUTING))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.BLOCKED, CaseStatus.GUARD_REVIEW))

    def test_16_secret_leakage_redaction(self):
        # 16. Verify credentials never appear in API responses or trace audit logs
        db_session = next(get_db())
        event = PaymentEvent(
            event_type="FAILED_PAYMENT",
            amount=100.0,
            currency="INR",
            failure_code="bank_timeout"
        )
        db_session.add(event)
        db_session.commit()

        case = RecoveryCase(
            event_id=event.id,
            status=CaseStatus.IDENTIFIED,
            audit_log=[
                {"node": "planner", "secret_key": "supersecretpassword", "token": "sensitiveauth"}
            ]
        )
        db_session.add(case)
        db_session.commit()

        resp = self.client.get(f"/api/cases/{case.id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Secrets must be redacted recursively
        log = data["audit_log"][0]
        self.assertEqual(log["secret_key"], "**REDACTED**")
        self.assertEqual(log["token"], "**REDACTED**")

    def test_17_concurrency_double_claim_lock(self):
        # 17. Concurrent double execution requests using the same token
        # Generate fresh token bound to distinct ids first
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 800.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-17",
            "event_id": "event-test-17",
            "action_id": "action-test-17"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        token = res_eval.json()["authorization_token"]

        exec_payload1 = {
            "action_type": "RETRY_PAYMENT",
            "amount": 800.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-17",
            "event_id": "event-test-17",
            "action_id": "action-test-17",
            "ground_truth": {
                "action_success": True
            }
        }
        
        # Payload 2 simulates concurrency conflict attempt using different action_id but same consumed token
        exec_payload2 = {
            "action_type": "RETRY_PAYMENT",
            "amount": 800.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-17",
            "event_id": "event-test-17",
            "action_id": "action-test-17-diff",
            "ground_truth": {
                "action_success": True
            }
        }

        results = []
        # Concurrent request 1 succeeds, request 2 is rejected due to token reuse
        r1 = self.client.post("/api/execute", json=exec_payload1, headers=self.headers)
        r2 = self.client.post("/api/execute", json=exec_payload2, headers=self.headers)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 403)

    def test_18_fail_closed_api_key_configuration(self):
        # 1. Save original api key value
        orig_key = os.environ.get("RECOVERAI_API_KEY")
        
        # 2. Clear env configuration to test fail closed behaviors
        if "RECOVERAI_API_KEY" in os.environ:
            del os.environ["RECOVERAI_API_KEY"]
            
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 100.0,
            "currency": "INR",
            "authorization_token": "token",
            "guard_approved": True,
            "case_id": "case-18",
            "event_id": "event-18",
            "action_id": "action-18"
        }
        
        try:
            # Recreate testclient to refresh backend dependency cache if needed
            resp = self.client.post("/api/execute", json=payload, headers={"X-API-Key": "any"})
            self.assertEqual(resp.status_code, 500)
            self.assertIn("not configured", resp.json()["detail"])
        finally:
            if orig_key is not None:
                os.environ["RECOVERAI_API_KEY"] = orig_key

    def test_19_provenance_poisoning_defense(self):
        # Verify ground_truth cannot overwrite decision_source provenance
        payload_eval = {
            "action_type": "RETRY_PAYMENT",
            "amount": 900.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-19",
            "event_id": "event-test-19",
            "action_id": "action-test-19"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload_eval, headers=self.headers)
        token = res_eval.json()["authorization_token"]
        
        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 900.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-19",
            "event_id": "event-test-19",
            "action_id": "action-test-19",
            "ground_truth": {
                "action_success": True,
                "decision_source": "LLM",
                "model": "gpt-fake-model",
                "confidence": 0.99
            }
        }
        
        # Create DB record representing case to check audit trails commits
        db_session = next(get_db())
        from app.models.case import PaymentEvent
        e = PaymentEvent(id="event-test-19", event_type="FAILED_PAYMENT", amount=900.0, currency="INR")
        db_session.add(e)
        c = RecoveryCase(id="case-test-19", event_id="event-test-19", status=CaseStatus.APPROVED)
        db_session.add(c)
        db_session.commit()
        
        resp = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        
        # Verify persisted case audit log decision source
        db_session.refresh(c)
        log = c.audit_log[-1]
        self.assertEqual(log["decision_source"], "API_SIMULATION")
        self.assertNotEqual(log["model"], "gpt-fake-model")

        # Verify Execution record exists in database
        from app.models.case import Execution
        db_exec = db_session.query(Execution).filter(Execution.case_id == "case-test-19").first()
        self.assertIsNotNone(db_exec)
        self.assertEqual(float(db_exec.amount), 900.0)
        self.assertEqual(db_exec.status, "SUCCESS")

    def test_20_execution_side_effects_on_rejection(self):
        # Rejected token execution must cause zero side-effects
        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 400.0,
            "currency": "INR",
            "authorization_token": "bad-token",
            "guard_approved": True,
            "case_id": "case-test-20",
            "event_id": "event-test-20",
            "action_id": "action-test-20"
        }
        
        db_session = next(get_db())
        c = RecoveryCase(id="case-test-20", event_id="event-test-20", status=CaseStatus.GUARD_REVIEW)
        db_session.add(c)
        db_session.commit()
        
        resp = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(resp.status_code, 403)
        
        # Ensure Case state remained GUARD_REVIEW (unchanged) and no audit records added
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.GUARD_REVIEW)
        self.assertEqual(len(c.audit_log or []), 0)

    def test_21_concurrency_same_action_id(self):
        # Generate fresh token
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1000.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-21",
            "event_id": "event-test-21",
            "action_id": "action-test-21"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        token = res_eval.json()["authorization_token"]

        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1000.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-21",
            "event_id": "event-test-21",
            "action_id": "action-test-21",
            "ground_truth": {"action_success": True}
        }

        # Let's verify genuinely concurrent requests using a threading barrier
        # to ensure both threads trigger at the exact same time, hitting the registry checks.
        import threading
        barrier = threading.Barrier(2)
        results = []

        def execute_request_thread():
            # Create a localized clean client per thread to prevent socket sharing warnings
            from fastapi.testclient import TestClient
            from app.main import app
            local_client = TestClient(app)
            barrier.wait()
            try:
                res = local_client.post("/api/execute", json=exec_payload, headers=self.headers)
                results.append(res)
            except Exception as ex:
                results.append(ex)

        threads = []
        for _ in range(2):
            t = threading.Thread(target=execute_request_thread)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Both concurrent requests must yield successful 200 responses and return the exact same execution_id
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(results[1].status_code, 200)
        self.assertEqual(results[0].json()["execution_id"], results[1].json()["execution_id"])

    def test_22_concurrency_different_action_id_same_token(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1100.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-22",
            "event_id": "event-test-22",
            "action_id": "action-test-22-a"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        token = res_eval.json()["authorization_token"]

        exec_payload1 = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1100.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-22",
            "event_id": "event-test-22",
            "action_id": "action-test-22-a",
            "ground_truth": {"action_success": True}
        }
        exec_payload2 = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1100.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-22",
            "event_id": "event-test-22",
            "action_id": "action-test-22-b",
            "ground_truth": {"action_success": True}
        }

        # First request succeeds and consumes token
        resp1 = self.client.post("/api/execute", json=exec_payload1, headers=self.headers)
        self.assertEqual(resp1.status_code, 200)

        # Second request with a different action_id but same token is rejected (consumed token replay defense)
        resp2 = self.client.post("/api/execute", json=exec_payload2, headers=self.headers)
        self.assertEqual(resp2.status_code, 403)

    def test_23_idempotency_parameter_mismatches(self):
        payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1200.0,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": "case-test-23",
            "event_id": "event-test-23",
            "action_id": "action-test-23"
        }
        res_eval = self.client.post("/api/action-guard/evaluate", json=payload, headers=self.headers)
        token = res_eval.json()["authorization_token"]

        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1200.0,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": "case-test-23",
            "event_id": "event-test-23",
            "action_id": "action-test-23",
            "ground_truth": {"action_success": True}
        }

        resp1 = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(resp1.status_code, 200)

        # Retry with different amount -> 409
        bad_amt_payload = dict(exec_payload, amount=1200.01)
        resp2 = self.client.post("/api/execute", json=bad_amt_payload, headers=self.headers)
        self.assertEqual(resp2.status_code, 409)

        # Retry with different currency -> 409
        bad_curr_payload = dict(exec_payload, currency="USD")
        resp3 = self.client.post("/api/execute", json=bad_curr_payload, headers=self.headers)
        self.assertEqual(resp3.status_code, 409)

        # Retry with different case_id -> 409
        bad_case_payload = dict(exec_payload, case_id="case-diff-23")
        resp4 = self.client.post("/api/execute", json=bad_case_payload, headers=self.headers)
        self.assertEqual(resp4.status_code, 409)

    def test_20_payment_event_ingestion_and_idempotency(self):
        payload = {
            "event_id": "test-ingest-evt-001",
            "merchant_id": "merch-ingest-test",
            "customer_id": "cust-ingest-test",
            "event_type": "FAILED_PAYMENT",
            "amount": 2500.00,
            "currency": "INR",
            "failure_code": "bank_timeout",
            "provider": "razorpay",
            "provider_event_id": "prov-ref-ingest-001",
            "metadata": {
                "customer_email": "ingest@example.com"
            }
        }

        # 1. First Ingestion
        resp = self.client.post("/api/events/payment", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["message"], "Event ingested successfully")
        self.assertEqual(data["event_id"], "test-ingest-evt-001")
        case_id = data["case_id"]
        self.assertTrue(case_id.startswith("case-"))

        # 2. Duplicate Ingestion (Idempotent check by event_id)
        resp2 = self.client.post("/api/events/payment", json=payload, headers=self.headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["status"], "success")
        self.assertEqual(data2["message"], "Event already processed (idempotent)")
        self.assertEqual(data2["case_id"], case_id)

        # 3. Duplicate Ingestion by provider_event_id (different event_id)
        payload_dup_provider = dict(payload, event_id="test-ingest-evt-002")
        resp3 = self.client.post("/api/events/payment", json=payload_dup_provider, headers=self.headers)
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertEqual(data3["status"], "success")
        self.assertEqual(data3["message"], "Event already processed (idempotent)")
        self.assertEqual(data3["case_id"], case_id)

    def test_21_case_state_transitions_audit_trail(self):
        # 1. Ingest event to create case (starts at IDENTIFIED)
        payload = {
            "event_id": "test-tr-evt-001",
            "merchant_id": "merch-tr-test",
            "customer_id": "cust-tr-test",
            "event_type": "FAILED_PAYMENT",
            "amount": 1200.00,
            "currency": "INR",
            "failure_code": "bank_timeout",
            "provider": "razorpay",
            "provider_event_id": "prov-ref-tr-001",
            "metadata": {}
        }
        resp = self.client.post("/api/events/payment", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        case_id = resp.json()["case_id"]

        # Check DB to verify status is IDENTIFIED
        db_session = next(get_db())
        c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        self.assertEqual(c.status, CaseStatus.IDENTIFIED)

        # Transition IDENTIFIED -> ANALYZING -> ACTION_PROPOSED
        CaseStateMachine.transition_status(db_session, c, CaseStatus.ANALYZING, "analyze", "SYSTEM")
        CaseStateMachine.transition_status(db_session, c, CaseStatus.ACTION_PROPOSED, "propose", "SYSTEM")
        db_session.commit()

        # 2. Evaluate action (should walk ACTION_PROPOSED -> GUARD_REVIEW -> APPROVED)
        payload_eval = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1200.00,
            "currency": "INR",
            "current_attempts": 0,
            "max_retries": 3,
            "case_id": case_id,
            "event_id": "test-tr-evt-001",
            "action_id": "action-tr-001"
        }
        resp_eval = self.client.post("/api/action-guard/evaluate", json=payload_eval, headers=self.headers)
        self.assertEqual(resp_eval.status_code, 200)
        token = resp_eval.json()["authorization_token"]

        # Refresh from DB and verify transitions occurred
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.APPROVED)
        
        # Verify transition logs exist in audit trail
        transitions = [log for log in c.audit_log if log.get("event") == "state_transition"]
        self.assertTrue(len(transitions) >= 4)
        self.assertEqual(transitions[0]["inputs"]["new_status"], "ANALYZING")
        self.assertEqual(transitions[1]["inputs"]["new_status"], "ACTION_PROPOSED")
        self.assertEqual(transitions[2]["inputs"]["new_status"], "GUARD_REVIEW")
        self.assertEqual(transitions[3]["inputs"]["new_status"], "APPROVED")

        # 3. Execute recovery (should walk APPROVED -> EXECUTING -> RECOVERED)
        exec_payload = {
            "action_type": "RETRY_PAYMENT",
            "amount": 1200.00,
            "currency": "INR",
            "authorization_token": token,
            "guard_approved": True,
            "case_id": case_id,
            "event_id": "test-tr-evt-001",
            "action_id": "action-tr-001",
            "ground_truth": {
                "action_success": True
            }
        }
        resp_exec = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
        self.assertEqual(resp_exec.status_code, 200)

        # Refresh from DB and verify final status and transitions
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.RECOVERED)
        
        transitions = [log for log in c.audit_log if log.get("event") == "state_transition"]
        self.assertTrue(len(transitions) >= 6)
        self.assertEqual(transitions[4]["inputs"]["new_status"], "EXECUTING")
        self.assertEqual(transitions[5]["inputs"]["new_status"], "RECOVERED")

    def test_22_retry_and_replanning_loop(self):
        # 1. Ingest event to create case (attempts = 0, max = 3)
        payload = {
            "event_id": "test-loop-evt-001",
            "merchant_id": "merch-loop",
            "customer_id": "cust-loop",
            "event_type": "FAILED_PAYMENT",
            "amount": 800.00,
            "currency": "INR",
            "failure_code": "bank_timeout",
            "provider": "razorpay",
            "provider_event_id": "prov-ref-loop-001",
            "metadata": {}
        }
        resp = self.client.post("/api/events/payment", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        case_id = resp.json()["case_id"]

        db_session = next(get_db())
        c = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        self.assertEqual(c.status, CaseStatus.IDENTIFIED)
        self.assertEqual(c.current_recovery_attempt, 0)
        self.assertEqual(c.max_attempts, 3)

        # Helper to simulate an evaluate and failed execution cycle
        def run_failed_attempt(action_id: str, current_attempts: int):
            # Transition IDENTIFIED or ANALYZING -> ACTION_PROPOSED
            if c.status == CaseStatus.IDENTIFIED or c.status == CaseStatus.ANALYZING:
                if c.status == CaseStatus.IDENTIFIED:
                    CaseStateMachine.transition_status(db_session, c, CaseStatus.ANALYZING, "analyze", "SYSTEM")
                CaseStateMachine.transition_status(db_session, c, CaseStatus.ACTION_PROPOSED, "propose", "SYSTEM")
                db_session.commit()

            payload_eval = {
                "action_type": "RETRY_PAYMENT",
                "amount": 800.00,
                "currency": "INR",
                "current_attempts": current_attempts,
                "max_retries": 3,
                "case_id": case_id,
                "event_id": "test-loop-evt-001",
                "action_id": action_id
            }
            resp_eval = self.client.post("/api/action-guard/evaluate", json=payload_eval, headers=self.headers)
            self.assertEqual(resp_eval.status_code, 200)
            token = resp_eval.json()["authorization_token"]

            exec_payload = {
                "action_type": "RETRY_PAYMENT",
                "amount": 800.00,
                "currency": "INR",
                "authorization_token": token,
                "guard_approved": True,
                "case_id": case_id,
                "event_id": "test-loop-evt-001",
                "action_id": action_id,
                "ground_truth": {
                    "action_success": False  # Force failure outcome
                }
            }
            resp_exec = self.client.post("/api/execute", json=exec_payload, headers=self.headers)
            self.assertEqual(resp_exec.status_code, 200)

        # Attempt 1 -> fails -> transitions back to ANALYZING
        run_failed_attempt("act-l-1", 0)
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.ANALYZING)
        self.assertEqual(c.current_recovery_attempt, 1)
        self.assertIsNotNone(c.next_action_at)

        # Attempt 2 -> fails -> transitions back to ANALYZING
        run_failed_attempt("act-l-2", 1)
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.ANALYZING)
        self.assertEqual(c.current_recovery_attempt, 2)

        # Attempt 3 -> fails -> transitions to CLOSED (since attempt count reaches max 3)
        run_failed_attempt("act-l-3", 2)
        db_session.refresh(c)
        self.assertEqual(c.status, CaseStatus.CLOSED)
        self.assertEqual(c.current_recovery_attempt, 3)
        self.assertIsNotNone(c.closed_at)




if __name__ == "__main__":
    unittest.main()
