import os

class Settings:
    RECOVERAI_API_KEY = os.getenv("RECOVERAI_API_KEY", "RECOVERAI-TESTKEY-12345")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/recoverai_db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://recoverai-ai-service:8001")

settings = Settings()
