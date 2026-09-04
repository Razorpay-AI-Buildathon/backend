import os
os.environ["RECOVERAI_API_KEY"] = "RECOVERAI-TESTKEY-12345"

import pytest
import threading
import time
import httpx
import uuid
import random
from sqlalchemy.orm import Session
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, CaseStatus, Execution, AiDecision, Merchant, Customer, PaymentEvent
from app.services.worker import RecoveryWorker
from app.services.redis_cache import RedisLock

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()

def test_chaos_load_and_concurrency_safety(setup_db, monkeypatch):
    db_session = setup_db

    # Initialize demo structures
    merchant = Merchant(id="merch_chaos", name="Chaos Merchant")
    customer = Customer(id="cust_chaos", merchant_id="merch_chaos", email="chaos@test.com")
    db_session.add_all([merchant, customer])
    db_session.commit()

    # Mock RedisLock using a threading.Lock to serialize executions locally in SQLite test environment
    local_eval_lock = threading.Lock()
    monkeypatch.setattr(RedisLock, "__enter__", lambda self: local_eval_lock.acquire(blocking=True))
    monkeypatch.setattr(RedisLock, "__exit__", lambda self, exc_type, exc_val, exc_tb: local_eval_lock.release())

    # Mock httpx.post to randomly throw errors (simulating AI unavailable/gateway timeout)
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data

    ai_calls = 0
    def mock_post(*args, **kwargs):
        nonlocal ai_calls
        ai_calls += 1
        # Randomly simulate AI Service failure (503 Service Unavailable)
        if random.random() < 0.15:
            raise httpx.HTTPStatusError("AI service unavailable", request=None, response=MockResponse({}, 503))
        
        # Randomly simulate gateway timeout / slow response
        if random.random() < 0.1:
            time.sleep(0.02)

        return MockResponse({
            "final_action": "RETRY_PAYMENT",
            "final_confidence": 0.90,
            "action_id": f"act-chaos-{uuid.uuid4().hex[:6]}"
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    # Generate 100 cases under load (100 events/sec simulated enqueue)
    case_ids = []
    for i in range(100):
        evt_id = f"evt_chaos_{i}"
        case_id = f"case_chaos_{i}"
        
        event = PaymentEvent(
            id=evt_id,
            merchant_id="merch_chaos",
            customer_id="cust_chaos",
            event_type="FAILED_PAYMENT",
            amount=100.0 * (i + 1),
            currency="INR"
        )
        db_session.add(event)
        db_session.flush()

        case = RecoveryCase(
            id=case_id,
            event_id=evt_id,
            merchant_id="merch_chaos",
            customer_id="cust_chaos",
            status=CaseStatus.IDENTIFIED
        )
        db_session.add(case)
        case_ids.append(case_id)
        
    db_session.commit()

    # We will spawn 10 concurrent worker threads evaluating the cases
    workers = [RecoveryWorker() for _ in range(10)]
    
    # We mix up the case IDs list and duplicate items to simulate duplicate jobs
    eval_list = case_ids * 2  # Each case evaluated twice
    random.shuffle(eval_list)

    errors = []
    def worker_loop(worker_instance):
        while eval_list:
            try:
                # Thread-safe pop
                case_id = eval_list.pop()
            except IndexError:
                break
            try:
                worker_instance.process_case_evaluation(case_id)
            except Exception as e:
                errors.append(str(e))

    threads = []
    for w in workers:
        t = threading.Thread(target=worker_loop, args=(w,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Expire all cache
    db_session.expire_all()

    # 1. Assert: No case has more than ONE decision proposed
    for c_id in case_ids:
        decisions = db_session.query(AiDecision).filter(AiDecision.case_id == c_id).all()
        if len(decisions) > 1:
            print(f"DEBUG: Case {c_id} has decisions: {[d.id for d in decisions]}")
            c = db_session.query(RecoveryCase).filter(RecoveryCase.id == c_id).first()
            print(f"DEBUG: Case {c_id} final status: {c.status}")
        assert len(decisions) <= 1, f"Duplicate decision proposed for case {c_id}"

    # 2. Assert: No duplicate executions recorded
    for c_id in case_ids:
        executions = db_session.query(Execution).filter(Execution.case_id == c_id).all()
        assert len(executions) <= 1, f"Duplicate execution found for case {c_id}"

    print(f"Chaos test completed. Total AI calls: {ai_calls}. Active errors: {len(errors)}")
