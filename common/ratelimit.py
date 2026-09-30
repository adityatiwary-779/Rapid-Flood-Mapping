"""Fixed-window rate limits keyed by a salted hash of the client IP (raw IPs are never stored)."""
import hashlib


class RateLimited(Exception):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.message, self.retry_after = message, int(retry_after)


def client_id(headers, salt):
    xff = headers.get("x-forwarded-for") or ""
    ip = xff.split(",")[0].strip() or headers.get("x-real-ip") or "unknown"
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


def hit(store, action, cid, limit, window_s, message):
    """Count one call; raise RateLimited when over the limit."""
    now = store.now()
    bucket = int(now // window_s)
    n = store.incr(f"rl:{action}:{cid}:{bucket}", ex=window_s)
    if n > limit:
        raise RateLimited(message, window_s - (now % window_s))
