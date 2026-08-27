import json
import random
import uuid
from datetime import datetime, timedelta, timezone

def generate_synthetic_dataset_v2(output_path: str):
    # Domain-consistent failure code taxonomy per recovery strategy
    taxonomy = {
        "FAILED_PAYMENT": [
            ("insufficient_funds", 0.40, 1.0),
            ("bank_timeout", 0.85, 1.0),
            ("network_error", 0.80, 1.0),
            ("card_expired", 0.15, 1.0),
            ("card_declined", 0.25, 1.0),
            ("authentication_failed", 0.50, 1.0),
            ("payment_method_invalid", 0.10, 1.0)
        ],
        "CHECKOUT_ABANDONMENT": [
            ("user_abandoned", 0.35, 1.2),
            ("payment_timeout", 0.60, 1.1),
            ("payment_method_error", 0.55, 1.1),
            ("session_expired", 0.40, 1.0)
        ],
        "SUBSCRIPTION_FAILURE": [
            ("card_declined", 0.30, 1.0),
            ("insufficient_funds", 0.45, 1.0),
            ("payment_method_expired", 0.15, 1.0),
            ("recurring_payment_failed", 0.50, 1.0)
        ],
        "OVERDUE_INVOICE": [
            ("overdue", 0.60, 1.5),
            ("partially_paid", 0.75, 1.3),
            ("payment_promise_broken", 0.50, 1.4),
            ("disputed", 0.20, 1.0)
        ]
    }
    
    currencies = ["INR", "USD"]
    events = []
    
    # Generate 100 cases with ground_truth outcomes
    # 35 Failed Payments, 25 Checkouts, 20 Subscriptions, 20 Invoices
    distributions = [
        ("FAILED_PAYMENT", 35),
        ("CHECKOUT_ABANDONMENT", 25),
        ("SUBSCRIPTION_FAILURE", 20),
        ("OVERDUE_INVOICE", 20)
    ]
    
    for event_type, count in distributions:
        for i in range(count):
            failure_code, base_prob, urgency = random.choice(taxonomy[event_type])
            currency = random.choice(currencies)
            
            # Amount range
            if event_type == "OVERDUE_INVOICE":
                amount = round(random.uniform(10000, 150000), 2)
            else:
                amount = round(random.uniform(200, 15000), 2)
                
            success_rate = round(random.uniform(0.3, 0.95), 2)
            risk_score = round(random.uniform(0.05, 0.75), 2)
            
            # Outcome simulation logic
            prob = base_prob * success_rate
            is_recoverable = prob > 0.30
            recovered = is_recoverable and (random.random() < prob)
            
            # Default action mapping
            rec_action = "DO_NOTHING"
            if event_type == "FAILED_PAYMENT":
                rec_action = "SEND_PAYMENT_REMINDER" if failure_code == "insufficient_funds" else "RETRY_PAYMENT"
            elif event_type == "CHECKOUT_ABANDONMENT":
                rec_action = "SEND_CHECKOUT_RECOVERY_MESSAGE"
            elif event_type == "SUBSCRIPTION_FAILURE":
                rec_action = "RETRY_SUBSCRIPTION"
            elif event_type == "OVERDUE_INVOICE":
                rec_action = "SEND_INVOICE_REMINDER"
                
            events.append({
                "id": str(uuid.uuid4()),
                "event_type": event_type,
                "amount": amount,
                "currency": currency,
                "failure_code": failure_code,
                "customer_history": {
                    "email": f"cust_{event_type.lower()}_{i}@example.com",
                    "risk_score": risk_score,
                    "success_rate": success_rate
                },
                "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=random.randint(10, 20000))).isoformat(),
                "ground_truth": {
                    "recoverability_probability": round(prob, 2),
                    "recoverable": is_recoverable,
                    "recommended_action": rec_action,
                    "action_success": recovered,
                    "recovered": recovered,
                    "recovered_amount": amount if recovered else 0.0,
                    "outcome": "RECOVERED" if recovered else "FAILED"
                }
            })
            
    with open(output_path, "w") as f:
        json.dump(events, f, indent=2)
    print(f"Successfully generated 100 synthetic events at {output_path}")

if __name__ == "__main__":
    from pathlib import Path
    out_path = Path(__file__).parent / "synthetic_events.json"
    generate_synthetic_dataset_v2(str(out_path))
