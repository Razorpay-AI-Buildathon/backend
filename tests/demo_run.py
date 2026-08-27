import requests
import json
import time

BACKEND_URL = "http://localhost:8000"
AI_SERVICE_URL = "http://localhost:8001"
API_KEY = "RECOVERAI-TESTKEY-12345"

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

def create_event_in_db(event_data):
    # Inserts event into DB (usually generated on payment failure webhook)
    # We will simulate this by creating a case and event in DB using the test endpoints
    # For buildathon demo populating, we can call backend endpoints
    pass

def run_demo():
    print("Starting Live End-to-End Demo Script...")
    
    # 1. Ingest Synthetic Events into Backend
    # Let's define 4 distinct scenarios
    scenarios = [
        {
            "id": "demo-case-1-retry",
            "event_type": "FAILED_PAYMENT",
            "amount": 2000.0,
            "currency": "INR",
            "failure_code": "bank_timeout",
            "customer_id": "cust-demo-1",
            "customer_risk_score": 0.15,
            "customer_payment_history_success_rate": 0.85,
            "recovery_attempt_count": 0,
            "max_retries": 3,
            "has_active_action": False,
            "previous_customer_contacts": 0
        },
        {
            "id": "demo-case-2-human-review",
            "event_type": "FAILED_PAYMENT",
            "amount": 7500.0,  # Exceeds 5000 INR threshold
            "currency": "INR",
            "failure_code": "insufficient_funds",
            "customer_id": "cust-demo-2",
            "customer_risk_score": 0.2,
            "customer_payment_history_success_rate": 0.9,
            "recovery_attempt_count": 0,
            "max_retries": 3,
            "has_active_action": False,
            "previous_customer_contacts": 0
        },
        {
            "id": "demo-case-3-retry-limit",
            "event_type": "FAILED_PAYMENT",
            "amount": 1500.0,
            "currency": "INR",
            "failure_code": "card_declined",
            "customer_id": "cust-demo-3",
            "customer_risk_score": 0.45,
            "customer_payment_history_success_rate": 0.5,
            "recovery_attempt_count": 3,  # Exceeds max retries limit (3)
            "max_retries": 3,
            "has_active_action": False,
            "previous_customer_contacts": 1
        },
        {
            "id": "demo-case-4-reminder",
            "event_type": "CHECKOUT_ABANDONMENT",
            "amount": 3500.0,
            "currency": "INR",
            "failure_code": "user_abandoned",
            "customer_id": "cust-demo-4",
            "customer_risk_score": 0.1,
            "customer_payment_history_success_rate": 0.95,
            "recovery_attempt_count": 0,
            "max_retries": 3,
            "has_active_action": False,
            "previous_customer_contacts": 0
        }
    ]

    # Clean previous evaluations to start fresh
    try:
        requests.post(f"{BACKEND_URL}/api/execute", json={}, headers=headers)
    except Exception:
        pass

    for index, sc in enumerate(scenarios):
        print(f"\n--- Running Scenario {index+1}: {sc['id']} ---")
        
        # Phase 1: Call AI Council LangGraph Service to analyze event
        print(f"1. Sending event payload to AI Council at {AI_SERVICE_URL}/analyze-event...")
        state_input = {
            "case_id": sc["id"],
            "event_id": f"evt-{sc['id']}",
            "event_type": sc["event_type"],
            "amount": sc["amount"],
            "currency": sc["currency"],
            "failure_code": sc["failure_code"],
            "customer_id": sc["customer_id"],
            "customer_risk_score": sc["customer_risk_score"],
            "customer_payment_history_success_rate": sc["customer_payment_history_success_rate"],
            "recovery_attempt_count": sc["recovery_attempt_count"],
            "max_retries": sc["max_retries"],
            "has_active_action": sc["has_active_action"],
            "last_contact_at_str": None,
            "now_str": "2026-08-27T10:00:00Z"
        }
        
        try:
            ai_resp = requests.post(f"{AI_SERVICE_URL}/analyze-event", json=state_input, timeout=10)
            ai_data = ai_resp.json()
            proposed_action = ai_data.get("final_action", "DO_NOTHING")
            confidence = ai_data.get("final_confidence", 0.0)
            reason = ai_data.get("final_reason", "")
            print(f"   [AI Council Decision]: {proposed_action} (Confidence: {confidence:.2f})")
            print(f"   [Reason]: {reason}")
        except Exception as e:
            print(f"   Error calling AI Council: {e}")
            continue

        # Phase 2: Validate Action Proposed through Action Guard on Backend
        print(f"2. Validating action through Action Guard on Backend at {BACKEND_URL}/api/action-guard/evaluate...")
        guard_input = {
            "case_id": sc["id"],
            "event_id": f"evt-{sc['id']}",
            "action_id": f"act-{sc['id']}",
            "action_type": proposed_action,
            "amount": sc["amount"],
            "currency": sc["currency"],
            "current_attempts": sc["recovery_attempt_count"],
            "max_retries": sc["max_retries"],
            "amount_threshold": 5000.0,
            "has_active_action": sc["has_active_action"],
            "last_contact_at": None,
            "now": "2026-08-27T10:00:00Z",
            "planner_confidence": confidence
        }
        
        try:
            guard_resp = requests.post(f"{BACKEND_URL}/api/action-guard/evaluate", json=guard_input, headers=headers)
            guard_data = guard_resp.json()
            approved = guard_data.get("approved", False)
            token = guard_data.get("authorization_token")
            resulting_status = guard_data.get("resulting_status")
            violations = guard_data.get("violations", [])
            print(f"   [Action Guard Status]: {resulting_status} (Approved: {approved})")
            if violations:
                print(f"   [Violations]: {violations}")
        except Exception as e:
            print(f"   Error calling Action Guard: {e}")
            continue

        # Phase 3: Execute Recovery Action on Backend
        if approved and token:
            print(f"3. Executing action on Backend simulator at {BACKEND_URL}/api/execute...")
            execute_input = {
                "action_type": proposed_action,
                "amount": sc["amount"],
                "currency": sc["currency"],
                "authorization_token": token,
                "guard_approved": approved,
                "case_id": sc["id"],
                "event_id": f"evt-{sc['id']}",
                "action_id": f"act-{sc['id']}",
                "ground_truth": {
                    "recommended_action": proposed_action,
                    "action_success": True if sc["id"] == "demo-case-1-retry" else False,
                    "recovered": True if sc["id"] == "demo-case-1-retry" else False,
                    "recovered_amount": sc["amount"] if sc["id"] == "demo-case-1-retry" else 0.0
                }
            }
            try:
                exec_resp = requests.post(f"{BACKEND_URL}/api/execute", json=execute_input, headers=headers)
                exec_data = exec_resp.json()
                print(f"   [Executor Result]: {exec_data.get('status')} (Recovered: {exec_data.get('recovered')})")
                print(f"   [Execution ID]: {exec_data.get('execution_id')}")
            except Exception as e:
                print(f"   Error calling Executor: {e}")
        else:
            print("3. Action Guard blocked/escalated execution. Skipping simulator call.")

    print("\nLive End-to-End Demo Script Completed successfully!")

if __name__ == "__main__":
    time.sleep(2)
    run_demo()
