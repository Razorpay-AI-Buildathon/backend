import os
import time
import httpx
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
                    retries = payload.get("retries", 0)
                    try:
                        self.process_case_evaluation(payload["case_id"])
                    except Exception as eval_err:
                        if retries >= 2:
                            self.move_to_dlq(payload["case_id"], task, str(eval_err), retries + 1)
                        else:
                            payload["retries"] = retries + 1
                            try:
                                self.queue.enqueue(task_name, payload)
                            except Exception as eq_err:
                                print(f"RecoveryWorker: Failed to re-enqueue task: {eq_err}")
                elif task_name == "execute_case":
                    retries = payload.get("retries", 0)
                    try:
                        self.process_case_execution(payload["case_id"])
                    except Exception as exec_err:
                        if retries >= 2:
                            self.move_to_dlq(payload["case_id"], task, str(exec_err), retries + 1)
                        else:
                            payload["retries"] = retries + 1
                            try:
                                self.queue.enqueue(task_name, payload)
                            except Exception as eq_err:
                                print(f"RecoveryWorker: Failed to re-enqueue execute task: {eq_err}")
            except Exception as e:
                print(f"RecoveryWorker: Error in execution loop: {e}")

    def move_to_dlq(self, case_id: str, task: dict, error_msg: str, retry_count: int):
        db = SessionLocal()
        try:
            from app.models.case import DeadLetterJob
            dlq_job = DeadLetterJob(
                job_id=task.get("job_id", f"job-{uuid.uuid4().hex[:12]}"),
                case_id=case_id,
                payload=task,
                failure_reason=error_msg,
                retry_count=retry_count,
                last_error=error_msg
            )
            db.add(dlq_job)
            db.commit()
            print(f"RecoveryWorker: Task moved to DLQ: Case {case_id}, Error: {error_msg}")
        except Exception as e:
            print(f"RecoveryWorker: Failed to write DLQ: {e}")
        finally:
            db.close()

    def process_case_evaluation(self, case_id: str):
        from app.services.redis_cache import RedisLock
        self.lock = RedisLock(f"case_evaluation:{case_id}", expire_seconds=30)
        if not self.lock.__enter__():
            print(f"RecoveryWorker: Case {case_id} is currently locked by another worker.")
            return

        db = SessionLocal()
        try:
            from sqlalchemy import text
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).with_for_update().first()
            if not case:
                print(f"RecoveryWorker: Case {case_id} not found in database.")
                return

            # Force raw status select to bypass connection/pooling cache anomalies in SQLite/Postgres
            raw_status = db.execute(
                text("SELECT status FROM recovery_cases WHERE id = :id"),
                {"id": case_id}
            ).scalar()

            if raw_status not in ("IDENTIFIED", "ANALYZING"):
                print(f"RecoveryWorker: Case {case_id} is in status {raw_status}, not ready for evaluation.")
                return

            from datetime import datetime
            if raw_status == "ANALYZING" and case.next_action_at and case.next_action_at > datetime.utcnow():
                print(f"RecoveryWorker: Case {case_id} is scheduled for future evaluation at {case.next_action_at}. Skipping.")
                return

            from app.models.case import Execution
            active_execution = db.query(Execution).filter(
                Execution.case_id == case_id,
                Execution.status == "PENDING"
            ).first()
            if active_execution:
                print(f"RecoveryWorker: Active execution found for case {case_id}. Aborting evaluation.")
                return

            event = db.query(PaymentEvent).filter(PaymentEvent.id == case.event_id).first()
            customer = db.query(Customer).filter(Customer.id == case.customer_id).first()
            if not event or not customer:
                print(f"RecoveryWorker: Event or Customer not found for case {case_id}.")
                return

            # Task 25: Merchant Policy check
            from app.models.case import MerchantRecoveryPolicy, AiDecision
            policy = db.query(MerchantRecoveryPolicy).filter(
                MerchantRecoveryPolicy.merchant_id == case.merchant_id,
                MerchantRecoveryPolicy.enabled == True
            ).order_by(MerchantRecoveryPolicy.version.desc()).first()

            if policy:
                case.policy_id = policy.id
                case.policy_version = policy.version
                db.flush()

                # Evaluate risk score & amount threshold
                human_overrides = [log for log in (case.audit_log or []) if log.get("event") in ("human_approved_risk", "human_approved_amount")]
                has_risk_override = any(log.get("event") == "human_approved_risk" for log in human_overrides)

                if float(customer.risk_score) > float(policy.risk_threshold) and not has_risk_override:
                    CaseStateMachine.transition_status(
                        db, case, CaseStatus.HUMAN_REVIEW, "policy_risk_escalation", "SYSTEM",
                        {"reason": f"Customer risk score {customer.risk_score} exceeds policy threshold {policy.risk_threshold}"}
                    )
                    db.commit()
                    return

                if case.current_recovery_attempt >= policy.max_attempts:
                    CaseStateMachine.transition_status(db, case, CaseStatus.CLOSED, "policy_attempts_exhausted", "SYSTEM")
                    db.commit()
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

            if case.experiment_group == "CONTROL":
                ai_data = {
                    "final_action": "RETRY_PAYMENT",
                    "final_confidence": 1.0,
                    "action_id": f"act-{uuid.uuid4().hex[:12]}",
                    "model": "control_default",
                    "model_version": "v1.0",
                    "prompt_version": "control",
                    "strategy_version": "control",
                    "playbook_version": "control"
                }
            else:
                try:
                    resp = httpx.post(f"{ai_service_url}/analyze-event", json=state_input, timeout=10.0)
                    if resp.status_code != 200:
                        raise Exception(f"AI Service HTTP error {resp.status_code}")
                    ai_data = resp.json()
                except Exception as e:
                    # Task 28: AI Failure Isolation Fallback (Escalate to Human Operator)
                    print(f"RecoveryWorker: AI Service call failed: {e}. Escalating to human operator.")
                    CaseStateMachine.transition_status(db, case, CaseStatus.HUMAN_REVIEW, "ai_service_failure", "SYSTEM", {"error": str(e)})
                    db.commit()
                    return

            proposed_action = ai_data.get("final_action", "DO_NOTHING")
            confidence = ai_data.get("final_confidence", 0.70)
            action_id = ai_data.get("action_id", f"act-{uuid.uuid4().hex}")

            # Task 27: Save AI Decision record for reproducibility
            import hashlib
            import json
            context_str = json.dumps(state_input, sort_keys=True)
            context_hash = hashlib.sha256(context_str.encode("utf-8")).hexdigest()
            
            from app.models.case import RecoveryAction, Execution, ActionState
            
            db_action = RecoveryAction(
                id=action_id,
                case_id=case.id,
                action_type=proposed_action,
                proposed_by="AI_PLANNER" if case.experiment_group == "TREATMENT" else "CONTROL_STRATEGY",
                state=ActionState.PROPOSED,
                authorization_token=None,
                action_id=action_id,
                execution_id=None
            )
            db.add(db_action)
            db.flush()
            
            ai_decision = AiDecision(
                case_id=case.id,
                action_id=action_id,
                model=ai_data.get("model", "gpt-4o"),
                model_version=ai_data.get("model_version", "2024-05-13"),
                prompt_version=ai_data.get("prompt_version", "v1.2"),
                strategy_version=ai_data.get("strategy_version", "v1.0"),
                playbook_version=ai_data.get("playbook_version", "v1.0"),
                input_context_hash=context_hash,
                confidence=confidence,
                proposal=proposed_action
            )
            db.add(ai_decision)
            db.flush()

            # Task 25: Verify against policy limits (amount threshold)
            max_limit = float(policy.amount_threshold) if policy else 5000.0
            human_overrides = [log for log in (case.audit_log or []) if log.get("event") in ("human_approved_risk", "human_approved_amount")]
            has_amount_override = any(log.get("event") == "human_approved_amount" for log in human_overrides)

            if float(event.amount) > max_limit and not has_amount_override:
                db_action.state = ActionState.REJECTED_BY_GUARD
                CaseStateMachine.transition_status(
                    db, case, CaseStatus.HUMAN_REVIEW, "policy_amount_escalation", "SYSTEM",
                    {"reason": f"Payment amount {event.amount} exceeds policy threshold {max_limit}"}
                )
                db.commit()
                return

            approved, token, violations = ActionGuard.validate_action(
                action_type=proposed_action,
                amount=event.amount,
                currency=event.currency,
                current_attempts=case.current_recovery_attempt,
                max_retries=case.max_attempts,
                amount_threshold_inr=max_limit,
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

                db_action.state = ActionState.EXECUTED if proposed_action in ("RETRY_PAYMENT", "RETRY") else ActionState.APPROVED_BY_GUARD
                db_action.authorization_token = token
                db_action.execution_id = res["execution_id"]
                db.flush()

                is_payment_action = (proposed_action in ("RETRY_PAYMENT", "RETRY")) and res.get("async_reconciliation", False)
                exec_status = "PENDING" if is_payment_action else res["status"]
                
                execution = Execution(
                    id=res["execution_id"],
                    action_id=db_action.id,
                    case_id=case.id,
                    status=exec_status,
                    provider="razorpay",
                    provider_reference=res["execution_id"],
                    amount=event.amount,
                    currency=event.currency,
                    attempted_at=datetime.utcnow(),
                    completed_at=None if exec_status == "PENDING" else datetime.utcnow(),
                    result_code=None,
                    failure_reason=None if res["recovered"] or exec_status == "PENDING" else res["message"]
                )
                db.add(execution)

                target_state = CaseStatus.EXECUTING if is_payment_action else (CaseStatus.RECOVERED if res["recovered"] else CaseStatus.FAILED)
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
                        backoff_seconds = (2 ** case.current_recovery_attempt) * 5
                        jitter = random.uniform(0.9, 1.1)
                        actual_delay = backoff_seconds * jitter
                        execute_at = time.time() + actual_delay
                        from datetime import timedelta
                        case.next_action_at = datetime.utcnow() + timedelta(seconds=actual_delay)
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
            print(f"RecoveryWorker: DB Error processing case {case_id}: {e}")
        finally:
            db.close()
            try:
                self.lock.__exit__(None, None, None)
            except Exception as le:
                print(f"RecoveryWorker: Error releasing lock: {le}")

    def process_case_execution(self, case_id: str):
        from app.services.redis_cache import RedisLock
        self.lock = RedisLock(f"case_execution:{case_id}", expire_seconds=30)
        if not self.lock.__enter__():
            print(f"RecoveryWorker: Case execution {case_id} is currently locked.")
            return

        db = SessionLocal()
        try:
            case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).with_for_update().first()
            if not case:
                return

            if case.status != CaseStatus.APPROVED:
                print(f"RecoveryWorker: Case {case_id} is not APPROVED, it is {case.status}. Skipping execution.")
                return

            event = db.query(PaymentEvent).filter(PaymentEvent.id == case.event_id).first()
            
            from app.models.case import RecoveryAction, Execution, ActionState
            db_action = db.query(RecoveryAction).filter(RecoveryAction.case_id == case_id).order_by(RecoveryAction.created_at.desc()).first()
            if not db_action:
                print(f"RecoveryWorker: No action found for case {case_id}.")
                return

            CaseStateMachine.transition_status(db, case, CaseStatus.EXECUTING, "worker_execute_start", "SYSTEM")
            db.commit()

            # The action_type can be an Enum or a string depending on how it was saved
            action_type_val = db_action.action_type.value if hasattr(db_action.action_type, 'value') else db_action.action_type

            simulate_failure = db_action.authorization_token and db_action.authorization_token.startswith("FAIL-")

            res = ExecutionSimulator.execute_action(
                action_type=action_type_val,
                amount=event.amount,
                currency=event.currency,
                is_guard_approved=True,
                auth_token=db_action.authorization_token or "mocked-token",
                case_id=case.id,
                event_id=event.id,
                action_id=db_action.id,
                simulate_failure=simulate_failure
            )

            db_action.state = ActionState.EXECUTED if res["status"] == "SUCCESS" else ActionState.FAILED
            db_action.execution_id = res["execution_id"]
            db.flush()

            is_payment_action = (action_type_val in ("RETRY_PAYMENT", "RETRY")) and res.get("async_reconciliation", False)
            exec_status = "PENDING" if is_payment_action else res["status"]
            
            execution = Execution(
                id=res["execution_id"],
                action_id=db_action.id,
                case_id=case.id,
                status=exec_status,
                provider="razorpay",
                provider_reference=res["execution_id"],
                amount=event.amount,
                currency=event.currency,
                attempted_at=datetime.utcnow(),
                completed_at=None if exec_status == "PENDING" else datetime.utcnow(),
                result_code=None,
                failure_reason=None if res["recovered"] or exec_status == "PENDING" else res["message"]
            )
            db.add(execution)

            if is_payment_action:
                target_state = CaseStatus.EXECUTING
            else:
                target_state = CaseStatus.RECOVERED if res["status"] == "SUCCESS" else CaseStatus.FAILED
            
            CaseStateMachine.transition_status(db, case, target_state, "worker_execution_result", "SYSTEM", {"execution_id": res["execution_id"]})

            from app.services.logging import logger as struct_logger
            struct_logger.info(
                "Action execution completed",
                case_id=case.id,
                event_id=event.id,
                action_id=db_action.id,
                execution_id=res["execution_id"],
                status=target_state.value
            )

            if target_state == CaseStatus.FAILED:
                case.current_recovery_attempt += 1
                if case.current_recovery_attempt >= case.max_attempts:
                    CaseStateMachine.transition_status(db, case, CaseStatus.HUMAN_REVIEW, "worker_retry_limit_exhausted", "SYSTEM", {"reason": "Max recovery attempts reached. Escalating to human operator."})
                else:
                    CaseStateMachine.transition_status(db, case, CaseStatus.ANALYZING, "worker_replan_triggered", "SYSTEM")
                    backoff_seconds = (2 ** case.current_recovery_attempt) * 5
                    import random
                    jitter = random.uniform(0.9, 1.1)
                    actual_delay = backoff_seconds * jitter
                    import time
                    execute_at = time.time() + actual_delay
                    from datetime import timedelta
                    case.next_action_at = datetime.utcnow() + timedelta(seconds=actual_delay)
                    self.scheduler.schedule("evaluate_case", {"case_id": case.id}, execute_at)
            db.commit()

        except Exception as e:
            print(f"RecoveryWorker: DB Error processing execution for case {case_id}: {e}")
        finally:
            db.close()
            try:
                self.lock.__exit__(None, None, None)
            except Exception as le:
                print(f"RecoveryWorker: Error releasing lock: {le}")
