import uuid
import json
from datetime import datetime
from app.models.case import OutboxEvent
from app.services.queue import RedisQueue

def create_outbox_event(db, event_type: str, aggregate_id: str, payload: dict) -> OutboxEvent:
    event = OutboxEvent(
        id=str(uuid.uuid4()),
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        created_at=datetime.utcnow()
    )
    db.add(event)
    return event

class OutboxPublisher:
    @staticmethod
    def publish_pending_events(db) -> int:
        pending = db.query(OutboxEvent).filter(OutboxEvent.published_at == None).order_by(OutboxEvent.created_at.asc()).all()
        published_count = 0
        
        # Initialize queue. We wrap it in a try-except in case Redis is down
        try:
            queue = RedisQueue()
        except Exception as e:
            print(f"OutboxPublisher: Redis client initialization failed: {e}")
            return 0
            
        for event in pending:
            try:
                queue.enqueue(event.event_type, event.payload)
                event.published_at = datetime.utcnow()
                published_count += 1
            except Exception as e:
                event.attempt_count += 1
                event.last_error = str(e)
                print(f"OutboxPublisher: Failed to publish event {event.id}: {e}")
                
        db.commit()
        return published_count
