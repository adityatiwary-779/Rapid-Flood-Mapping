#!/usr/bin/env python3
"""Tiny static file server that applies the "rewrites" from <dir>/vercel.json, to preview the static demo
exactly as the Vercel deployment should serve it.   python tests/static_server.py public 8080"""
import http.server
import json
import os
import re
import sys
import functools


def load_rewrites(root):
    try:
        cfg = json.load(open(os.path.join(root, "vercel.json")))
    except OSError:
        return []
    out = []
    for r in cfg.get("rewrites", []):
        pattern = "^" + re.sub(r":(\w+)", r"(?P<\1>[^/]+)", r["source"]) + "$"
        out.append((re.compile(pattern), r["destination"]))
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, rewrites=(), **k):
        self.rewrites = rewrites
        super().__init__(*a, **k)

    def translate_path(self, path):
        clean = path.split("?")[0].split("#")[0]
        if not os.path.exists(super().translate_path(clean)) or clean.startswith("/api/"):
            for rx, dest in self.rewrites:
                m = rx.match(clean)
                if m:
                    clean = re.sub(r":(\w+)", lambda x: m.group(x.group(1)), dest)
                    break
        return super().translate_path(clean)

    def log_message(self, *a):
        pass


def serve(root, port):
    h = functools.partial(Handler, directory=root, rewrites=load_rewrites(root))
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), h)


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "public"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    print(f"serving {root} on http://localhost:{port}")
    serve(root, port).serve_forever()
