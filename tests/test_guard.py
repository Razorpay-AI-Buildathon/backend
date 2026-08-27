import unittest
import json
from pathlib import Path
from app.services.action_guard import ActionGuard
from app.services.scoring import calculate_erv, calculate_priority_score
from app.services.executor import ExecutionSimulator

class TestRecoverAISafetyAndScoring(unittest.TestCase):
    
    def setUp(self):
        # Resolve portable path relative to file directory
        events_path = Path(__file__).parent / "synthetic_events.json"
        with open(events_path, "r") as f:
            self.events = json.load(f)

    def test_run_against_user_synthetic_events(self):
        for idx, event in enumerate(self.events):
            amount = event["amount"]
            currency = event["currency"]
            failure_code = event.get("failure_code")
            customer_history = event["customer_history"]
            recovery_context = event["recovery_context"]
            
            erv = calculate_erv(
                amount=amount,
                currency=currency,
                failure_code=failure_code,
                history_success_rate=customer_history["success_rate"],
                attempt=recovery_context["attempt_number"]
            )
            self.assertTrue(erv >= 0)
            
            priority = calculate_priority_score(
                amount=amount,
                currency=currency,
                failure_code=failure_code,
                history_success_rate=customer_history["success_rate"],
                attempt=recovery_context["attempt_number"]
            )
            self.assertTrue(0 <= priority <= 100)
            
            approved, token, violations = ActionGuard.validate_action(
                action_type=event["ground_truth"]["recommended_action"],
                amount=amount,
                currency=currency,
                current_attempts=recovery_context["attempt_number"],
                max_retries=recovery_context["max_retries"],
                amount_threshold_inr=5000.0,
                has_active_action=recovery_context["has_active_action"],
                case_id=event["id"],
                event_id=event["id"],
                action_id="action-id"
            )
            
            if approved:
                self.assertTrue(token.startswith("AUTH-EXEC-"))
                self.assertEqual(len(violations), 0)
                
                # Check execution simulator behavior for approved actions
                res = ExecutionSimulator.execute_action(
                    action_type=event["ground_truth"]["recommended_action"],
                    amount=amount,
                    currency=currency,
                    is_guard_approved=approved,
                    auth_token=token,
                    ground_truth={
                        **event["ground_truth"],
                        "case_id": event["id"],
                        "event_id": event["id"],
                        "action_id": "action-id"
                    }
                )
                self.assertTrue(res["execution_id"].startswith("SIM-") or res["execution_id"].startswith("EXEC-"))
                
                if event["ground_truth"]["recommended_action"] not in ("DO_NOTHING", "ESCALATE_TO_HUMAN"):
                    self.assertEqual(res["status"], "SUCCESS" if event["ground_truth"]["action_success"] else "FAILED")
                else:
                    self.assertEqual(res["status"], "SUCCESS")
            else:
                self.assertEqual(token, "")
                self.assertTrue(len(violations) > 0)
                
                # Assert blocked actions throw permission error
                with self.assertRaises(PermissionError):
                    ExecutionSimulator.execute_action(
                        action_type=event["ground_truth"]["recommended_action"],
                        amount=amount,
                        currency=currency,
                        is_guard_approved=approved
                    )

    def test_action_guard_blocks_unauthorized_action(self):
        approved, token, violations = ActionGuard.validate_action(
            action_type="REFUND_TRANSACTION",
            amount=100.0,
            currency="INR",
            current_attempts=0,
            max_retries=3,
            amount_threshold_inr=5000.0,
            case_id="case-1",
            event_id="event-1",
            action_id="action-1"
        )
        self.assertFalse(approved)
        self.assertIn("not supported", violations[0])

        with self.assertRaises(PermissionError):
            ExecutionSimulator.execute_action("REFUND_TRANSACTION", 100.0, "INR", approved, auth_token="token")

    def test_action_guard_blocks_amount_threshold_usd(self):
        approved, token, violations = ActionGuard.validate_action(
            action_type="RETRY_PAYMENT",
            amount=100.0,
            currency="USD",
            current_attempts=0,
            max_retries=3,
            amount_threshold_inr=5000.0,
            case_id="case-1",
            event_id="event-1",
            action_id="action-1"
        )
        self.assertFalse(approved)
        self.assertTrue(any("exceeds security threshold" in v for v in violations))

        with self.assertRaises(PermissionError):
            ExecutionSimulator.execute_action("RETRY_PAYMENT", 100.0, "USD", approved, auth_token="token")

    def test_action_guard_frequency_cap_cooldown(self):
        # 12 hours diff fails the 24 hour cooldown window constraint
        approved, token, violations = ActionGuard.validate_action(
            action_type="SEND_PAYMENT_REMINDER",
            amount=100.0,
            currency="INR",
            current_attempts=0,
            max_retries=3,
            amount_threshold_inr=5000.0,
            last_contact_at_str="2026-08-25T10:00:00Z",
            now_str="2026-08-25T22:00:00Z",
            contact_cooldown_hours=24,
            case_id="case-1",
            event_id="event-1",
            action_id="action-1"
        )
        self.assertFalse(approved)
        self.assertTrue(any("Cooldown active" in v for v in violations))

    def test_action_guard_confidence_threshold(self):
        # Low confidence fails check
        approved, token, violations = ActionGuard.validate_action(
            action_type="RETRY_PAYMENT",
            amount=100.0,
            currency="INR",
            current_attempts=0,
            max_retries=3,
            amount_threshold_inr=5000.0,
            planner_confidence=0.40,
            case_id="case-1",
            event_id="event-1",
            action_id="action-1",
            min_confidence_threshold=0.55
        )
        self.assertFalse(approved)
        self.assertTrue(any("below minimum allowed threshold" in v for v in violations))

        with self.assertRaises(PermissionError):
            ExecutionSimulator.execute_action("RETRY_PAYMENT", 100.0, "INR", approved, auth_token="token")

if __name__ == "__main__":
    unittest.main()
