"""Remembered payee rules. Run: python -m unittest discover -s tests -v  (from backend/)"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import ledger as ledger_api
from app.api.endpoints import payee_rules as payee_api
from app.database import connection as dbconn
from app.database.models import HistoryImport, LedgerRun, PayeeRule, SuggestionDecision, User
from app.security import auth
from app.services import accuracy as acc
from app.services import ledger_coding as lc
from app.services import payee_rules as svc
from app.services import providers as prov
from app.services.coding.text import payee_key, payee_matches
from app.services.providers.base import LedgerProvider

ACCOUNTS = [{"code": "200", "name": "Sales", "class": "REVENUE"}, {"code": "310", "name": "Repairs", "class": "EXPENSE"},
            {"code": "404", "name": "Bank Fees", "class": "EXPENSE"}, {"code": "420", "name": "Fuel", "class": "EXPENSE"}]
NAMES = {a["code"]: a["name"] for a in ACCOUNTS}
_n = 0


def item(contact="Bunnings", amount=-50.0, desc="Timber", current="310", status="unassigned", **kw):
    global _n
    _n += 1
    it = {"id": f"i{_n}:0", "tx_id": f"i{_n}", "line_index": 0, "date": f"2026-03-{(_n % 28) + 1:02d}", "description": desc, "contact": contact,
          "reference": "", "type": "SPEND", "direction": "spend" if amount < 0 else "receive", "amount": amount,
          "current_code": current, "proposed_code": None, "source": None, "confidence": None, "reason": "", "status": status,
          "bank_account": "Cheque"}
    it.update(kw)
    return it


def rule(key="c:bunnings", direction="spend", code="420", display="Bunnings", id_=1, active=True):
    return {"id": id_, "payee_key": key, "payee_display": display, "direction": direction, "account_code": code, "active": active}


class Keys(unittest.TestCase):
    def test_a_named_contact_gives_an_exact_key_otherwise_the_meaningful_words(self):
        self.assertEqual(payee_key(item(contact="  Bunnings Warehouse ")), ("c:bunnings warehouse", "Bunnings Warehouse"))
        k, d = payee_key(item(contact="", desc="CARD 1234 BRIDGE SELECT PTY LTD MELBOURNE AU"))
        self.assertEqual((k, d[:11]), ("d:BRIDGE MELBOURNE SELECT", "CARD 1234 B"))
        self.assertEqual(payee_key(item(contact="", desc="CARD 1234 5678"))[0], None)

    def test_matching(self):
        self.assertTrue(payee_matches("c:bunnings", "c:bunnings"))
        self.assertFalse(payee_matches("c:bunnings", "c:bunnings warehouse"))                    # contacts are exact
        self.assertTrue(payee_matches("d:ALPHA BRAVO CHARLIE DELTA ECHO", "d:ALPHA BRAVO CHARLIE DELTA"))     # 4/5 = 0.8
        self.assertFalse(payee_matches("d:ALPHA BRAVO CHARLIE DELTA", "d:ALPHA BRAVO CHARLIE"))               # 3/4 = 0.75: too loose
        self.assertFalse(payee_matches("d:BRIDGE", "d:BRIDGE SELECT"))                            # one word is never fuzzy
        self.assertFalse(payee_matches("c:bridge select", "d:BRIDGE SELECT"))                     # a contact never matches words
        self.assertFalse(payee_matches(None, "c:x"))


class PlaceholderContacts(unittest.TestCase):
    """This app's own statement export files every line under 'Suspense Contact'. That is not a payee."""

    def test_a_placeholder_contact_is_no_contact_so_the_payee_falls_back_to_the_words(self):
        from app.services.coding.text import contact_key, item_tokens
        t = item(contact="Suspense Contact", desc="CARD 1234 OFFICEWORKS MELBOURNE")
        self.assertEqual(contact_key(t), "")
        self.assertEqual(payee_key(t)[0], "d:MELBOURNE OFFICEWORKS")
        self.assertNotIn("SUSPENSE", item_tokens(t))
        self.assertEqual(payee_key(item(contact="Real Supplier"))[0], "c:real supplier")

    def test_history_is_not_inflated_by_a_shared_placeholder_contact(self):
        from app.services.coding.decide import assess
        from app.services.coding.history import HistoryIndex
        pool = [item(contact="Suspense Contact", desc=f"UNRELATED NARRATIVE {w}", current="310") for w in ("ALPHA", "BRAVO", "CHARLIE", "DELTA")] * 4
        target = item(contact="Suspense Contact", desc="COMPLETELY DIFFERENT PAYEE", current="420")
        ev = HistoryIndex(pool + [target]).evidence_for(target)
        self.assertIsNone(ev)                                  # before the fix: every pair got a 0.8 'same supplier' floor
        self.assertEqual(assess(target, ev, NAMES).outcome, "no_evidence")

    def test_two_lines_from_the_same_real_supplier_are_still_the_same_supplier(self):
        from app.services.coding.history import HistoryIndex
        pool = [item(contact="Bunnings", desc=f"INV {i} some different words {i}x", current="310") for i in range(12)]
        target = item(contact="Bunnings", desc="totally other narrative", current="420")
        self.assertEqual(HistoryIndex(pool + [target]).evidence_for(target).by_account[0]["code"], "310")


class Applying(unittest.TestCase):
    def apply(self, items, rules, accounts=ACCOUNTS):
        lc.apply_payee_rules(items, rules, accounts)
        lc.refresh(items, accounts)
        return {i["id"]: i for i in items}

    def test_matches_by_payee_and_direction_and_is_trusted(self):
        spend, receive = item(), item(amount=50.0)
        out = self.apply([spend, receive], [rule()])
        self.assertEqual((out[spend["id"]]["proposed_code"], out[spend["id"]]["source"], out[spend["id"]]["status"]), ("420", "payee", "ok"))
        self.assertEqual(out[spend["id"]]["payee_rule_id"], 1)
        self.assertIsNone(out[receive["id"]]["source"])                    # same payee, other direction: no rule

    def test_it_beats_keyword_rules_history_and_ai_but_never_a_persons_own_decision(self):
        kw, hist, ai = [item(proposed_code="310", source=s) for s in ("rule", "history", "ai")]
        manual, keep, approved = item(proposed_code="404", source="manual"), item(proposed_code="404", source="keep"), item(proposed_code="404", source="history", approved=True)
        locked = item(status="locked", proposed_code="310", source="locked")
        out = self.apply([kw, hist, ai, manual, keep, approved, locked], [rule()])
        self.assertEqual([out[x["id"]]["source"] for x in (kw, hist, ai)], ["payee"] * 3)
        self.assertEqual([out[x["id"]]["proposed_code"] for x in (manual, keep, approved, locked)], ["404", "404", "404", "310"])
        self.assertEqual([out[x["id"]]["source"] for x in (manual, keep, approved, locked)], ["manual", "keep", "history", "locked"])

    def test_later_stages_do_not_overwrite_a_payee_result(self):
        t = item()
        lc.apply_payee_rules([t], [rule()], ACCOUNTS)
        lc.apply_rules([t], [{"id": 9, "name": "timber", "keywords": "timber", "direction": "any", "account_code": "310", "priority": 1, "active": True}], overwrite=True)
        self.assertEqual((t["source"], t["proposed_code"]), ("payee", "420"))
        history = [item(current="310", desc="Timber") for _ in range(30)]
        lc.apply_history([t], history, ACCOUNTS)
        self.assertEqual((t["source"], t["proposed_code"]), ("payee", "420"))
        self.assertIn("Payee rule says 420", t["history_conflict"])        # strong history disagrees: shown, not obeyed
        calls = []
        orig = lc.complete_json
        lc.complete_json = lambda *a, **k: calls.append(a) or {"assignments": []}
        try:
            lc.ai_categorise([t], ACCOUNTS, object())
        finally:
            lc.complete_json = orig
        self.assertEqual(calls, [])

    def test_removing_or_disabling_a_rule_takes_effect_on_reapply(self):
        t = item()
        self.apply([t], [rule()])
        out = self.apply([t], [])
        self.assertEqual((out[t["id"]]["source"], out[t["id"]]["proposed_code"], out[t["id"]]["status"]), (None, None, "unassigned"))
        out = self.apply([t], [rule(active=False)])
        self.assertIsNone(out[t["id"]]["source"])

    def test_an_approved_payee_result_stays_when_the_rule_goes(self):
        t = item()
        self.apply([t], [rule()])
        t["approved"] = True
        self.assertEqual(self.apply([t], [])[t["id"]]["proposed_code"], "420")

    def test_a_rule_pointing_at_an_account_not_in_the_chart_is_ignored(self):
        t = item()
        self.assertIsNone(self.apply([t], [rule(code="999")])[t["id"]]["source"])

    def test_loose_matching_for_payees_known_only_by_their_words(self):
        t = item(contact="", desc="Fast Transfer From BRIDGE SELECT MELBOURNE")
        key, _ = payee_key(t)
        out = self.apply([t], [rule(key=key, display="bridge select")])
        self.assertEqual(out[t["id"]]["source"], "payee")
        other = item(contact="", desc="Totally different narrative here")
        self.assertIsNone(self.apply([other], [rule(key=key)])[other["id"]]["source"])

    def test_a_payee_rule_line_that_looks_wrong_for_its_direction_goes_to_review(self):
        t = item(amount=-50.0)
        out = self.apply([t], [rule(code="200")])                          # money out to a REVENUE account
        self.assertEqual(out[t["id"]]["status"], "review")


class Candidates(unittest.TestCase):
    def hist(self, contact, code, n, amount=-50.0):
        return [item(contact=contact, current=code, amount=amount) for _ in range(n)]

    def test_consistent_payees_without_a_rule_are_proposed_biggest_first(self):
        h = (self.hist("Bunnings", "310", 12) + self.hist("Shell", "420", 5) + self.hist("Mixed Co", "310", 5) + self.hist("Mixed Co", "420", 5) +
             self.hist("Rare", "310", 2) + self.hist("Archived", "999", 9) + self.hist("Has Rule", "310", 9))
        got = svc.candidates(h, NAMES, existing={("c:has rule", "spend")})
        self.assertEqual([(c["payee_display"], c["lines"], c["account_code"]) for c in got], [("Bunnings", 12, "310"), ("Shell", 5, "420")])
        self.assertEqual((got[0]["agree_pct"], got[0]["account_name"], got[0]["direction"]), (100.0, "Repairs", "spend"))

    def test_the_agreement_threshold_and_direction_are_respected(self):
        h = self.hist("Acme", "310", 9) + self.hist("Acme", "420", 1)       # 90% -> qualifies
        self.assertEqual(len(svc.candidates(h, NAMES, [])), 1)
        self.assertEqual(svc.candidates(h, NAMES, [], min_share=0.95), [])
        split_dir = self.hist("Acme", "200", 4, amount=50.0) + self.hist("Acme", "310", 4)
        self.assertEqual(sorted(c["direction"] for c in svc.candidates(split_dir, NAMES, [])), ["receive", "spend"])


class Remembering(unittest.TestCase):
    def test_only_a_persons_decisions_become_rules(self):
        mine, approved = item(proposed_code="420", source="manual"), item(contact="Shell", proposed_code="420", source="history", approved=True)
        system = [item(contact="By Rule", proposed_code="310", source="rule"), item(contact="By History", proposed_code="310", source="history"),
                  item(contact="By AI", proposed_code="310", source="ai"), item(contact="By Payee", proposed_code="310", source="payee"),
                  item(contact="Locked", status="locked", proposed_code="310", source="manual")]
        r = svc.remembered_entries([mine, approved] + system, NAMES)
        self.assertEqual(sorted(e["payee_display"] for e in r["entries"]), ["Bunnings", "Shell"])
        self.assertEqual(r["skipped"]["not a person's decision"], 5)

    def test_ambiguous_missing_and_unknown_are_skipped_and_ids_can_narrow_it(self):
        a, b = item(contact="Twice", proposed_code="420", source="manual"), item(contact="Twice", proposed_code="310", source="manual")
        nopayee = item(contact="", desc="1234 5678", proposed_code="420", source="manual")
        unknown = item(contact="Archived", proposed_code="999", source="manual")
        ok = item(contact="Fine", proposed_code="420", source="manual")
        r = svc.remembered_entries([a, b, nopayee, unknown, ok], NAMES)
        self.assertEqual([e["payee_display"] for e in r["entries"]], ["Fine"])
        self.assertEqual((r["skipped"]["payee coded to two accounts"], r["skipped"]["no payee on the line"], r["skipped"]["account not in the chart"]), (1, 1, 1))
        only = svc.remembered_entries([a, b, ok], NAMES, only_ids={ok["id"]})
        self.assertEqual([e["payee_display"] for e in only["entries"]], ["Fine"])


class Rows(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        self.db = Session(self.eng)
        self.addCleanup(self.db.close)
        u = User(username="u", hashed_password="x")
        self.db.add(u)
        self.db.commit()
        self.db.refresh(u)
        self.uid = u.id

    def entry(self, key="c:bunnings", code="420", direction="spend", display="Bunnings"):
        return {"payee_key": key, "payee_display": display, "direction": direction, "account_code": code, "account_name": NAMES.get(code)}

    def test_one_batch_per_create_duplicates_skipped_and_abn_attached(self):
        r = svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry(), self.entry("c:shell", "420", display="Shell")], "history",
                             abns={"bunnings": "51824753556"})
        self.db.commit()
        self.assertEqual((r["created"], r["skipped"], bool(r["batch_id"])), (2, 0, True))
        again = svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry()], "history")
        self.assertEqual((again["created"], again["skipped"], again["batch_id"]), (0, 1, None))
        rows = {x.payee_display: x for x in self.db.query(PayeeRule).all()}
        self.assertEqual((rows["Bunnings"].abn, rows["Shell"].abn, rows["Bunnings"].batch_id == rows["Shell"].batch_id), ("51824753556", None, True))

    def test_a_reviewers_correction_repoints_the_rule_and_resets_its_overrides(self):
        svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry(code="420")], "history")
        row = self.db.query(PayeeRule).one()
        row.overrides, row.active = 3, False
        self.db.add(row)
        self.db.commit()
        r = svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry(code="310")], "remembered", update_existing=True)
        self.db.commit()
        row = self.db.query(PayeeRule).one()
        self.assertEqual((r["created"], r["updated"], row.account_code, row.overrides, row.active), (0, 1, "310", 0, True))

    def test_rules_are_per_client(self):
        svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry()], "history")
        svc.create_rules(self.db, self.uid, "xero", "C2", [self.entry(code="310")], "history")
        self.db.commit()
        self.assertEqual([r["account_code"] for r in svc.rule_dicts(self.db, self.uid, "xero", "C1")], ["420"])
        self.assertEqual(svc.rule_dicts(self.db, self.uid, "xero", None), [])

    def test_usage_is_computed_from_history(self):
        svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry(code="420")], "history")
        self.db.commit()
        hist = [item(current="420", date="2026-01-05"), item(current="420", date="2026-02-09"), item(current="310", date="2026-03-01"),
                item(contact="Other", current="420")]
        for h in hist:
            h["date"] = {0: "2026-01-05", 1: "2026-02-09", 2: "2026-03-01"}.get(hist.index(h), "2025-01-01")
        u = svc.usage(hist, self.db.query(PayeeRule).all())
        r = self.db.query(PayeeRule).one()
        self.assertEqual(u[r.id], {"times": 3, "agree": 2, "last_seen": "2026-03-01"})

    def test_overrides_are_counted(self):
        svc.create_rules(self.db, self.uid, "xero", "C1", [self.entry()], "history")
        self.db.commit()
        r = self.db.query(PayeeRule).one()
        svc.note_override(self.db, r.id)
        svc.note_override(self.db, None)
        svc.note_override(self.db, 9999)
        self.db.commit()
        self.assertEqual(self.db.query(PayeeRule).one().overrides, 1)

    def test_overriding_a_payee_rule_line_is_recorded_as_a_decision_on_that_suggestion(self):
        run = LedgerRun(user_id=self.uid, provider="xero", client_id="C1", organization="Acme", date_from="a", date_to="b", status="ready", items=[], accounts=ACCOUNTS)
        self.db.add(run)
        self.db.commit()
        before = item(proposed_code="420", source="payee", confidence=1.0)
        self.assertTrue(acc.record_decision(self.db, run, before, "310", "edit"))
        self.db.commit()
        row = self.db.query(SuggestionDecision).one()
        self.assertEqual((row.source, row.outcome), ("payee", "corrected"))
        self.assertEqual(acc.summary(self.db, self.uid, None, None)["by_confidence"][0]["bucket"], "payee rule")


# ---------------------------------------------------------------- API
class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def list_clients(self): return [{"id": "C1", "name": "Acme"}]
    def default_client_id(self): return "C1"
    def list_accounts(self, client_id): return ACCOUNTS
    def fetch_ledger(self, *a, **k): return [], {}
    def apply_changes(self, *a, **k): return {}
    def list_suppliers(self, client_id): return [{"id": "s1", "name": "Bunnings", "abn": "51 824 753 556"}]


class Api(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        with Session(self.eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            self.uid = u.id
            hist = [dict(item(contact="Bunnings", current="310"), origin="import") for _ in range(8)] + \
                   [dict(item(contact="Shell", current="420"), origin="import") for _ in range(4)] + \
                   [dict(item(contact="Mixed", current="310"), origin="import") for _ in range(3)] + \
                   [dict(item(contact="Mixed", current="420"), origin="import") for _ in range(3)]
            s.add(HistoryImport(user_id=self.uid, provider="xero", client_id="C1", filename="f.csv", items=hist, lines=len(hist)))
            s.commit()
        app = FastAPI()
        app.include_router(ledger_api.router, prefix="/ledger")
        app.include_router(payee_api.router, prefix="/payee-rules")

        def test_db():
            with Session(self.eng) as s:
                yield s

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: SimpleNamespace(id=self.uid)
        self.http = TestClient(app)
        overview = [{"key": "xero", "label": "Xero", "status": "connected", "ready": True, "verified": True, "supports_apply": True, "note": ""}]
        for p in (mock.patch.object(prov, "provider_overview", return_value=overview), mock.patch.object(prov, "provider_for", return_value=FakeSystem())):
            p.start()
            self.addCleanup(p.stop)

    P = {"provider": "xero", "client_id": "C1"}

    def seed_run(self, *items):
        with Session(self.eng) as s:
            r = LedgerRun(user_id=self.uid, provider="xero", client_id="C1", organization="Acme", date_from="a", date_to="b", status="ready",
                          items=list(items), accounts=ACCOUNTS)
            s.add(r)
            s.commit()
            return str(r.id)

    def test_candidates_then_create_the_ticked_ones_as_a_batch_with_usage_and_abn(self):
        c = self.http.get("/payee-rules/candidates", params=self.P).json()["candidates"]
        self.assertEqual([(x["payee_display"], x["lines"]) for x in c], [("Bunnings", 8), ("Shell", 4)])       # "Mixed" is 50/50: not proposed
        r = self.http.post("/payee-rules", json={**self.P, "entries": [{k: x[k] for k in ("payee_key", "payee_display", "direction", "account_code")} for x in c]})
        self.assertEqual((r.status_code, r.json()["created"]), (200, 2))
        listing = self.http.get("/payee-rules", params=self.P).json()
        by = {x["payee"]: x for x in listing["rules"]}
        self.assertEqual((by["Bunnings"]["times"], by["Bunnings"]["agree"], by["Bunnings"]["abn"], by["Bunnings"]["account_name"]), (8, 8, "51824753556", "Repairs"))
        self.assertEqual((listing["last_batch"]["rules"], listing["payees_known"]), (2, 3))
        # created rules no longer appear as candidates
        self.assertEqual(self.http.get("/payee-rules/candidates", params=self.P).json()["candidates"], [])

    def test_create_validation(self):
        e = {"payee_key": "c:bunnings", "payee_display": "Bunnings", "direction": "spend", "account_code": "310"}
        self.assertEqual(self.http.post("/payee-rules", json={**self.P, "entries": [{**e, "account_code": "999"}]}).status_code, 400)
        self.assertEqual(self.http.post("/payee-rules", json={**self.P, "entries": [{**e, "direction": "sideways"}]}).status_code, 400)
        self.assertEqual(self.http.post("/payee-rules", json={**self.P, "entries": [{**e, "payee_key": "bunnings"}]}).status_code, 400)
        self.assertEqual(self.http.post("/payee-rules", json={**self.P, "client_id": "NOT-MINE", "entries": [e]}).status_code, 400)
        self.assertEqual(self.http.get("/payee-rules/candidates", params={**self.P, "min_lines": 1}).status_code, 400)

    def test_edit_disable_delete_and_undo_only_the_latest_batch(self):
        mk = lambda key, name: self.http.post("/payee-rules", json={**self.P, "entries": [{"payee_key": key, "payee_display": name, "direction": "spend", "account_code": "310"}]}).json()
        first, second = mk("c:a", "A"), mk("c:b", "B")
        rules = {r["payee"]: r for r in self.http.get("/payee-rules", params=self.P).json()["rules"]}
        self.assertEqual(self.http.patch(f"/payee-rules/{rules['A']['id']}", json={"account_code": "420", "active": False}).status_code, 200)
        self.assertEqual(self.http.patch(f"/payee-rules/{rules['A']['id']}", json={"account_code": "999"}).status_code, 400)
        a = {r["payee"]: r for r in self.http.get("/payee-rules", params=self.P).json()["rules"]}["A"]
        self.assertEqual((a["account_code"], a["active"]), ("420", False))

        undo = self.http.post("/payee-rules/undo-last-batch", json=self.P).json()
        self.assertEqual((undo["removed"], undo["batch_id"] == second["batch_id"]), (1, True))
        self.assertEqual([r["payee"] for r in self.http.get("/payee-rules", params=self.P).json()["rules"]], ["A"])
        self.assertEqual(self.http.delete(f"/payee-rules/{rules['A']['id']}").status_code, 200)
        self.assertEqual(self.http.post("/payee-rules/undo-last-batch", json=self.P).status_code, 404)
        self.assertEqual(self.http.delete("/payee-rules/12345").status_code, 404)

    def test_remember_from_a_run_uses_only_a_persons_lines_and_a_correction_replaces_the_rule(self):
        mine = item(contact="Shell", proposed_code="420", source="manual", status="ok")
        system = item(contact="Auto Supplier", proposed_code="310", source="history", status="review")
        rid = self.seed_run(mine, system)
        r = self.http.post(f"/payee-rules/from-run/{rid}", json={})
        self.assertEqual((r.status_code, r.json()["created"], r.json()["updated"]), (200, 1, 0))
        self.assertEqual([x["payee"] for x in self.http.get("/payee-rules", params=self.P).json()["rules"]], ["Shell"])

        fixed = item(contact="Shell", proposed_code="310", source="manual", status="ok")      # the reviewer corrects it later
        rid2 = self.seed_run(fixed)
        r2 = self.http.post(f"/payee-rules/from-run/{rid2}", json={"item_ids": [fixed["id"]]}).json()
        self.assertEqual((r2["created"], r2["updated"]), (0, 1))
        rule = self.http.get("/payee-rules", params=self.P).json()["rules"][0]
        self.assertEqual(rule["account_code"], "310")

        self.assertEqual(self.http.post(f"/payee-rules/from-run/{self.seed_run(system)}", json={}).status_code, 409)   # nothing a person decided
        self.assertEqual(self.http.post("/payee-rules/from-run/not-a-uuid", json={}).status_code, 404)

    def test_reapply_applies_payee_rules_and_a_later_override_marks_the_rule_stale(self):
        self.http.post("/payee-rules", json={**self.P, "entries": [{"payee_key": "c:bunnings", "payee_display": "Bunnings", "direction": "spend", "account_code": "420"}]})
        a, b = item(contact="Bunnings"), item(contact="Unknown Payee")
        rid = self.seed_run(a, b)
        out = self.http.post(f"/ledger/runs/{rid}/reapply-rules").json()
        got = {i["id"]: i for i in out["items"]}
        self.assertEqual((got[a["id"]]["source"], got[a["id"]]["proposed_code"], got[a["id"]]["status"]), ("payee", "420", "ok"))
        self.assertIsNone(got[b["id"]]["source"])

        self.http.patch(f"/ledger/runs/{rid}/items", json={"changes": {a["id"]: "310"}})              # the person disagrees
        rule = self.http.get("/payee-rules", params=self.P).json()["rules"][0]
        self.assertEqual(rule["overrides"], 1)


if __name__ == "__main__":
    unittest.main()
