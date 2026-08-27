import unittest
import json
import os
from pathlib import Path
from app.services.evaluator import BatchEvaluator

class TestBatchEvaluator(unittest.TestCase):
    
    def setUp(self):
        self.events_path = str(Path(__file__).parent / "synthetic_events.json")

    def test_evaluator_stratified_limit_runs(self):
        # 1. Test development mode limit 25 in backend mode
        res = BatchEvaluator.evaluate_batch(self.events_path, limit=25, stratify=True, mode="backend")
        self.assertEqual(res["total_cases"], 25)
        self.assertTrue(res["total_amount_at_risk"] > 0)
        self.assertTrue(res["recovery_rate"] >= 0.0)
        self.assertEqual(res["backend_action_agreement"], 100.0)
        self.assertEqual(len(res["cases"]), 25)
        
        out_path = str(Path(__file__).parent / "evaluation_results.json")
        with open(out_path, "w") as f:
            json.dump(res, f, indent=2)
        self.assertTrue(os.path.exists(out_path))

    def test_evaluator_council_mode_runs(self):
        ai_service_path = Path(__file__).parent.parent.parent / "ai-service"
        if not ai_service_path.exists():
            self.skipTest("ai-service directory not found (skipping council mode test in CI environment)")
        # 2. Test AI Council mode evaluation limit 15 (Runs transactions through 5-Agent LangGraph)
        res = BatchEvaluator.evaluate_batch(self.events_path, limit=15, stratify=True, mode="council")
        self.assertEqual(res["total_cases"], 15)
        self.assertTrue(res["council_action_agreement"] >= 0.0)
        self.assertEqual(len(res["cases"]), 15)

    def test_evaluator_full_batch_run(self):
        # 3. Test full evaluation mode (1000 events)
        res = BatchEvaluator.evaluate_batch(self.events_path, limit=1000, stratify=False, mode="backend")
        self.assertEqual(res["total_cases"], 1000)
        self.assertTrue(res["guard_block_rate"] >= 0)
        self.assertTrue(res["human_escalation_rate"] > 0)

    def test_evaluator_metrics_outcome_classes(self):
        # 4. Assert distinct semantics for all four outcome classes
        # Generate mock events pathway matching distinct states
        mock_events = [
            {
                "id": "mock-event-1",
                "amount": 100.0,
                "currency": "INR",
                "customer_id": "cust-1",
                "customer_history": {"risk_score": 0.1, "success_rate": 0.9},
                "recovery_context": {"attempt_number": 0, "max_retries": 3, "has_active_action": False},
                "ground_truth": {"recommended_action": "RETRY_PAYMENT", "action_success": True, "recovered_amount": 100.0}
            },
            {
                "id": "mock-event-2",
                "amount": 200.0,
                "currency": "INR",
                "customer_id": "cust-2",
                "customer_history": {"risk_score": 0.1, "success_rate": 0.9},
                "recovery_context": {"attempt_number": 3, "max_retries": 3, "has_active_action": False}, # Exceeds attempts -> BLOCKED
                "ground_truth": {"recommended_action": "RETRY_PAYMENT", "action_success": True, "recovered_amount": 200.0}
            },
            {
                "id": "mock-event-3",
                "amount": 300.0,
                "currency": "INR",
                "customer_id": "cust-3",
                "customer_history": {"risk_score": 0.1, "success_rate": 0.9},
                "recovery_context": {"attempt_number": 0, "max_retries": 3, "has_active_action": False},
                "ground_truth": {"recommended_action": "ESCALATE_TO_HUMAN", "action_success": True, "recovered_amount": 300.0}
            }
        ]
        
        mock_file = Path(__file__).parent / "mock_eval_events.json"
        with open(mock_file, "w") as f:
            json.dump(mock_events, f)
            
        try:
            res = BatchEvaluator.evaluate_batch(str(mock_file), limit=10, stratify=False, mode="backend")
            self.assertEqual(res["total_cases"], 3)
            # Case 1 recovers, Case 2 blocks (zero revenue), Case 3 escalates (zero revenue)
            self.assertEqual(res["revenue_recovered"], 100.0) # Only Case 1
            self.assertEqual(res["guard_block_rate"], 33.33) # 1/3 blocked
            self.assertEqual(res["human_escalation_rate"], 33.33) # 1/3 escalated
            
            # Verify no rate conflation
            case_1 = next(c for c in res["cases"] if c["case_id"] == "mock-event-1")
            case_2 = next(c for c in res["cases"] if c["case_id"] == "mock-event-2")
            case_3 = next(c for c in res["cases"] if c["case_id"] == "mock-event-3")
            
            self.assertEqual(case_1["resulting_status"], "RECOVERED")
            self.assertEqual(case_2["resulting_status"], "BLOCKED")
            self.assertEqual(case_3["resulting_status"], "HUMAN_REVIEW")
            
        finally:
            if mock_file.exists():
                os.remove(mock_file)

if __name__ == "__main__":
    unittest.main()
