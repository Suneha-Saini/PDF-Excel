"""Webhook status panel + Send-test-event endpoint. Run: python -m unittest discover -s tests -v  (from backend/)"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import sync as sync_ep
from app.api.endpoints import webhooks
from app.database import connection as dbconn
from app.database.models import SyncTarget, User, WebhookEvent
from app.security import auth
from app.services import tunnel


class FakeSystem:
    def __init__(self, clients):
        self._c = clients

    def list_clients(self):
        return self._c


class TestWebhookEndpoint(unittest.TestCase):
    XERO_KEY, QBO_TOKEN = "xero-secret", "qbo-secret"

    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        with Session(self.eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            self.user = u
            s.add_all([SyncTarget(user_id=u.id, provider="xero", client_id="TENANT-1"),
                       SyncTarget(user_id=u.id, provider="quickbooks", client_id="REALM-1")])
            s.commit()

        app = FastAPI()
        app.include_router(sync_ep.router, prefix="/api/sync")
        app.include_router(webhooks.router, prefix="/api/webhooks")

        def test_db():
            with Session(self.eng) as session:
                yield session

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: self.user
        self.http = TestClient(app)
        self.worker = mock.Mock()

        def fake_post(url, content=None, headers=None, timeout=None):      # "the tunnel": straight into the real receiver
            path = url[url.index("/api/webhooks"):]
            r = self.http.post(path, content=content, headers=headers)
            return httpx.Response(r.status_code, content=r.content)

        self.clients = {"xero": [{"id": "TENANT-1", "name": "AOSC"}], "quickbooks": [{"id": "REALM-1", "name": "Sandbox"}]}
        patches = [
            mock.patch.object(webhooks, "worker", self.worker),
            mock.patch.object(sync_ep.settings, "XERO_WEBHOOK_KEY", self.XERO_KEY),
            mock.patch.object(sync_ep.settings, "QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN", self.QBO_TOKEN),
            mock.patch.object(webhooks.settings, "XERO_WEBHOOK_KEY", self.XERO_KEY),
            mock.patch.object(webhooks.settings, "QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN", self.QBO_TOKEN),
            mock.patch.object(sync_ep, "detect_public_url", lambda port=8000: "https://demo.ngrok-free.dev"),
            mock.patch.object(sync_ep.httpx, "post", fake_post),
            mock.patch.object(sync_ep.prov, "provider_overview",
                              lambda db, user: [{"key": "xero", "ready": True, "label": "Xero"},
                                                {"key": "quickbooks", "ready": True, "label": "QuickBooks"},
                                                {"key": "manager_cloud", "ready": True, "label": "Manager"}]),
            mock.patch.object(sync_ep.prov, "provider_for", lambda key, db, user: FakeSystem(self.clients[key])),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_xero_test_event_goes_through_the_receiver_and_is_recorded(self):
        r = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["via"], "tunnel")
        self.assertEqual(d["url"], "https://demo.ngrok-free.dev/api/webhooks/xero")
        self.assertTrue(d["recorded"])
        self.assertEqual(d["matched_targets"], 1)           # the enabled Xero target was matched
        self.worker.enqueue.assert_called()                 # and a sync queued

    def test_quickbooks_test_event_works_too(self):
        d = self.http.post("/api/sync/test-webhook", json={"provider": "quickbooks", "client_id": "REALM-1"}).json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["matched_targets"], 1)

    def test_status_labels_test_events_and_never_leaks_secrets(self):
        self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"})
        r = self.http.get("/api/sync/status")
        d = r.json()
        self.assertEqual(d["public_url"], "https://demo.ngrok-free.dev")
        self.assertEqual(d["webhooks"]["xero"]["register_url"], "https://demo.ngrok-free.dev/api/webhooks/xero")
        self.assertTrue(d["webhooks"]["xero"]["configured"])
        self.assertTrue(d["recent_events"][0]["is_test"])
        self.assertNotIn(self.XERO_KEY, r.text)
        self.assertNotIn(self.QBO_TOKEN, r.text)

    def test_two_clicks_are_not_deduplicated_away(self):
        for _ in range(2):                       # within the same second
            self.assertTrue(self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"}).json()["ok"])
        with Session(self.eng) as s:
            self.assertEqual(s.query(WebhookEvent).count(), 2)

    def test_refuses_manager_and_unknown_provider(self):
        r = self.http.post("/api/sync/test-webhook", json={"provider": "manager_cloud", "client_id": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Xero and QuickBooks", r.json()["detail"])

    def test_refuses_when_the_secret_is_not_loaded(self):
        with mock.patch.object(sync_ep.settings, "XERO_WEBHOOK_KEY", None):
            r = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("XERO_WEBHOOK_KEY", r.json()["detail"])

    def test_refuses_a_client_this_login_cannot_access(self):
        r = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "SOMEONE-ELSES"})
        self.assertEqual(r.status_code, 400)

    def test_a_wrong_key_in_the_receiver_is_reported_as_a_signature_failure(self):
        with mock.patch.object(sync_ep, "_sign", lambda secret, body: "bm90LXRoZS1yaWdodC1zaWduYXR1cmU="):
            d = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"}).json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["status_code"], 401)
        self.assertIn("signature", d["explanation"])

    def test_unreachable_tunnel_is_explained_not_a_500(self):
        def boom(*a, **k):
            raise httpx.ConnectError("down")
        with mock.patch.object(sync_ep.httpx, "post", boom):
            r = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("tunnel", r.json()["explanation"])

    def test_without_a_tunnel_it_says_it_only_tested_locally(self):
        with mock.patch.object(sync_ep, "detect_public_url", lambda port=8000: None):
            d = self.http.post("/api/sync/test-webhook", json={"provider": "xero", "client_id": "TENANT-1"}).json()
        self.assertEqual(d["via"], "local")
        self.assertIn("locally only", d["note"])


class DetectPublicUrl(unittest.TestCase):
    def tunnels(self, *items):
        resp = mock.Mock()
        resp.json.return_value = {"tunnels": [{"public_url": u, "config": {"addr": a}} for u, a in items]}
        return mock.patch.object(tunnel.httpx, "get", return_value=resp)

    def test_picks_the_tunnel_that_points_at_the_backend(self):
        with mock.patch.object(tunnel.settings, "PUBLIC_BASE_URL", None), \
             self.tunnels(("https://front.ngrok-free.dev", "http://localhost:3000"), ("https://api.ngrok-free.dev", "http://localhost:8000")):
            self.assertEqual(tunnel.detect_public_url(8000), "https://api.ngrok-free.dev")

    def test_ignores_a_tunnel_to_the_frontend_only(self):
        with mock.patch.object(tunnel.settings, "PUBLIC_BASE_URL", None), \
             self.tunnels(("https://front.ngrok-free.dev", "http://localhost:3000")):
            self.assertIsNone(tunnel.detect_public_url(8000))

    def test_env_override_wins_and_ngrok_absent_is_none(self):
        with mock.patch.object(tunnel.settings, "PUBLIC_BASE_URL", "https://mine.example.com/"):
            self.assertEqual(tunnel.detect_public_url(), "https://mine.example.com")
        with mock.patch.object(tunnel.settings, "PUBLIC_BASE_URL", None), \
             mock.patch.object(tunnel.httpx, "get", side_effect=httpx.ConnectError("no ngrok")):
            self.assertIsNone(tunnel.detect_public_url())


if __name__ == "__main__":
    unittest.main()
