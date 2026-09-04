import os
import logging
import redis
from typing import Optional

logger = logging.getLogger("recoverai.cache")

from app.core.config import settings
REDIS_URL = settings.REDIS_URL

class RedisCache:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            try:
                cls._client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2.0)
                cls._client.ping()
                logger.info(f"Connected to Redis at {REDIS_URL}")
            except Exception as e:
                logger.warning(f"Failed to connect to Redis at {REDIS_URL}: {e}. Caching is disabled.")
                cls._client = False
        return cls._client

    @classmethod
    def get(cls, key: str) -> Optional[str]:
        client = cls.get_client()
        if not client:
            return None
        try:
            return client.get(key)
        except Exception as e:
            logger.error(f"Redis GET failed for key {key}: {e}")
            return None

    @classmethod
    def set(cls, key: str, value: str, expire_seconds: int = 300) -> bool:
        client = cls.get_client()
        if not client:
            return False
        try:
            client.set(key, value, ex=expire_seconds)
            return True
        except Exception as e:
            logger.error(f"Redis SET failed for key {key}: {e}")
            return False

    @classmethod
    def delete(cls, key: str) -> bool:
        client = cls.get_client()
        if not client:
            return False
        try:
            client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Redis DELETE failed for key {key}: {e}")
            return False

    @classmethod
    def clear_cache_pattern(cls, pattern: str) -> bool:
        client = cls.get_client()
        if not client:
            return False
        try:
            keys = client.keys(pattern)
            if keys:
                client.delete(*keys)
            return True
        except Exception as e:
            logger.error(f"Redis clear pattern {pattern} failed: {e}")
            return False


class RedisLock:
    def __init__(self, lock_key: str, expire_seconds: int = 10):
        import uuid
        self.lock_key = f"lock:{lock_key}"
        self.expire_seconds = expire_seconds
        self.client = RedisCache.get_client()
        self.token = str(uuid.uuid4())

    def __enter__(self) -> bool:
        if not self.client:
            return True # Fail open if Redis is down
        try:
            acquired = self.client.set(self.lock_key, self.token, nx=True, ex=self.expire_seconds)
            return bool(acquired)
        except Exception as e:
            print(f"RedisLock acquire error: {e}")
            return True # Fail open on Redis connectivity issues

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.client:
            return
        # Lua script to release lock atomically only if token matches
        lua_release = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            self.client.eval(lua_release, 1, self.lock_key, self.token)
        except Exception as e:
            print(f"RedisLock release error: {e}")
