import time
from fastapi import HTTPException, status
from app.services.redis_cache import RedisCache

def check_rate_limit(key: str, limit: int = 10, window_seconds: int = 60):
    client = RedisCache.get_client()
    if not client:
        # If Redis is down, fail-open to prevent complete system outage
        return
    
    try:
        now = time.time()
        r_key = f"ratelimit:{key}"
        
        # Sliding window rate limit logic using Redis Sorted Sets (ZSET)
        pipe = client.pipeline()
        pipe.zremrangebyscore(r_key, 0, now - window_seconds)
        pipe.zadd(r_key, {f"{now}-{r_key}": now})
        pipe.zcard(r_key)
        pipe.expire(r_key, window_seconds + 5)
        res = pipe.execute()
        
        count = res[2]
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Rate limit exceeded."
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"RateLimiter: error checking limit: {e}")
