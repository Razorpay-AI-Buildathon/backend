import asyncio
import json

class SSEManager:
    def __init__(self):
        self.connections = set()
        self.loop = None
        
    def register(self, queue: asyncio.Queue):
        self.connections.add(queue)
        
    def unregister(self, queue: asyncio.Queue):
        self.connections.discard(queue)
        
    def publish(self, event_type: str, data: dict):
        payload = {
            "event": event_type,
            "data": data
        }
        if not self.loop:
            try:
                self.loop = asyncio.get_event_loop()
            except RuntimeError:
                pass
                
        if self.loop and self.loop.is_running():
            for q in list(self.connections):
                self.loop.call_soon_threadsafe(q.put_nowait, payload)

sse_manager = SSEManager()
