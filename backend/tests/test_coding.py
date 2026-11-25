"""History / tax / supplier checks / backtest. Run: python -m unittest discover -s tests -v  (from backend/)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.database.models import LedgerRun, User
from app.services import ledger_coding as lc
from app.services.coding import evaluate, tax
from app.services.coding.decide import assess
from app.services.coding.history import HistoryIndex
from app.services.coding.store import load_history
from app.services.coding.suppliers import abn_checksum_valid, supplier_findings
from app.services.coding.text import jaccard, tokens

SALES, OTHER, COGS = "200", "260", "310"
ACCOUNTS = [{"code": SALES, "name": "Sales - Residential", "class": "REVENUE"},
            {"code": OTHER, "name": "Other Revenue", "class": "REVENUE"},
            {"code": COGS, "name": "Cost of Goods Sold", "class": "EXPENSE"}]
NAMES = {a["code"]: a["name"] for a in ACCOUNTS}

_seq = 0


def mk(current=SALES, desc="BRIDGE SELECT RESIDENTIAL", amount=100.0, contact="", status="unassigned", **over):
    global _seq
    _seq += 1
    it = {"id": f"t{_seq}:0", "tx_id": f"t{_seq}", "line_index": 0, "date": "2026-03-01", "description": desc,
          "contact": contact, "reference": "", "type": "RECEIVE" if amount > 0 else "SPEND",
          "direction": "receive" if amount > 0 else "spend", "amount": amount, "current_code": current,
          "tax_code": None, "proposed_code": None, "source": None, "confidence": None, "reason": "",
          "status": status, "bank_account": "Cheque"}
    it.update(over)
    return it


def many(n, **kw):
    return [mk(**kw) for _ in range(n)]


class TextTests(unittest.TestCase):
    def test_noise_is_stripped_and_variants_of_one_merchant_match(self):
        self.assertEqual(sorted(tokens("CARD 1234 BRIDGE SELECT PTY LTD MELBOURNE AU 12/03 TXN83921 VISA PURCHASE")),
                         ["BRIDGE", "MELBOURNE", "SELECT"])
        self.assertEqual(jaccard(tokens("BRIDGE SELECT MELBOURNE"),
                                 tokens("VISA PURCHASE 5521 BRIDGE SELECT PTY LTD MELBOURNE AU")), 1.0)


class HistoryTests(unittest.TestCase):
    history = many(47) + many(3, current=OTHER)

    def decide(self, target, pool=None):
        idx = HistoryIndex((pool if pool is not None else self.history) + [target])
        return assess(target, idx.evidence_for(target), NAMES)

    def test_94_percent_case_flags_a_miscoded_line_with_high_confidence(self):
        d = self.decide(mk(current=OTHER))
        self.assertEqual((d.outcome, d.code, d.level), ("flag", SALES, "high"))
        self.assertIn("of 50 comparable", d.explanation)
        self.assertTrue(d.example_ids)

    def test_agreement_with_usual_coding(self):
        self.assertEqual(self.decide(mk()).outcome, "agree")

    def test_thin_history_is_no_evidence_not_a_guess(self):
        self.assertEqual(self.decide(mk(current=OTHER), pool=many(2)).outcome, "no_evidence")

    def test_money_in_is_not_compared_with_money_out(self):
        self.assertEqual(self.decide(mk(current=OTHER, amount=-100.0)).outcome, "no_evidence")

    def test_loose_similarity_is_not_high(self):
        loose = many(12, desc="BRIDGE SELECT RESIDENTIAL OTHER STUFF")        # token overlap exactly 0.5
        d = self.decide(mk(current=OTHER, desc="BRIDGE SELECT RESIDENTIAL MISC"), pool=loose)
        self.assertEqual(d.outcome, "flag")
        self.assertNotEqual(d.level, "high")

    def test_history_pointing_at_an_archived_account_is_ignored(self):
        gone = many(30, current="999")
        self.assertEqual(self.decide(mk(current=OTHER), pool=gone).outcome, "no_evidence")

    def test_same_supplier_counts_as_context_even_when_narratives_differ(self):
        pool = [mk(desc=f"INV {i} different words {i}x", contact="Acme Plumbing") for i in range(12)]
        d = self.decide(mk(current=OTHER, desc="totally other narrative", contact="Acme Plumbing"), pool=pool)
        self.assertEqual((d.code, d.level), (SALES, "high"))


class ApplyHistoryTests(unittest.TestCase):
    def run_pipeline(self, items, history=()):
        lc.apply_history(items, list(history), ACCOUNTS)
        lc.refresh(items, ACCOUNTS)
        return {i["id"]: i for i in items}

    def test_agreement_is_trusted_but_a_code_change_always_waits_for_a_person(self):
        agree, wrong = mk(), mk(current=OTHER)
        out = self.run_pipeline([agree, wrong], many(47) + many(3, current=OTHER))
        self.assertEqual((out[agree["id"]]["source"], out[agree["id"]]["status"], out[agree["id"]]["level"]), ("history", "ok", "high"))
        w = out[wrong["id"]]
        self.assertEqual((w["proposed_code"], w["status"]), (SALES, "review"))      # never auto-approved
        self.assertIsNone(w["confidence"])                                           # no invented percentage
        self.assertEqual(w["history"]["n"] >= 40, True)
        self.assertEqual(len(lc.changes_to_apply([w])), 1)

    def test_rules_manual_and_keep_decisions_are_never_overridden(self):
        r = mk(current=OTHER, proposed_code=OTHER, source="rule", confidence=1.0)
        m = mk(current=OTHER, proposed_code=OTHER, source="manual", confidence=1.0)
        k = mk(current=OTHER, proposed_code=OTHER, source="keep", confidence=1.0)
        a = mk(current=OTHER, proposed_code=OTHER, source="ai", confidence=0.9)
        out = self.run_pipeline([r, m, k, a], many(47))
        self.assertEqual([out[x["id"]]["source"] for x in (r, m, k, a)], ["rule", "manual", "keep", "ai"])
        self.assertTrue(all(out[x["id"]]["proposed_code"] == OTHER for x in (r, m, k, a)))

    def test_a_rule_that_contradicts_strong_history_is_annotated_not_changed(self):
        r = mk(current=OTHER, proposed_code=OTHER, source="rule", confidence=1.0)
        out = self.run_pipeline([r], many(47))
        self.assertEqual(out[r["id"]]["proposed_code"], OTHER)
        self.assertIn("history says", out[r["id"]]["history_conflict"])
        report = lc.run_checks(list(out.values()), ACCOUNTS)
        self.assertIn("Rules agree with history", [c["name"] for c in report["checks"]])
        self.assertTrue(report["balanced"] or any(c["level"] == "warn" for c in report["checks"]))   # a warning, not a blocker

    def test_locked_lines_are_untouched_and_stale_history_suggestions_are_cleared(self):
        locked = mk(current=OTHER, status="locked", proposed_code=OTHER, source="locked")
        stale = mk(current=OTHER, source="history", proposed_code=SALES, level="high")
        out = self.run_pipeline([locked, stale], many(1))       # 2 comparable lines: below even the weak threshold (3)
        self.assertEqual(out[locked["id"]]["source"], "locked")
        self.assertIsNone(out[stale["id"]]["source"])
        self.assertEqual(out[stale["id"]]["status"], "unassigned")

    def test_the_ai_step_skips_lines_history_already_answered(self):
        h = mk(current=OTHER)
        lc.apply_history([h], many(47), ACCOUNTS)
        calls = []

        class FakeAI:
            pass

        orig = lc.complete_json
        lc.complete_json = lambda *a, **k: calls.append(a) or {"assignments": []}
        try:
            lc.ai_categorise([h], ACCOUNTS, FakeAI())
        finally:
            lc.complete_json = orig
        self.assertEqual(calls, [])                              # nothing left for the AI to answer
        self.assertEqual(h["source"], "history")


class TaxTests(unittest.TestCase):
    def test_outlier_when_one_code_dominates(self):
        items = many(19, current=COGS, tax_code="INPUT2") + [mk(current=COGS, tax_code="EXEMPTINPUT")]
        f = tax.findings(items, NAMES)
        self.assertEqual(sorted(x["kind"] for x in f), ["multiple_tax_codes", "tax_outlier"])
        self.assertEqual(len(next(x for x in f if x["kind"] == "tax_outlier")["line_ids"]), 1)

    def test_even_split_is_reported_for_review_not_as_an_error(self):
        f = tax.findings(many(10, current=COGS, tax_code="A") + many(10, current=COGS, tax_code="B"), NAMES)
        self.assertEqual([x["kind"] for x in f], ["multiple_tax_codes"])
        self.assertIn("Not necessarily an error", f[0]["explanation"])

    def test_missing_tax_code_where_the_account_has_a_default(self):
        f = tax.findings(many(3, current=COGS, tax_code=None), NAMES, defaults={COGS: "INPUT2"})
        self.assertEqual(next(x for x in f if x["kind"] == "missing_tax_code")["line_ids"].__len__(), 3)

    def test_lines_without_tax_info_are_ignored_not_called_untaxed(self):
        legacy = [{k: v for k, v in mk(current=COGS).items() if k != "tax_code"} for _ in range(5)]
        self.assertEqual(tax.findings(legacy, NAMES, defaults={COGS: "INPUT2"}), [])

    def test_tax_findings_are_a_warning_not_a_blocker(self):
        items = many(10, current=COGS, tax_code="A", amount=-50.0) + many(10, current=COGS, tax_code="B", amount=-50.0)
        for i in items:
            i.update(proposed_code=COGS, source="keep")
        lc.refresh(items, ACCOUNTS)
        report = lc.run_checks(items, ACCOUNTS)
        check = next(c for c in report["checks"] if c["name"] == "Tax codes consistent")
        self.assertEqual((check["ok"], check["level"]), (False, "warn"))
        self.assertTrue(report["balanced"])


class SupplierTests(unittest.TestCase):
    def s(self, id_, name, abn=None):
        return {"id": id_, "name": name, "abn": abn}

    def test_abn_checksum(self):
        self.assertTrue(abn_checksum_valid("51 824 753 556"))
        self.assertFalse(abn_checksum_valid("51 824 753 557"))
        self.assertFalse(abn_checksum_valid("123"))
        self.assertFalse(abn_checksum_valid(None))

    def test_findings(self):
        f = supplier_findings([
            self.s("1", "Bridge Select Pty Ltd", "51 824 753 556"), self.s("2", "Bridge Select", "51824753556"),
            self.s("3", "Acme Plumbing"), self.s("4", "Acme Plumbing Services", "12345678901"),
            self.s("5", "Bunnings Warehouse", "51824753556x"), self.s("6", "Bunnings Warehouse Pty Ltd")])
        kinds = [x["kind"] for x in f]
        for k in ("shared_abn", "missing_abn", "invalid_abn_format", "possible_duplicate"):
            self.assertIn(k, kinds)
        self.assertEqual(len([x for x in f if set(x["ids"]) == {"1", "2"}]), 1)        # reported once, via the shared ABN

    def test_a_ledger_with_no_abns_at_all_gets_one_note_not_a_finding_per_supplier(self):
        f = supplier_findings([self.s(str(i), f"Vendor {i}") for i in range(20)])
        self.assertEqual([x["kind"] for x in f], ["no_abns_recorded"])


class BacktestTests(unittest.TestCase):
    def test_reports_measured_agreement_per_level(self):
        items = many(47) + many(3, current=OTHER) + many(2, desc="UNIQUE ONE OFF THING", current=COGS)
        r = evaluate.backtest(items, NAMES)
        self.assertEqual(r["coded_lines"], 52)
        self.assertEqual(r["with_evidence"], 50)                      # the 2 one-offs have no comparable history
        self.assertEqual(r["by_level"]["high"]["lines"], 50)
        self.assertEqual(r["by_level"]["high"]["agree_with_books_pct"], 94.0)   # 47 of 50 agree
        self.assertEqual(r["top_disagreements"][0]["lines"], 3)
        self.assertEqual((r["top_disagreements"][0]["books"], r["top_disagreements"][0]["history_says"]), (OTHER, SALES))
        self.assertIn("may itself contain errors", r["note"])

    def test_empty_ledger_does_not_divide_by_zero(self):
        r = evaluate.backtest([], NAMES)
        self.assertEqual((r["coded_lines"], r["coverage_pct"], r["needs_review_pct"]), (0, None, None))


class LoadHistoryTests(unittest.TestCase):
    def test_collects_this_clients_coded_lines_newest_first_and_never_crosses_clients(self):
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)

            def run(client, items, org="Acme", provider="quickbooks"):
                r = LedgerRun(user_id=u.id, provider=provider, client_id=client, organization=org,
                              date_from="2026-01-01", date_to="2026-01-31", items=items, accounts=[])
                s.add(r)
                s.commit()
                return r

            old = run("C1", [mk(current="1", id="x:0"), mk(current="2", id="y:0"), mk(current=None, id="z:0")])
            new = run("C1", [mk(current="9", id="x:0")])                          # same line, newer run wins
            run("C2", [mk(current="7", id="other:0")])                            # another client: excluded
            run("C1", [mk(current="8", id="xero:0")], provider="xero")            # another system: excluded
            legacy = run(None, [mk(current="5", id="legacy:0")], org="Acme")      # pre-tracking run, matched by org name
            new.updated_at = old.updated_at.replace(year=2099)
            s.add(new)
            s.commit()

            got = {i["id"]: i["current_code"] for i in load_history(s, u.id, "quickbooks", "C1", "Acme")}
            self.assertEqual(got, {"x:0": "9", "y:0": "2", "legacy:0": "5"})
            self.assertNotIn("x:0", {i["id"] for i in load_history(s, u.id, "quickbooks", "C1", "Acme", exclude_run_id=new.id)
                                     if i["current_code"] == "9"})


if __name__ == "__main__":
    unittest.main()
