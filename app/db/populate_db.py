import json
import os
import sys
import uuid
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone

# Add parent directory to path so app imports work
sys.path.append(str(Path(__file__).parent.parent.parent))

from app.db.session import SessionLocal, engine, Base
from app.models.case import (
    Merchant,
    Customer,
    PaymentEvent,
    RecoveryCase,
    RecoveryAction,
    CaseStatus,
    ActionType,
    ActionState
)
from app.services.scoring import calculate_erv, calculate_priority_score

def populate():
    print("Connecting to database and dropping/creating tables...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    
    events_path = Path(__file__).parent.parent.parent / "tests" / "synthetic_events.json"
    if not events_path.exists():
        print(f"Error: synthetic_events.json not found at {events_path}")
        return
        
    print(f"Reading synthetic events from {events_path}...")
    with open(events_path, "r") as f:
        events = json.load(f)
        
    print(f"Ingesting {len(events)} events into the database...")
    
    # Pre-create a default merchant
    default_merchant = Merchant(
        id="merch-default",
        name="Demo Merchant",
        razorpay_key_id="rzp_test_demo",
        razorpay_key_secret="rzp_secret_demo",
        amount_threshold=5000.00,
        max_retries=3
    )
    db.add(default_merchant)
    
    for idx, e_data in enumerate(events):
        # 1. Create Customer
        cust_history = e_data["customer_history"]
        customer = db.query(Customer).filter(Customer.email == cust_history["email"]).first()
        if not customer:
            customer = Customer(
                id=e_data.get("customer_id", f"cust-{idx}"),
                email=cust_history["email"],
                phone=cust_history.get("phone", "9999999999"),
                risk_score=Decimal(str(cust_history["risk_score"])),
                payment_history_success_rate=Decimal(str(cust_history["success_rate"]))
            )
            db.add(customer)
            db.flush()
            
        # 2. Create PaymentEvent
        amount = Decimal(str(e_data["amount"]))
        failure_code = e_data.get("failure_code")
        
        event = PaymentEvent(
            id=e_data["id"],
            event_type=e_data["event_type"],
            amount=amount,
            currency=e_data["currency"],
            failure_code=failure_code,
            payload_metadata=e_data.get("payload_metadata", {}),
            timestamp=datetime.fromisoformat(e_data["timestamp"].replace("Z", "+00:00"))
        )
        db.add(event)
        db.flush()
        
        # 3. Create RecoveryCase
        recovery_context = e_data.get("recovery_context", {})
        attempt = recovery_context.get("attempt_number", 0)
        
        # Calculate ERV and Priority Score
        erv = calculate_erv(
            amount=amount,
            currency=e_data["currency"],
            failure_code=failure_code,
            history_success_rate=cust_history["success_rate"],
            attempt=attempt
        )
        
        priority = calculate_priority_score(
            amount=amount,
            currency=e_data["currency"],
            failure_code=failure_code,
            history_success_rate=cust_history["success_rate"],
            attempt=attempt
        )
        
        # Determine status
        ground_truth = e_data.get("ground_truth", {})
        recovered = ground_truth.get("recovered", False)
        recommended_action = ground_truth.get("recommended_action")
        
        status = CaseStatus.IDENTIFIED
        if recovered:
            status = CaseStatus.RECOVERED
        elif attempt >= 3:
            status = CaseStatus.FAILED
        elif recommended_action == "ESCALATE_TO_HUMAN":
            status = CaseStatus.HUMAN_REVIEW
        elif recommended_action == "DO_NOTHING":
            status = CaseStatus.BLOCKED
            
        case = RecoveryCase(
            id=e_data["id"],
            event_id=event.id,
            status=status,
            priority_score=priority,
            expected_recovery_value=erv,
            current_recovery_attempt=attempt,
            audit_log=[
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "node": "ingestion",
                    "event": "event_received",
                    "inputs": {"event_type": event.event_type, "amount": float(amount)},
                    "outputs": {"status": "success"},
                    "decision": "IDENTIFIED",
                    "confidence": 1.0,
                    "decision_source": "SYSTEM",
                    "model": "rule_engine"
                }
            ],
            created_at=datetime.fromisoformat(e_data["timestamp"].replace("Z", "+00:00"))
        )
        db.add(case)
        db.flush()
        
        # 4. Create RecoveryAction if there was a proposed action in ground truth
        if recommended_action and recommended_action != "DO_NOTHING":
            action_state = ActionState.PROPOSED
            if status == CaseStatus.RECOVERED:
                action_state = ActionState.SUCCESSFUL
            elif status == CaseStatus.FAILED:
                action_state = ActionState.FAILED
                
            action = RecoveryAction(
                case_id=case.id,
                action_type=ActionType(recommended_action),
                proposed_by="AI_Council",
                state=action_state,
                authorization_token=f"AUTH-EXEC-{uuid.uuid4().hex[:12].upper()}",
                action_id=f"act-{case.id[:8]}",
                execution_id=f"exec-{case.id[:8]}" if recovered else None,
                created_at=case.created_at
            )
            db.add(action)
            
    db.commit()
    db.close()
    print("Database population completed successfully!")

if __name__ == "__main__":
    populate()
