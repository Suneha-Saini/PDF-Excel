"""Webhooks + auto-sync. Run: python -m unittest discover -s tests -v   (from the backend folder)

APIs are faked and the database is in-memory; nothing touches the real DB or any accounting system.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import webhooks
from app.database import connection as dbconn
from app.database.models import CodingRule, LedgerRun, SyncTarget, User, WebhookEvent
from app.services import autosync
from app.services import ledger_coding as lc
from app.services import providers as prov
from app.services.autosync import SyncWorker, due_target_ids, merge_items, sync_target
from app.services.providers.base import LedgerProvider, ProviderError
from app.services.quickbooks_service import QuickBooksError, QuickBooksService
from app.services.xero_service import XeroError, XeroService


def memory_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def line(id_, desc="ACME WIDGETS", amount=-10.0, current="400", status="unassigned", **over):
    base = {"id": id_, "tx_id": id_.split(":")[0], "line_index": 0, "date": "2026-03-01", "description": desc,
            "contact": "", "reference": "", "type": "SPEND", "direction": "spend" if amount < 0 else "receive",
            "amount": amount, "current_code": current, "proposed_code": None, "source": None, "confidence": None,
            "reason": "", "status": status, "bank_account": "Cheque"}
    if status == "locked":
        base.update(proposed_code=current, source="locked")
    base.update(over)
    return base


ACCOUNTS = [{"code": "400", "name": "Office", "class": "EXPENSE"}, {"code": "500", "name": "Rent", "class": "EXPENSE"},
            {"code": "200", "name": "Sales", "class": "REVENUE"}]


# ---------------------------------------------------------------- merging
class Merge(unittest.TestCase):
    def test_human_decisions_and_ai_survive_rules_do_not_and_vanished_lines_go(self):
        old = [line("a:0", proposed_code="500", source="manual", confidence=1.0, reason="Set by you"),
               line("b:0", proposed_code="400", source="rule", confidence=1.0, reason="Rule: x"),
               line("c:0", proposed_code="500", source="ai", confidence=0.9, reason="AI"),
               line("d:0", proposed_code="400", source="ai", confidence=0.4, approved=True),
               line("gone:0", proposed_code="400", source="manual")]
        new = [line("a:0"), line("b:0"), line("c:0"), line("d:0"), line("new:0"),
               line("l:0", status="locked", current="999")]
        out = {i["id"]: i for i in merge_items(old, new)}

        self.assertEqual((out["a:0"]["proposed_code"], out["a:0"]["source"]), ("500", "manual"))
        self.assertIsNone(out["b:0"]["proposed_code"])                      # rule result recomputed, not kept
        self.assertEqual((out["c:0"]["proposed_code"], out["c:0"]["source"]), ("500", "ai"))
        self.assertTrue(out["d:0"]["approved"])
        self.assertNotIn("gone:0", out)
        self.assertEqual(out["new:0"]["status"], "unassigned")
        self.assertEqual(out["l:0"]["source"], "locked")


# ---------------------------------------------------------------- sync_target
class FakeSystem(LedgerProvider):
    key, label, verified = "quickbooks", "QuickBooks Online", True

    def __init__(self, items, error=None):
        self.items, self.error, self.fetches = items, error, []

    def list_clients(self): return [{"id": "R1", "name": "Acme Co"}]
    def default_client_id(self): return "R1"
    def list_accounts(self, client_id): return ACCOUNTS

    def fetch_ledger(self, client_id, d_from, d_to, progress=None):
        self.fetches.append((client_id, d_from, d_to))
        if self.error:
            raise self.error
        return [dict(i) for i in self.items], {"cache": True}

    def apply_changes(self, *a, **k): return {}


class SyncTargetTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 4, 1, 12, 0, 0)
    TODAY = datetime.date(2026, 4, 1)

    def setUp(self):
        self.eng = memory_engine()
        SQLModel.metadata.create_all(self.eng)
        with Session(self.eng) as s:
            user = User(username="u", hashed_password="x")
            s.add(user)
            s.commit()
            s.refresh(user)
            self.uid = user.id
            s.add(CodingRule(user_id=user.id, provider="quickbooks", name="Rent", keywords="landlord", account_code="500"))
            s.add(CodingRule(user_id=user.id, provider="xero", name="Other system", keywords="acme", account_code="200"))
            t = SyncTarget(user_id=user.id, provider="quickbooks", client_id="R1", client_name="Acme Co", lookback_days=30)
            s.add(t)
            s.commit()
            self.tid = t.id
        self.system = FakeSystem([line("a:0", desc="LANDLORD PTY LTD"), line("b:0", desc="Bunnings")])
        p = mock.patch.object(prov, "provider_for", side_effect=lambda *a, **k: self.system)
        p.start()
        self.addCleanup(p.stop)

    def sync(self, trigger="manual", **kw):
        return sync_target(self.tid, trigger, engine=self.eng, now=kw.pop("now", self.NOW), today=self.TODAY)

    def target(self):
        with Session(self.eng) as s:
            return s.get(SyncTarget, self.tid)

    def managed_run(self):
        with Session(self.eng) as s:
            return s.query(LedgerRun).filter(LedgerRun.auto_sync == True).one()  # noqa: E712

    def test_first_sync_creates_the_managed_run_and_applies_only_this_systems_rules(self):
        r = self.sync()
        self.assertEqual(r["status"], "ok")
        run = self.managed_run()
        by_id = {i["id"]: i for i in run.items}
        self.assertEqual((by_id["a:0"]["proposed_code"], by_id["a:0"]["source"]), ("500", "rule"))
        self.assertIsNone(by_id["b:0"]["proposed_code"])        # the 'acme' rule belongs to Xero, not this system
        self.assertEqual((run.provider, run.client_id, run.organization, run.status), ("quickbooks", "R1", "Acme Co", "ready"))
        self.assertEqual((run.date_from, run.date_to), ("2026-03-02", "2026-04-01"))   # 30-day lookback window
        t = self.target()
        self.assertEqual((t.last_status, t.last_synced_at, t.last_trigger), ("ok", self.NOW, "manual"))
        self.assertEqual(t.last_summary["rule_matched"], 1)
        self.assertIsNone(t.lease_until)

    def test_second_sync_keeps_the_users_manual_edit_and_picks_up_new_lines(self):
        self.sync()
        with Session(self.eng) as s:                              # the user recodes b:0 by hand
            run = s.query(LedgerRun).one()
            items = [dict(i) for i in run.items]
            next(i for i in items if i["id"] == "b:0").update(proposed_code="200", source="manual", confidence=1.0)
            run.items = items
            s.add(run)
            s.commit()
        self.system.items.append(line("c:0", desc="New thing"))
        r = self.sync("webhook", now=self.NOW + datetime.timedelta(minutes=5))

        self.assertEqual(r["summary"]["new_lines"], 1)
        by_id = {i["id"]: i for i in self.managed_run().items}
        self.assertEqual((by_id["b:0"]["proposed_code"], by_id["b:0"]["source"]), ("200", "manual"))
        self.assertIn("c:0", by_id)
        with Session(self.eng) as s:
            self.assertEqual(s.query(LedgerRun).count(), 1)       # updated in place, not duplicated

    def test_a_concurrent_user_edit_is_never_overwritten(self):
        self.sync()
        real_refresh, state = lc.refresh, {"first": True}

        def refresh_with_a_user_edit_sneaking_in(items, accounts):
            if state["first"]:
                state["first"] = False
                with Session(self.eng) as other:                  # user saves while we are mid-merge
                    run = other.query(LedgerRun).one()
                    its = [dict(i) for i in run.items]
                    next(i for i in its if i["id"] == "b:0").update(proposed_code="200", source="manual")
                    run.items, run.updated_at = its, datetime.datetime.utcnow()
                    other.add(run)
                    other.commit()
            real_refresh(items, accounts)

        with mock.patch.object(autosync.lc, "refresh", refresh_with_a_user_edit_sneaking_in):
            r = self.sync("nightly", now=self.NOW + datetime.timedelta(hours=1))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(next(i for i in self.managed_run().items if i["id"] == "b:0")["proposed_code"], "200")

    def test_a_run_the_user_is_applying_is_not_touched(self):
        self.sync()
        with Session(self.eng) as s:
            run = s.query(LedgerRun).one()
            run.status = "applying"
            s.add(run)
            s.commit()
        self.assertEqual(self.sync("nightly", now=self.NOW + datetime.timedelta(hours=1))["status"], "deferred")
        self.assertEqual(self.target().last_status, "deferred")

    def test_rate_limit_backs_off_and_scheduled_triggers_wait_but_manual_overrides(self):
        self.system.error = QuickBooksError("QuickBooks API 429: Too Many Requests")
        r = self.sync()
        self.assertEqual(r["status"], "rate_limited")
        self.assertEqual(self.target().backoff_until, self.NOW + datetime.timedelta(minutes=15))

        self.assertEqual(self.sync("nightly", now=self.NOW + datetime.timedelta(minutes=5))["status"], "skipped")
        self.assertEqual(len(self.system.fetches), 1)                          # no extra API call while backing off
        self.system.error = None
        self.assertEqual(self.sync("manual", now=self.NOW + datetime.timedelta(minutes=5))["status"], "ok")
        self.assertIsNone(self.target().backoff_until)

    def test_broken_auth_is_flagged_for_reconnect(self):
        self.system.error = XeroError("Xero token refresh failed (reconnect Xero): invalid_grant")
        self.assertEqual(self.sync()["status"], "needs_reconnect")
        self.assertIn("reconnect", self.target().last_error)
        self.assertGreaterEqual(self.target().backoff_until - self.NOW, datetime.timedelta(hours=6))

    def test_unexpected_errors_are_recorded_not_raised(self):
        self.system.error = ValueError("boom")
        r = self.sync()
        self.assertEqual((r["status"], self.target().last_error), ("error", "boom"))
        self.assertIsNone(self.target().lease_until)

    def test_a_running_sync_blocks_a_second_one(self):
        with Session(self.eng) as s:
            t = s.get(SyncTarget, self.tid)
            t.lease_until = self.NOW + datetime.timedelta(minutes=5)
            s.add(t)
            s.commit()
        self.assertEqual(self.sync()["reason"], "a sync is already running")
        self.assertEqual(len(self.system.fetches), 0)

    def test_disabled_targets_do_nothing(self):
        with Session(self.eng) as s:
            t = s.get(SyncTarget, self.tid)
            t.enabled = False
            s.add(t)
            s.commit()
        self.assertEqual(self.sync()["status"], "skipped")
        self.assertEqual(len(self.system.fetches), 0)


# ---------------------------------------------------------------- scheduling + worker
class Scheduling(unittest.TestCase):
    def test_due_logic_covers_first_sync_nightly_catch_up_and_back_off(self):
        eng = memory_engine()
        SQLModel.metadata.create_all(eng)
        now = datetime.datetime(2026, 4, 2, 3, 0)          # 03:00, after a 02:00 nightly hour
        with Session(eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)

            def add(cid, **kw):
                t = SyncTarget(user_id=u.id, provider="xero", client_id=cid, **kw)
                s.add(t)
                s.commit()
                return str(t.id)

            never = add("never")
            stale = add("stale", last_synced_at=now - datetime.timedelta(hours=25))
            fresh = add("fresh", last_synced_at=now - datetime.timedelta(hours=3))
            off = add("off", enabled=False)
            backing = add("backing", backoff_until=now + datetime.timedelta(hours=1))
            leased = add("leased", lease_until=now + datetime.timedelta(minutes=5))
            ancient = add("ancient", last_synced_at=now - datetime.timedelta(hours=60))

            due = set(due_target_ids(s, now, now, nightly_hour=2, max_age_hours=24))
            self.assertEqual(due, {never, stale, ancient})
            # before the nightly hour, 25h-stale waits; very stale (2x) and never-synced do not
            early = datetime.datetime(2026, 4, 2, 1, 0)
            self.assertEqual(set(due_target_ids(s, now, early, 2, 24)), {never, ancient})
            self.assertTrue({fresh, off, backing, leased}.isdisjoint(due))

    def test_worker_coalesces_spaces_webhook_bursts_and_survives_job_errors(self):
        clock = {"t": 1000.0}
        calls = []

        def runner(tid, trig):
            calls.append((tid, trig))
            if tid == "bad":
                raise RuntimeError("crash")
            return {}

        w = SyncWorker(runner=runner, clock=lambda: clock["t"], min_gap_seconds=30)
        for _ in range(50):                                   # a burst of 50 events for one client
            w.enqueue("t1", "webhook")
        w.enqueue("bad", "manual")
        self.assertEqual(len(w.pending()), 2)

        self.assertEqual(w.run_due(), 1)                      # manual (due now) runs; the webhook waits ~1s
        self.assertEqual(calls, [("bad", "manual")])
        clock["t"] += 2
        self.assertEqual(w.run_due(), 1)
        self.assertEqual(calls[-1], ("t1", "webhook"))

        w.enqueue("t1", "webhook")                            # another event right after: spaced by the gap
        clock["t"] += 5
        self.assertEqual(w.run_due(), 0)
        clock["t"] += 30
        self.assertEqual(w.run_due(), 1)
        self.assertEqual(sum(1 for c in calls if c[0] == "t1"), 2)    # 51 events -> 2 syncs, not 51


# ---------------------------------------------------------------- webhooks
class WebhookEndpoints(unittest.TestCase):
    XERO_KEY, QBO_TOKEN = "xero-secret", "qbo-secret"

    def setUp(self):
        self.eng = memory_engine()
        SQLModel.metadata.create_all(self.eng)
        with Session(self.eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            self.on = SyncTarget(user_id=u.id, provider="xero", client_id="TENANT-1")
            off = SyncTarget(user_id=u.id, provider="xero", client_id="TENANT-OFF", enabled=False)
            qb = SyncTarget(user_id=u.id, provider="quickbooks", client_id="REALM-1")
            s.add_all([self.on, off, qb])
            s.commit()
            self.on_id, self.qb_id = str(self.on.id), str(qb.id)

        app = FastAPI()
        app.include_router(webhooks.router, prefix="/hooks")

        def test_db():
            with Session(self.eng) as session:
                yield session

        app.dependency_overrides[dbconn.get_db] = test_db
        self.http = TestClient(app)
        self.worker = mock.Mock()
        for p in (mock.patch.object(webhooks, "worker", self.worker),
                  mock.patch.object(webhooks.settings, "XERO_WEBHOOK_KEY", self.XERO_KEY),
                  mock.patch.object(webhooks.settings, "QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN", self.QBO_TOKEN)):
            p.start()
            self.addCleanup(p.stop)

    def post_xero(self, events, key=None, sig=None):
        body = json.dumps({"events": events, "firstEventSequence": 1, "lastEventSequence": len(events)}).encode()
        return self.http.post("/hooks/xero", content=body,
                              headers={"x-xero-signature": sig if sig is not None else sign(key or self.XERO_KEY, body)})

    def event(self, tenant="TENANT-1", rid="r1", **kw):
        return {"tenantId": tenant, "eventCategory": "INVOICE", "eventType": "UPDATE", "resourceId": rid,
                "eventDateUtc": "2026-04-01T01:00:00", **kw}

    def count_events(self):
        with Session(self.eng) as s:
            return s.query(WebhookEvent).count()

    def test_bad_signature_is_rejected_before_anything_is_stored(self):
        self.assertEqual(self.post_xero([self.event()], key="wrong").status_code, 401)
        self.assertEqual(self.post_xero([self.event()], sig="").status_code, 401)
        self.assertEqual(self.count_events(), 0)
        self.worker.enqueue.assert_not_called()

    def test_xero_intent_to_receive_with_empty_events_is_accepted(self):
        self.assertEqual(self.post_xero([]).status_code, 200)

    def test_xero_event_queues_one_webhook_sync_for_the_enabled_target_only(self):
        r = self.post_xero([self.event(rid="a"), self.event(rid="b"), self.event(tenant="TENANT-OFF"),
                            self.event(tenant="UNKNOWN")])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"")
        self.worker.enqueue.assert_called_once_with(self.on_id, "webhook")     # 2 events, 1 target -> 1 job
        self.assertEqual(self.count_events(), 4)

    def test_redelivery_is_ignored(self):
        self.post_xero([self.event()])
        self.worker.reset_mock()
        self.assertEqual(self.post_xero([self.event()]).status_code, 200)
        self.worker.enqueue.assert_not_called()
        self.assertEqual(self.count_events(), 1)

    def test_not_configured_means_503_not_a_silent_accept(self):
        with mock.patch.object(webhooks.settings, "XERO_WEBHOOK_KEY", None):
            self.assertEqual(self.post_xero([self.event()]).status_code, 503)

    def post_qbo(self, payload, token=None):
        body = json.dumps(payload).encode()
        return self.http.post("/hooks/quickbooks", content=body, headers={"intuit-signature": sign(token or self.QBO_TOKEN, body)})

    def test_quickbooks_relevant_entities_trigger_a_sync_others_do_not(self):
        payload = {"eventNotifications": [{"realmId": "REALM-1", "dataChangeEvent": {"entities": [
            {"name": "Purchase", "id": "1", "operation": "Create", "lastUpdated": "t1"},
            {"name": "Deposit", "id": "2", "operation": "Update", "lastUpdated": "t2"},
            {"name": "Customer", "id": "3", "operation": "Update", "lastUpdated": "t3"}]}}]}
        self.assertEqual(self.post_qbo(payload).status_code, 200)
        self.worker.enqueue.assert_called_once_with(self.qb_id, "webhook")
        self.assertEqual(self.count_events(), 2)                              # Customer ignored

    def test_quickbooks_bad_signature_is_rejected(self):
        self.assertEqual(self.post_qbo({"eventNotifications": []}, token="nope").status_code, 401)


# ---------------------------------------------------------------- rotated refresh tokens
class RefreshRace(unittest.TestCase):
    """Two threads, one rotating refresh token: the loser must adopt the winner's token, not reuse a spent one."""

    def values(self):
        return {"client_id": "c", "client_secret": "s", "access_token": "old-a", "refresh_token": "old-r",
                "expires_at": (datetime.datetime.utcnow() - datetime.timedelta(minutes=5)).isoformat(),
                "tenant_id": "T", "realm_id": "R"}

    def fresh_stored(self):
        return {"access_token": "new-a", "refresh_token": "new-r",
                "expires_at": (datetime.datetime.utcnow() + datetime.timedelta(minutes=30)).isoformat()}

    def check(self, service_cls, module, error_cls):
        # 1) another thread already refreshed -> adopt its tokens, make NO call to the token endpoint
        svc = service_cls(self.values(), reload_tokens=self.fresh_stored)
        with mock.patch.object(module.httpx, "Client", side_effect=AssertionError("must not call the token endpoint")):
            svc._refresh()
        self.assertEqual((svc.access_token, svc.refresh_token), ("new-a", "new-r"))

        # 2) nobody else refreshed -> do the refresh ourselves, exactly once, and persist the rotated pair
        persisted = []
        svc = service_cls(self.values(), persist_tokens=persisted.append, reload_tokens=lambda: {"refresh_token": "old-r"})
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"access_token": "x-a", "refresh_token": "x-r", "expires_in": 1800}
        client = mock.MagicMock()
        client.__enter__.return_value.post.return_value = resp
        with mock.patch.object(module.httpx, "Client", return_value=client):
            svc._refresh()
        self.assertEqual((svc.access_token, svc.refresh_token), ("x-a", "x-r"))
        self.assertEqual(client.__enter__.return_value.post.call_count, 1)
        self.assertEqual(persisted[0]["refresh_token"], "x-r")

        # 3) a failed refresh is an actionable error
        resp.status_code, resp.text = 400, "invalid_grant"
        svc = service_cls(self.values())
        with mock.patch.object(module.httpx, "Client", return_value=client):
            with self.assertRaises(error_cls):
                svc._refresh()

    def test_xero(self):
        from app.services import xero_service
        self.check(XeroService, xero_service, XeroError)

    def test_quickbooks(self):
        from app.services import quickbooks_service
        self.check(QuickBooksService, quickbooks_service, QuickBooksError)


if __name__ == "__main__":
    unittest.main()
