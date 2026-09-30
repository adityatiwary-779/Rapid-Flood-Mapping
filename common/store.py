"""Key-value state (job records, cache, rate-limit counters). Redis-REST in production, memory in mock/tests."""
import time

import requests


class MemoryStore:
    def __init__(self, clock=time.time):
        self._d, self._exp, self.clock = {}, {}, clock

    def now(self):
        return self.clock()

    def _live(self, key):
        exp = self._exp.get(key)
        if exp is not None and exp <= self.clock():
            self._d.pop(key, None)
            self._exp.pop(key, None)
        return key in self._d

    def get(self, key):
        return self._d[key] if self._live(key) else None

    def set(self, key, value, ex=None, nx=False):
        if nx and self._live(key):
            return False
        self._d[key] = str(value)
        if ex:
            self._exp[key] = self.clock() + ex
        else:
            self._exp.pop(key, None)
        return True

    def incr(self, key, ex=None):
        n = int(self.get(key) or 0) + 1
        keep = self._exp.get(key)
        self._d[key] = str(n)
        if n == 1 and ex:
            self._exp[key] = self.clock() + ex
        elif keep is not None:
            self._exp[key] = keep
        return n

    def delete(self, key):
        self._d.pop(key, None)
        self._exp.pop(key, None)


class UpstashStore:
    """Upstash Redis over its REST API (works from serverless; no persistent connections)."""

    def __init__(self, url, token, timeout=8):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.headers = {"Authorization": f"Bearer {token}"}

    def now(self):
        return time.time()

    def _cmd(self, *cmd):
        r = requests.post(self.url, json=[str(c) for c in cmd], headers=self.headers,
                          timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"Redis error: {body['error']}")
        return body.get("result")

    def get(self, key):
        return self._cmd("GET", key)

    def set(self, key, value, ex=None, nx=False):
        cmd = ["SET", key, value]
        if ex:
            cmd += ["EX", ex]
        if nx:
            cmd.append("NX")
        return self._cmd(*cmd) == "OK"

    def incr(self, key, ex=None):
        n = int(self._cmd("INCR", key))
        if n == 1 and ex:
            self._cmd("EXPIRE", key, ex)
        return n

    def delete(self, key):
        self._cmd("DEL", key)
