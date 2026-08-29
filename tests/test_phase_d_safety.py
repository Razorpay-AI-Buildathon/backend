import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
from sqlalchemy.orm import Session
import uuid
import threading
import time
import httpx

from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, ActionType, RecoveryAction, AuditEvent, ActionState, Merchant, Customer, PaymentEvent, AiDecision
from app.services.worker import RecoveryWorker

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_concurrent_worker_claiming_safety(setup_db, monkeypatch):
    db_session = setup_db

    # Mock httpx.post to simulate successful AI service call
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data

    def mock_post(*args, **kwargs):
        return MockResponse({
            "final_action": "RETRY_PAYMENT",
            "final_confidence": 0.95,
            "action_id": "act-mock-concurrency"
        })

    monkeypatch.setattr(httpx, "post", mock_post)
    
    # 1. Create seed data
    merchant = Merchant(id="merch_safe", name="Safe Merchant")
    customer = Customer(id="cust_safe", merchant_id="merch_safe", email="safe@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_safe",
        merchant_id="merch_safe",
        customer_id="cust_safe",
        event_type="FAILED_PAYMENT",
        amount=1000.0,
        currency="INR"
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        id="case_safe",
        event_id=event.id,
        merchant_id="merch_safe",
        customer_id="cust_safe",
        status=CaseStatus.IDENTIFIED
    )
    db_session.add(case)
    db_session.commit()

    # We will run evaluate_case concurrently using two worker threads.
    # Because of RedisLock, only one thread should successfully run the evaluation.
    results = []

    def worker_thread():
        # Create worker instance per thread
        worker = RecoveryWorker()
        try:
            worker.process_case_evaluation("case_safe")
            results.append("success")
        except Exception as e:
            results.append(str(e))

    # Spawn concurrent threads
    t1 = threading.Thread(target=worker_thread)
    t2 = threading.Thread(target=worker_thread)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Verify that the case transitions to a downstream status (e.g. approved or review or executing)
    # and not duplicated.
    db_session.expire_all()
    updated_case = db_session.query(RecoveryCase).filter(RecoveryCase.id == "case_safe").first()
    
    # Assert case status transitioned downstream
    assert updated_case.status in (CaseStatus.GUARD_REVIEW, CaseStatus.APPROVED, CaseStatus.EXECUTING, CaseStatus.FAILED, CaseStatus.RECOVERED, CaseStatus.HUMAN_REVIEW, CaseStatus.ANALYZING)

    # Let's count how many AiDecision records were created.
    # In a safe concurrency model, only 1 decision should be proposed and recorded!
    decisions = db_session.query(AiDecision).filter(AiDecision.case_id == "case_safe").all()
    assert len(decisions) == 1


def test_concurrent_worker_claiming_safety_without_redis(setup_db, monkeypatch):
    db_session = setup_db

    # Mock httpx.post to simulate successful AI service call
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data

    def mock_post(*args, **kwargs):
        # Add a tiny delay to maximize concurrency race condition likelihood
        time.sleep(0.05)
        return MockResponse({
            "final_action": "RETRY_PAYMENT",
            "final_confidence": 0.95,
            "action_id": f"act-mock-concurrency-{uuid.uuid4().hex[:6]}"
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    # Mock RedisLock to bypass lock and simulate down Redis
    from app.services.redis_cache import RedisLock
    monkeypatch.setattr(RedisLock, "__enter__", lambda self: True)
    monkeypatch.setattr(RedisLock, "__exit__", lambda self, exc_type, exc_val, exc_tb: None)
    
    # 1. Create seed data
    merchant = Merchant(id="merch_safe_db", name="Safe DB Merchant")
    customer = Customer(id="cust_safe_db", merchant_id="merch_safe_db", email="safedb@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_safe_db",
        merchant_id="merch_safe_db",
        customer_id="cust_safe_db",
        event_type="FAILED_PAYMENT",
        amount=1000.0,
        currency="INR"
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        id="case_safe_db",
        event_id=event.id,
        merchant_id="merch_safe_db",
        customer_id="cust_safe_db",
        status=CaseStatus.IDENTIFIED
    )
    db_session.add(case)
    db_session.commit()

    results = []

    def worker_thread():
        worker = RecoveryWorker()
        try:
            worker.process_case_evaluation("case_safe_db")
            results.append("success")
        except Exception as e:
            results.append(str(e))

    # Spawn concurrent threads
    t1 = threading.Thread(target=worker_thread)
    t2 = threading.Thread(target=worker_thread)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    db_session.expire_all()
    
    # Verify that only 1 decision was made, even though RedisLock returned True to both threads
    decisions = db_session.query(AiDecision).filter(AiDecision.case_id == "case_safe_db").all()
    assert len(decisions) == 1


def test_outbox_publisher_crash_recovery(setup_db, monkeypatch):
    db_session = setup_db
    from app.services.outbox import create_outbox_event, OutboxPublisher
    from app.models.case import OutboxEvent

    # 1. Create a dummy outbox event
    create_outbox_event(db_session, "evaluate_case", "case_crash_test", {"case_id": "case_crash_test"})
    db_session.commit()

    # 2. Simulate Redis down (OutboxPublisher initialization or enqueue fails)
    from app.services.queue import RedisQueue
    def mock_enqueue_fail(self, task_name, payload):
        raise ConnectionError("Redis is down")
    monkeypatch.setattr(RedisQueue, "enqueue", mock_enqueue_fail)

    # Run publisher - event should fail to publish and remain in DB
    published_count = OutboxPublisher.publish_pending_events(db_session)
    assert published_count == 0

    db_session.expire_all()
    events = db_session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == "case_crash_test").all()
    assert len(events) == 1
    assert events[0].published_at is None
    assert events[0].attempt_count >= 1

    # 3. Simulate publisher recovery and successful publish
    published_events = []
    def mock_enqueue_collect(self, task_name, payload):
        published_events.append((task_name, payload))
    monkeypatch.setattr(RedisQueue, "enqueue", mock_enqueue_collect)

    published_count = OutboxPublisher.publish_pending_events(db_session)
    assert published_count == 1

    db_session.expire_all()
    events = db_session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == "case_crash_test").all()
    assert len(events) == 1
    assert events[0].published_at is not None


def test_worker_outbox_duplicate_deduplication(setup_db, monkeypatch):
    db_session = setup_db

    # Mock httpx.post to simulate successful AI service call
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data

    def mock_post(*args, **kwargs):
        time.sleep(0.05)
        return MockResponse({
            "final_action": "RETRY_PAYMENT",
            "final_confidence": 0.95,
            "action_id": "act-outbox-dup"
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    # Mock RedisLock to bypass lock and isolate DB safety checks
    from app.services.redis_cache import RedisLock
    monkeypatch.setattr(RedisLock, "__enter__", lambda self: True)
    monkeypatch.setattr(RedisLock, "__exit__", lambda self, exc_type, exc_val, exc_tb: None)

    # Seed case details
    merchant = Merchant(id="merch_dup", name="Dup Merchant")
    customer = Customer(id="cust_dup", merchant_id="merch_dup", email="dup@test.com")
    db_session.add_all([merchant, customer])
    db_session.flush()

    event = PaymentEvent(
        id="evt_dup",
        merchant_id="merch_dup",
        customer_id="cust_dup",
        event_type="FAILED_PAYMENT",
        amount=500.0,
        currency="INR"
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        id="case_dup",
        event_id=event.id,
        merchant_id="merch_dup",
        customer_id="cust_dup",
        status=CaseStatus.IDENTIFIED
    )
    db_session.add(case)
    db_session.commit()

    # We will simulate the worker running process_case_evaluation concurrently twice
    # on duplicate events. Only one should successfully generate a decision.
    worker = RecoveryWorker()
    
    results = []
    def run_worker():
        try:
            worker.process_case_evaluation("case_dup")
            results.append("processed")
        except Exception as e:
            results.append(str(e))

    # Run twice concurrently
    t1 = threading.Thread(target=run_worker)
    t2 = threading.Thread(target=run_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    db_session.expire_all()
    decisions = db_session.query(AiDecision).filter(AiDecision.case_id == "case_dup").all()
    assert len(decisions) == 1
