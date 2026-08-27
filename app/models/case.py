from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    ForeignKey,
    Numeric,
    JSON,
    Enum,
)
# pyrefly: ignore [missing-import]
from sqlalchemy.dialects.postgresql import UUID, JSONB
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
from app.db.session import Base
import enum


class CaseStatus(str, enum.Enum):
    IDENTIFIED = "IDENTIFIED"
    DETECTED = "DETECTED"
    ANALYZING = "ANALYZING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    GUARD_REVIEW = "GUARD_REVIEW"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLOSED = "CLOSED"


class ActionType(str, enum.Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    SEND_PAYMENT_REMINDER = "SEND_PAYMENT_REMINDER"
    SEND_CHECKOUT_RECOVERY_MESSAGE = "SEND_CHECKOUT_RECOVERY_MESSAGE"
    RETRY_SUBSCRIPTION = "RETRY_SUBSCRIPTION"
    SEND_INVOICE_REMINDER = "SEND_INVOICE_REMINDER"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    DO_NOTHING = "DO_NOTHING"


class ActionState(str, enum.Enum):
    PROPOSED = "PROPOSED"
    APPROVED_BY_GUARD = "APPROVED_BY_GUARD"
    REJECTED_BY_GUARD = "REJECTED_BY_GUARD"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SUCCESSFUL = "SUCCESSFUL"


class Merchant(Base):
    __tablename__ = "merchants"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    razorpay_key_id = Column(String, nullable=True)
    razorpay_key_secret = Column(String, nullable=True)
    amount_threshold = Column(
        Numeric(12, 2), default=5000.00
    )  # Currency-neutral base threshold
    max_retries = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    merchant_id = Column(String, ForeignKey("merchants.id"), nullable=True)
    email = Column(String, nullable=False, index=True)
    phone = Column(String, nullable=True)
    risk_score = Column(Numeric(3, 2), default=0.00)
    payment_history_success_rate = Column(Numeric(3, 2), default=1.00)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    merchant_id = Column(String, ForeignKey("merchants.id"), nullable=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    event_type = Column(
        String, nullable=False
    )  # e.g. FAILED_PAYMENT, CHECKOUT_ABANDONMENT
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="INR")
    failure_code = Column(String, nullable=True)
    provider = Column(String, default="razorpay")
    provider_event_id = Column(String, nullable=True)
    payload_metadata = Column(JSON, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("payment_events.id"), nullable=False)
    merchant_id = Column(String, ForeignKey("merchants.id"), nullable=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=True)
    status = Column(Enum(CaseStatus), default=CaseStatus.IDENTIFIED)
    priority_score = Column(Integer, default=0)
    expected_recovery_value = Column(Numeric(12, 2), default=0.00)
    current_recovery_attempt = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    next_action_at = Column(DateTime, nullable=True)
    audit_log = Column(JSON, default=list)  # Append-only structured decision traces
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    event = relationship("PaymentEvent")
    actions = relationship("RecoveryAction", back_populates="case")
    audit_events = relationship("AuditEvent", back_populates="case", cascade="all, delete-orphan")


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False)
    action_type = Column(Enum(ActionType), nullable=False)
    proposed_by = Column(String, nullable=False)
    state = Column(Enum(ActionState), default=ActionState.PROPOSED)
    authorization_token = Column(String, nullable=True)
    action_id = Column(String, nullable=True)
    execution_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("RecoveryCase", back_populates="actions")
    audit_events = relationship("AuditEvent", back_populates="action")


class Execution(Base):
    __tablename__ = "executions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action_id = Column(String, ForeignKey("recovery_actions.id"), nullable=True)
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=False)
    status = Column(String, nullable=False)  # SUCCESS, FAILED
    provider = Column(String, default="razorpay")
    provider_reference = Column(String, nullable=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="INR")
    attempted_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    result_code = Column(String, nullable=True)
    failure_reason = Column(String, nullable=True)
    metadata_json = Column(JSON, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id = Column(String, ForeignKey("recovery_cases.id"), nullable=True)
    action_id = Column(String, ForeignKey("recovery_actions.id"), nullable=True)
    event_type = Column(String, nullable=False)
    actor = Column(String, nullable=False)
    decision_source = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    metadata_json = Column(JSON, nullable=True)

    case = relationship("RecoveryCase", back_populates="audit_events")
    action = relationship("RecoveryAction", back_populates="audit_events")



# Centralized State Machine Transitions Logic
class CaseStateMachine:
    # Allowable transitions: current -> set of allowed next statuses
    VALID_TRANSITIONS = {
        CaseStatus.IDENTIFIED: {
            CaseStatus.ANALYZING,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.DETECTED: {
            CaseStatus.ANALYZING,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.ANALYZING: {
            CaseStatus.ACTION_PROPOSED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.ACTION_PROPOSED: {
            CaseStatus.GUARD_REVIEW,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.GUARD_REVIEW: {
            CaseStatus.APPROVED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.APPROVED: {
            CaseStatus.EXECUTING,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.EXECUTING: {
            CaseStatus.RECOVERED,
            CaseStatus.FAILED,
            CaseStatus.BLOCKED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.FAILED: {
            CaseStatus.ANALYZING,
            CaseStatus.CLOSED,
            CaseStatus.HUMAN_REVIEW,
        },
        CaseStatus.HUMAN_REVIEW: {
            CaseStatus.APPROVED,
            CaseStatus.BLOCKED,
            CaseStatus.CLOSED,
        },
        # Terminal states (cannot transition back to active executing flows)
        CaseStatus.RECOVERED: set(),
        CaseStatus.BLOCKED: set(),
        CaseStatus.CLOSED: set(),
    }

    @classmethod
    def validate_transition(
        cls, current_status: CaseStatus, target_status: CaseStatus
    ) -> bool:
        if current_status == target_status:
            return True
        allowed = cls.VALID_TRANSITIONS.get(current_status, set())
        return target_status in allowed

    @classmethod
    def transition_status(
        cls,
        db,
        case,
        target_status: CaseStatus,
        event_name: str,
        actor: str = "SYSTEM",
        details: dict = None,
    ) -> bool:
        if not cls.validate_transition(case.status, target_status):
            raise ValueError(
                f"State Machine Guard: Invalid transition from {case.status} to {target_status} for case {case.id}"
            )
        
        old_status = case.status
        case.status = target_status
        if target_status in (CaseStatus.RECOVERED, CaseStatus.BLOCKED, CaseStatus.CLOSED):
            case.closed_at = datetime.utcnow()
            
        # Append state transition to audit log
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "node": "state_machine",
            "event": "state_transition",
            "inputs": {"old_status": old_status.value, "new_status": target_status.value, "trigger": event_name},
            "outputs": {"status": "success"},
            "decision": target_status.value,
            "confidence": 1.0,
            "decision_source": actor,
            "model": "rule_engine",
            "details": details or {}
        }
        case.audit_log = (case.audit_log or []) + [log_entry]

        # First-class AuditEvent creation
        event_type = f"STATE_TRANSITION_{target_status.value}"
        decision_source = "SYSTEM"
        
        if target_status == CaseStatus.IDENTIFIED:
            event_type = "CASE_CREATED"
            decision_source = "SYSTEM"
        elif target_status == CaseStatus.ANALYZING:
            if event_name in ("replan_triggered", "worker_replan_triggered", "replan"):
                event_type = "REPLAN_TRIGGERED"
                decision_source = "SYSTEM"
            else:
                event_type = "COUNCIL_STARTED"
                decision_source = "AI_COUNCIL"
        elif target_status == CaseStatus.ACTION_PROPOSED:
            event_type = "ACTION_PROPOSED"
            decision_source = "AI_COUNCIL"
        elif target_status == CaseStatus.GUARD_REVIEW:
            event_type = "POLICY_EVALUATED"
            decision_source = "POLICY_ENGINE"
        elif target_status == CaseStatus.APPROVED:
            event_type = "GUARD_APPROVED"
            decision_source = "ACTION_GUARD"
        elif target_status == CaseStatus.EXECUTING:
            event_type = "EXECUTION_STARTED"
            decision_source = "SYSTEM"
        elif target_status == CaseStatus.RECOVERED:
            event_type = "CASE_RECOVERED"
            decision_source = "SYSTEM"
        elif target_status == CaseStatus.FAILED:
            event_type = "EXECUTION_FAILED"
            decision_source = "SYSTEM"
        elif target_status == CaseStatus.BLOCKED:
            event_type = "GUARD_BLOCKED"
            decision_source = "ACTION_GUARD"
        elif target_status == CaseStatus.HUMAN_REVIEW:
            event_type = "HUMAN_ESCALATION"
            decision_source = "ACTION_GUARD"
        elif target_status == CaseStatus.CLOSED:
            event_type = "CASE_CLOSED"
            decision_source = "SYSTEM"

        if event_name.startswith("human_"):
            decision_source = "HUMAN_OPERATOR"

        # Determine action_id if available
        action_id = (details or {}).get("action_id")
        if not action_id and case.actions:
            sorted_actions = sorted(case.actions, key=lambda x: x.created_at, reverse=True)
            if sorted_actions:
                action_id = sorted_actions[0].id

        audit_evt = AuditEvent(
            case_id=case.id,
            action_id=action_id,
            event_type=event_type,
            actor=actor,
            decision_source=decision_source,
            metadata_json=details or {},
            timestamp=datetime.utcnow()
        )
        db.add(audit_evt)
        db.flush()
        return True
