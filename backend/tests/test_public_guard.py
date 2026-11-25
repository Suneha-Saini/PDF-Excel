"""Public-exposure guard. Run: python -m unittest discover -s tests -v  (from backend/)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.security.public_guard import allowed_when_public, is_public_request


class Classification(unittest.TestCase):
    def test_local_hosts_are_not_public(self):
        for host in ("localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "192.168.1.20:8000", "10.0.0.5", "172.16.3.4:3000",
                     "DELL", "dell:8000", "host.docker.internal:8000", "0.0.0.0:8000"):
            self.assertFalse(is_public_request({"host": host}), host)

    def test_internet_hosts_are_public(self):
        for host in ("superhero-collector-skydiver.ngrok-free.dev", "abc.trycloudflare.com", "8.8.8.8", "example.com:443"):
            self.assertTrue(is_public_request({"Host": host}), host)

    def test_tunnel_headers_make_a_request_public_even_if_the_host_was_rewritten_to_localhost(self):
        for h in ("X-Forwarded-For", "x-forwarded-host", "CF-Connecting-IP", "X-Real-IP"):
            self.assertTrue(is_public_request({"host": "localhost:8000", h: "203.0.113.9"}), h)

    def test_only_the_webhook_paths_are_allowed_through(self):
        self.assertTrue(allowed_when_public("/api/webhooks/xero"))
        self.assertTrue(allowed_when_public("/api/webhooks/quickbooks"))
        for p in ("/", "/docs", "/openapi.json", "/api/ledger/providers", "/api/connections/", "/api/webhooks", "/api/webhooksx/evil"):
            self.assertFalse(allowed_when_public(p), p)


class Middleware(unittest.TestCase):
    def setUp(self):
        app = FastAPI()

        @app.middleware("http")
        async def guard(request, call_next):                       # the same logic main.py installs
            from app.security.public_guard import allowed_when_public as ok, is_public_request as pub
            if pub(request.headers) and not ok(request.url.path):
                return PlainTextResponse("Not Found", status_code=404)
            return await call_next(request)

        @app.get("/api/ledger/providers")
        def private():
            return {"secret": "data"}

        @app.post("/api/webhooks/quickbooks")
        def hook():
            return {"ok": True}

        self.c = TestClient(app, base_url="http://localhost:8000")

    def test_local_use_is_unchanged(self):
        self.assertEqual(self.c.get("/api/ledger/providers").json(), {"secret": "data"})
        self.assertEqual(self.c.post("/api/webhooks/quickbooks").status_code, 200)

    def test_through_a_tunnel_only_the_webhook_answers(self):
        tunnel = {"Host": "superhero-collector-skydiver.ngrok-free.dev", "X-Forwarded-For": "203.0.113.9"}
        r = self.c.get("/api/ledger/providers", headers=tunnel)
        self.assertEqual((r.status_code, r.text), (404, "Not Found"))                     # indistinguishable from a missing page
        self.assertEqual(self.c.get("/docs", headers=tunnel).status_code, 404)
        self.assertEqual(self.c.post("/api/webhooks/quickbooks", headers=tunnel).status_code, 200)

    def test_a_tunnel_that_rewrites_the_host_header_is_still_caught_by_its_forwarding_headers(self):
        r = self.c.get("/api/ledger/providers", headers={"Host": "localhost:8000", "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
