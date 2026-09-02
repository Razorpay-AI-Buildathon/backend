import os

class Settings:
    RECOVERAI_API_KEY = os.getenv("RECOVERAI_API_KEY", "RECOVERAI-TESTKEY-12345")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/recoverai_db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://recoverai-ai-service:8001")
    
    # Google OAuth
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")
    
    # Razorpay Integration
    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    RAZORPAY_ENVIRONMENT = os.getenv("RAZORPAY_ENVIRONMENT", "test")
    
    # Gateway Mode (SIMULATION / RAZORPAY_TEST)
    GATEWAY_MODE = os.getenv("GATEWAY_MODE", "SIMULATION")
    
    # Security Session Key (for secure cookies or JWT)
    SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "recoverai-secure-secret-key-987654321")

settings = Settings()
