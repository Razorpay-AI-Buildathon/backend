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


def start_redis_background_services():
    import threading
    import time
    from app.services.queue import RedisQueue, RedisScheduler
    from app.services.worker import RecoveryWorker

    # 1. Start RecoveryWorker dequeue consumer thread
    def run_worker():
        worker = RecoveryWorker()
        worker.run()

    # 2. Start RedisScheduler polling thread
    def run_scheduler():
        scheduler = RedisScheduler()
        queue = RedisQueue()
        while True:
            try:
                due_tasks = scheduler.poll_due_tasks()
                for task in due_tasks:
                    queue.enqueue(task["task_name"], task["payload"])
            except Exception as e:
                print(f"RedisScheduler: Error polling due tasks: {e}")
            time.sleep(2)  # Check scheduled tasks every 2 seconds

    threading.Thread(target=run_worker, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()


@app.on_event("startup")
def startup_event():
    start_redis_background_services()
