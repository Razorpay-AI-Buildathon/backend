import os
import pytest
import json
from fastapi.testclient import TestClient

from app.core.config import settings

@pytest.fixture(scope="module", autouse=True)
def configure_oauth_settings():
    old_mode = settings.GATEWAY_MODE
    old_google_id = settings.GOOGLE_CLIENT_ID
    old_google_secret = settings.GOOGLE_CLIENT_SECRET
    
    settings.GATEWAY_MODE = "SIMULATION"
    settings.GOOGLE_CLIENT_ID = ""
    settings.GOOGLE_CLIENT_SECRET = ""
    
    yield
    
    settings.GATEWAY_MODE = old_mode
    settings.GOOGLE_CLIENT_ID = old_google_id
    settings.GOOGLE_CLIENT_SECRET = old_google_secret

from app.main import app
from app.db.session import SessionLocal, Base, engine
from app.models.case import User
from app.services.redis_cache import RedisCache

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_google_oauth_redirect():
    # 1. Initiating Google auth sets oauth_state cookie and redirects
    resp = client.get("/api/auth/google", follow_redirects=False)
    assert resp.status_code == 307
    assert "oauth_state" in resp.cookies
    state = resp.cookies["oauth_state"]
    assert state != ""


def test_google_oauth_callback_invalid_state():
    # 2. Callback fails if state param does not match cookie state
    resp = client.get("/api/auth/google/callback?code=mock&state=invalid", cookies={"oauth_state": "valid_state"})
    assert resp.status_code == 400
    assert "Invalid or expired OAuth state" in resp.json()["detail"]


def test_google_oauth_callback_mock_success(setup_db):
    # 3. Successful mock callback redirects to frontend dashboard and registers user
    state = "mock_state"
    resp = client.get(
        f"/api/auth/google/callback?code=mock_code&state={state}",
        cookies={"oauth_state": state},
        follow_redirects=False
    )
    assert resp.status_code == 307
    assert "recoverai_session" in resp.cookies
    session_token = resp.cookies["recoverai_session"]
    assert session_token != ""

    # Verify session registered in Redis
    session_data = RedisCache.get(f"session:{session_token}")
    assert session_data is not None
    data = json.loads(session_data)
    assert data["email"] == "operator@recoverai.com"

    # Verify user created in DB
    db = setup_db
    user = db.query(User).filter(User.email == "operator@recoverai.com").first()
    assert user is not None
    assert user.role == "ADMIN" # First user defaults to ADMIN


def test_google_oauth_auth_me(setup_db):
    # Create user
    db = setup_db
    user = User(
        google_subject_id="sub-123",
        email="test_operator@recoverai.com",
        name="Test Operator",
        role="OPERATOR",
        is_active=True
    )
    db.add(user)
    db.commit()

    # Create mock session
    session_token = "sess-abc-123"
    RedisCache.set(f"session:{session_token}", json.dumps({
        "user_id": user.id,
        "email": user.email,
        "role": user.role
    }))

    # Call /auth/me with valid session cookie
    resp = client.get("/api/auth/me", cookies={"recoverai_session": session_token})
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["email"] == "test_operator@recoverai.com"
    assert res_data["role"] == "OPERATOR"


def test_google_oauth_logout():
    # Setup mock session
    session_token = "sess-logout-123"
    RedisCache.set(f"session:{session_token}", json.dumps({
        "user_id": "u1",
        "email": "u1@recoverai.com",
        "role": "OPERATOR"
    }))

    resp = client.post("/api/auth/logout", cookies={"recoverai_session": session_token})
    assert resp.status_code == 200
    assert resp.json() == {"status": "logged_out"}
    
    # Assert session deleted in Redis
    assert RedisCache.get(f"session:{session_token}") is None


def test_operator_role_validation_viewer(setup_db):
    # 4. VIEWERS cannot trigger mutations
    db = setup_db
    user = User(
        google_subject_id="sub-viewer",
        email="viewer@recoverai.com",
        name="Viewer User",
        role="VIEWER",
        is_active=True
    )
    db.add(user)
    db.commit()

    session_token = "sess-viewer"
    RedisCache.set(f"session:{session_token}", json.dumps({
        "user_id": user.id,
        "email": user.email,
        "role": user.role
    }))

    # Attempt to post a human review note
    resp = client.post(
        "/api/cases/case-123/review",
        json={"action": "APPROVE", "operator_id": "op", "notes": "notes"},
        cookies={"recoverai_session": session_token}
    )
    # Since they are VIEWER, they are forbidden (403)
    assert resp.status_code == 403
    assert "Read-only VIEWER role cannot perform mutations" in resp.json()["detail"]
