import os
import time
import requests
import uuid
import random
from datetime import datetime, timezone, timedelta
from app.db.session import SessionLocal
from app.models.case import RecoveryCase, CaseStatus, PaymentEvent, Customer, CaseStateMachine
from app.services.executor import ExecutionSimulator
from app.services.action_guard import ActionGuard
from app.services.queue import RedisQueue, RedisScheduler

class RecoveryWorker:
    def __init__(self):
        self.queue = RedisQueue()
        self.scheduler = RedisScheduler()

    def run(self):
        while True:
            try:
                task = self.queue.dequeue(timeout=5)
                if not task:
                    continue

                task_name = task["task_name"]
                payload = task["payload"]

                if task_name == "evaluate_case":
                    self.process_case_evaluation(payload["case_id"])
            except Exception as e:
                print(f"RecoveryWorker: Error in execution loop: {e}")

    def process_case_evaluation(self, case_id: str):
        db = SessionLocal()
        try:
            case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
            if not case:
                print(f"RecoveryWorker: Case {case_id} not found in database.")
                return

            if case.status not in (CaseStatus.IDENTIFIED, CaseStatus.ANALYZING):
                print(f"RecoveryWorker: Case {case_id} is in status {case.status}, not ready for evaluation.")
                return

            event = db.query(PaymentEvent).filter(PaymentEvent.id == case.event_id).first()
            customer = db.query(Customer).filter(Customer.id == case.customer_id).first()
            if not event or not customer:
                print(f"RecoveryWorker: Event or Customer not found for case {case_id}.")
                return

            if case.status == CaseStatus.IDENTIFIED:
                CaseStateMachine.transition_status(db, case, CaseStatus.ANALYZING, "worker_analyze", "SYSTEM")
            CaseStateMachine.transition_status(db, case, CaseStatus.ACTION_PROPOSED, "worker_propose", "SYSTEM")
            db.commit()

            failed_actions = []
            for log in (case.audit_log or []):
                if log.get("event") == "execution_result" and log.get("outputs", {}).get("recovered") is False:
                    failed_actions.append(log.get("inputs", {}).get("action"))

            ai_service_url = os.getenv("AI_SERVICE_URL", "http://recoverai-ai-service:8001")
            state_input = {
                "case_id": case.id,
                "event_id": event.id,
                "event_type": event.event_type,
                "amount": float(event.amount),
                "currency": event.currency,
                "failure_code": event.failure_code,
                "customer_id": customer.id,
                "customer_risk_score": float(customer.risk_score),
                "customer_payment_history_success_rate": float(customer.payment_history_success_rate),
                "recovery_attempt_count": case.current_recovery_attempt,
                "max_retries": case.max_attempts,
                "retry_count": case.current_recovery_attempt,
                "failed_actions": failed_actions
            }

            try:
                resp = requests.post(f"{ai_service_url}/analyze-event", json=state_input, timeout=10)
                if resp.status_code != 200:
                    print(f"RecoveryWorker: AI Service error: {resp.status_code}")
                    return

                ai_data = resp.json()
                proposed_action = ai_data.get("final_action", "DO_NOTHING")
                confidence = ai_data.get("final_confidence", 0.70)
                action_id = ai_data.get("action_id", f"act-{uuid.uuid4().hex}")

                approved, token, violations = ActionGuard.validate_action(
                    action_type=proposed_action,
                    amount=event.amount,
                    currency=event.currency,
                    current_attempts=case.current_recovery_attempt,
                    max_retries=case.max_attempts,
                    amount_threshold_inr=5000.0,
                    has_active_action=False,
                    planner_confidence=confidence,
                    case_id=case.id,
                    event_id=event.id,
                    action_id=action_id
                )

                CaseStateMachine.transition_status(db, case, CaseStatus.GUARD_REVIEW, "worker_evaluate_guard", "SYSTEM")

                if approved and token:
                    CaseStateMachine.transition_status(db, case, CaseStatus.APPROVED, "worker_guard_approved", "SYSTEM")
                    CaseStateMachine.transition_status(db, case, CaseStatus.EXECUTING, "worker_execute_start", "SYSTEM")
                    db.commit()

                    res = ExecutionSimulator.execute_action(
                        action_type=proposed_action,
                        amount=event.amount,
                        currency=event.currency,
                        is_guard_approved=approved,
                        auth_token=token,
                        case_id=case.id,
                        event_id=event.id,
                        action_id=action_id
                    )

                    target_state = CaseStatus.RECOVERED if res["recovered"] else CaseStatus.FAILED
                    CaseStateMachine.transition_status(db, case, target_state, "worker_execution_result", "SYSTEM", {"execution_id": res["execution_id"]})

                    from app.services.logging import logger as struct_logger
                    struct_logger.info(
                        "Action execution completed",
                        case_id=case.id,
                        event_id=event.id,
                        action_id=action_id,
                        execution_id=res["execution_id"],
                        status=target_state.value
                    )

                    if target_state == CaseStatus.FAILED:
                        case.current_recovery_attempt += 1
                        if case.current_recovery_attempt >= case.max_attempts:
                            CaseStateMachine.transition_status(db, case, CaseStatus.CLOSED, "worker_retry_limit_exhausted", "SYSTEM")
                        else:
                            CaseStateMachine.transition_status(db, case, CaseStatus.ANALYZING, "worker_replan_triggered", "SYSTEM")
                            backoff_seconds = (2 ** case.current_recovery_attempt) * 300
                            jitter = random.uniform(0.9, 1.1)
                            actual_delay = backoff_seconds * jitter
                            execute_at = time.time() + actual_delay
                            
                            self.scheduler.schedule("evaluate_case", {"case_id": case.id}, execute_at)
                    db.commit()
                else:
                    CaseStateMachine.transition_status(db, case, CaseStatus.BLOCKED, "worker_guard_blocked", "SYSTEM", {"violations": violations})
                    db.commit()

                    from app.services.logging import logger as struct_logger
                    struct_logger.info(
                        "Action execution blocked by guard",
                        case_id=case.id,
                        event_id=event.id,
                        action_id=action_id,
                        violations=violations
                    )
            except Exception as e:
                print(f"RecoveryWorker: Error calling AI Service/ActionGuard: {e}")
        except Exception as e:
            print(f"RecoveryWorker: DB Error processing case {case_id}: {e}")
        finally:
            db.close()
