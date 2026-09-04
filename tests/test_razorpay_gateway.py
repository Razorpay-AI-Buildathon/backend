import os
import pytest
import json
import hmac
import hashlib
from decimal import Decimal
from fastapi.testclient import TestClient

from app.core.config import settings

@pytest.fixture(scope="module", autouse=True)
def configure_razorpay_settings():
    old_mode = settings.GATEWAY_MODE
    old_id = settings.RAZORPAY_KEY_ID
    old_secret = settings.RAZORPAY_KEY_SECRET
    old_webhook = settings.RAZORPAY_WEBHOOK_SECRET
    
    settings.GATEWAY_MODE = "RAZORPAY_TEST"
    settings.RAZORPAY_KEY_ID = "rzp_test_mockkeyid"
    settings.RAZORPAY_KEY_SECRET = "mockkeysecret"
    settings.RAZORPAY_WEBHOOK_SECRET = "mockwebhooksecret"
    
    yield
    
    settings.GATEWAY_MODE = old_mode
    settings.RAZORPAY_KEY_ID = old_id
    settings.RAZORPAY_KEY_SECRET = old_secret
    settings.RAZORPAY_WEBHOOK_SECRET = old_webhook

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import RecoveryCase, PaymentEvent, RecoveryAction, Execution, Merchant, Customer
from app.services.gateway import RazorpayPaymentGateway

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_razorpay_gateway_missing_credentials():
    # Setup temporary unconfigured settings
    from app.core.config import settings
    old_id = settings.RAZORPAY_KEY_ID
    old_secret = settings.RAZORPAY_KEY_SECRET
    
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    
    try:
        gateway = RazorpayPaymentGateway()
        res = gateway.execute_action("RETRY_PAYMENT", Decimal("100.00"), "INR", "case-1", "evt-1", "act-1")
        assert res.success is False
        assert res.result_code == "UNCONFIGURED"
        assert "credentials missing" in res.failure_reason
    finally:
        settings.RAZORPAY_KEY_ID = old_id
        settings.RAZORPAY_KEY_SECRET = old_secret


def test_razorpay_gateway_api_success(monkeypatch):
    # Mock httpx POST request for Razorpay Payment Link creation
    class MockResponse:
        status_code = 201
        def json(self):
            return {"id": "plink_ABCD12345", "status": "created"}

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: MockResponse())

    gateway = RazorpayPaymentGateway()
    res = gateway.execute_action("RETRY_PAYMENT", Decimal("2000.00"), "INR", "case-1", "evt-1", "act-1")
    
    assert res.success is True
    assert res.recovered is False  # Not recovered yet! Requires webhook reconciliation.
    assert res.provider_reference == "plink_ABCD12345"
    assert res.result_code == "PENDING"
    assert res.async_reconciliation is True


def test_razorpay_webhook_signature_verification():
    # Test valid signature verification
    payload = {
        "event": "payment.captured",
        "provider_event_id": "evt_captured_999",
        "payload": {
            "payment": {
                "entity": {
                    "id": "plink_ABCD12345",
                    "status": "captured",
                    "amount": 200000
                }
            }
        }
    }
    body_str = json.dumps(payload)
    
    # Generate signature using HMAC-SHA256 and the raw body
    webhook_secret = "mockwebhooksecret"
    signature = hmac.new(
        webhook_secret.encode("utf-8"),
        body_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    # Create matching execution to reconcile
    db = SessionLocal()
    merchant = Merchant(id="m-1", name="Test Merchant")
    db.merge(merchant)
    customer = Customer(id="c-1", email="test@example.com")
    db.merge(customer)
    db.flush()

    pe = PaymentEvent(
        id="evt-1",
        merchant_id="m-1",
        customer_id="c-1",
        event_type="FAILED_PAYMENT",
        amount=Decimal("2000.00"),
        provider="razorpay",
        provider_event_id="prov-evt-1"
    )
    db.add(pe)
    db.flush()

    case = RecoveryCase(
        id="case-1",
        event_id=pe.id,
        merchant_id="m-1",
        customer_id="c-1",
        status="EXECUTING",
        priority_score=10,
        expected_recovery_value=Decimal("2000.00")
    )
    db.add(case)
    db.flush()

    execution = Execution(
        id="plink_ABCD12345",
        case_id=case.id,
        status="PENDING",
        provider="razorpay",
        provider_reference="plink_ABCD12345",
        amount=Decimal("2000.00"),
        currency="INR"
    )
    db.add(execution)
    db.commit()

    # Call webhook endpoint with valid signature
    resp = client.post(
        "/api/webhooks/provider",
        content=body_str,
        headers={"X-Razorpay-Signature": signature}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    # Assert execution status is updated to SUCCESS and case status to RECOVERED
    db.expire_all()
    updated_exec = db.query(Execution).filter(Execution.id == "plink_ABCD12345").first()
    assert updated_exec.status == "SUCCESS"
    assert updated_exec.reconciled_amount == Decimal("2000.00")

    updated_case = db.query(RecoveryCase).filter(RecoveryCase.id == "case-1").first()
    assert updated_case.status.value == "RECOVERED"
    db.close()


def test_razorpay_webhook_invalid_signature():
    payload = {"event": "payment.captured", "id": "evt_captured_999"}
    resp = client.post(
        "/api/webhooks/provider",
        json=payload,
        headers={"X-Razorpay-Signature": "wrong_signature"}
    )
    assert resp.status_code == 400
    assert "Invalid signature" in resp.json()["detail"]


def test_create_order_insufficient_amount():
    resp = client.post("/api/create-order", json={"amount": 50, "currency": "INR"})
    assert resp.status_code == 400
    assert "at least 100 paise" in resp.json()["detail"]


def test_create_order_success(monkeypatch):
    class MockResponse:
        status_code = 201
        def json(self):
            return {"id": "order_mock123", "amount": 500, "currency": "INR"}

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: MockResponse())

    resp = client.post("/api/create-order", json={"amount": 500, "currency": "INR"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["order_id"] == "order_mock123"
    assert data["amount"] == 500
    assert data["currency"] == "INR"


def test_verify_payment_success():
    # Setup verify payment payload
    order_id = "order_mock123"
    payment_id = "pay_mock123"
    
    # Calculate valid signature
    secret = "mockkeysecret"
    msg = f"{order_id}|{payment_id}"
    signature = hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    resp = client.post("/api/verify-payment", json={
        "razorpay_payment_id": payment_id,
        "razorpay_order_id": order_id,
        "razorpay_signature": signature
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_verify_payment_signature_mismatch():
    resp = client.post("/api/verify-payment", json={
        "razorpay_payment_id": "pay_mock123",
        "razorpay_order_id": "order_mock123",
        "razorpay_signature": "invalid_sig"
    })
    assert resp.status_code == 400
    assert "signature verification failed" in resp.json()["detail"]

