import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.endpoints import router as api_router
from app.db.session import engine, Base

# Create tables in the db engine on application startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="RecoverAI Backend API",
    description="Deterministic Safety Actions & Recovery Metrics API for Razorpay AI Revenue Recovery Buildathon",
    version="1.0.0",
)

# Configure CORS origins
allowed_origins_str = os.getenv("ALLOWED_CORS_ORIGINS", "http://localhost:3000")
origins = [
    origin.strip() for origin in allowed_origins_str.split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include main router
app.include_router(api_router)
