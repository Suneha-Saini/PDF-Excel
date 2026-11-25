"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, RefreshCw, CheckCircle2, AlertCircle, AlertTriangle, Download, Play, Trash2, Lock } from "lucide-react";
import HistoryTab from "./HistoryTab";
import AccuracyTab from "./AccuracyTab";
import SetupTab from "./SetupTab";
import PayeeRulesTab from "./PayeeRulesTab";
import WorkflowTab from "./WorkflowTab";
import SyncStatusPanel from "./SyncStatusPanel";
import { api, authHeaders, getApiBase } from "../../lib/api";

interface Account { code: string; name: string; type?: string; class?: string }
interface Item {
  id: string; tx_id: string; date: string; description: string; contact: string; type: string; direction: string;
  amount: number; current_code: string | null; proposed_code: string | null; source: string | null;
  confidence: number | null; reason: string; status: "unassigned" | "ok" | "review" | "locked"; bank_account?: string;
  tax_code?: string | null; level?: "high" | "medium" | "low" | null; history_conflict?: string; auto_approved?: boolean;
  approved?: boolean; payee_rule_id?: number | null;
}
// Evidence behind the suggestions (see /ledger/runs/{id}/insights and /suppliers)
interface Finding { kind: string; account?: string; line_ids?: string[]; ids?: string[]; explanation: string }
interface Insights {
  history_lines: number; tax_findings: Finding[]; conflicts: { id: string; date: string; description: string; reason: string }[];
  evaluation: {
    coded_lines: number; with_evidence: number; coverage_pct: number | null; needs_review_pct: number | null; note: string;
    by_level: Record<string, { lines: number; agree_with_books_pct: number | null }>;
    top_disagreements: { books: string; history_says: string; lines: number; books_name: string; history_name: string }[];
  };
}
interface SupplierReport { suppliers: number; findings: Finding[]; note: string }
interface Check { name: string; ok: boolean; level: "ok" | "warn" | "error"; detail: string }
interface Report {
  balanced: boolean; checks: Check[];
  totals: { lines: number; received: number; spent: number; net: number };
  categories: { code: string; name: string; count: number; received: number; spent: number; net: number }[];
}
interface Run {
  id: string; organization: string | null; date_from: string; date_to: string; status: string; progress: string | null;
  provider?: string; provider_label?: string; client_id?: string | null; auto_sync?: boolean;
  error: string | null; accounts?: Account[]; items?: Item[]; report?: Report; pending_changes?: number;
  apply_results?: { lines_changed: number; transactions_updated: number; transactions_failed: number; errors: string[] } | null;
}
interface Rule { id: number; name: string; keywords: string; direction: string; account_code: string; priority: number; active: boolean; provider?: string; client_id?: string | null }
interface Provider { provider_name: string; has_key: boolean }
// An accounting system (Xero, QuickBooks, Manager.io) and one of its clients (a company inside it).
interface System { key: string; label: string; status: string; ready: boolean; verified: boolean; supports_apply: boolean; note: string }
interface Client { id: string; name: string }
// A client kept in sync automatically (webhooks + nightly), see /api/sync.
interface SyncTarget {
  id: string; provider: string; client_id: string; enabled: boolean; lookback_days: number;
  last_synced_at: string | null; last_trigger: string | null; last_status: string | null; last_error: string | null;
  last_summary: { lines: number; new_lines: number; rule_matched: number; unassigned: number; pending_changes: number } | null;
  running: boolean; run_id: string | null;
}
interface SyncStatus { worker_running: boolean; nightly_hour: number; webhooks: Record<string, { configured: boolean; path: string }> }
const ago = (iso: string | null) => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

const money = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const iso = (d: Date) => d.toISOString().slice(0, 10);
const PAGE = 50;
const RUNNING = ["fetching", "categorising", "applying"];

const SOURCE: Record<string, string> = {
  rule: "bg-blue-50 text-blue-700 border-blue-200", ai: "bg-purple-50 text-purple-700 border-purple-200",
  manual: "bg-emerald-50 text-emerald-700 border-emerald-200", keep: "bg-slate-100 text-slate-600 border-slate-200",
  history: "bg-teal-50 text-teal-700 border-teal-200", payee: "bg-indigo-50 text-indigo-700 border-indigo-200",
  locked: "bg-slate-100 text-slate-400 border-slate-200",
};

export default function LedgerPage() {
  const [tab, setTab] = useState<"review" | "workflow" | "rules" | "payee" | "history" | "setup" | "accuracy">("review");
  const [notice, setNotice] = useState<string | null>(null);
  const [systems, setSystems] = useState<System[]>([]);
  const [system, setSystem] = useState("");            // chosen accounting system key; "" until the user/loader picks one
  const [clients, setClients] = useState<Client[]>([]);
  const [clientId, setClientId] = useState("");
  const [clientsError, setClientsError] = useState<string | null>(null);
  const [loadingClients, setLoadingClients] = useState(false);
  const [diagnosis, setDiagnosis] = useState<any[] | null>(null);
  const [target, setTarget] = useState<SyncTarget | null>(null);      // auto-sync settings of the selected client
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("");
  const [from, setFrom] = useState(() => `${new Date().getFullYear()}-01-01`);
  const [to, setTo] = useState(() => iso(new Date()));
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "history" | "ai" | "review" | "unassigned" | "changed" | "locked">("all");
  const [insights, setInsights] = useState<Insights | null>(null);
  const [supplierReport, setSupplierReport] = useState<SupplierReport | null>(null);
  const [insightBusy, setInsightBusy] = useState<"" | "analyse" | "suppliers">("");
  const [page, setPage] = useState(0);
  const [confirmApply, setConfirmApply] = useState(false);
  const [showResult, setShowResult] = useState(false);
  const [rules, setRules] = useState<Rule[]>([]);
  const [chart, setChart] = useState<Account[]>([]);
  const prevStatus = useRef<string | null>(null);

  // ---------------------------------------------------------------- load
  useEffect(() => {
    // 1) which systems exist / are connected  2) the most recent run decides what is on screen first
    Promise.all([
      api<System[]>("/ledger/providers"),
      api<Run[]>("/ledger/runs").catch(() => [] as Run[]),
    ]).then(([ss, rs]) => {
      setSystems(ss);
      if (rs.length) { setSystem(rs[0].provider || "xero"); loadRun(rs[0].id); }
      else setSystem((ss.find((s) => s.ready) || ss[0])?.key || "xero");
    }).catch(() => setError("Cannot reach the backend. Start it with `python run.py` and reload."));
    api<Provider[]>("/settings/providers").then((ps) => {
      setProviders(ps);
      const usable = ps.find((p) => p.has_key) || ps.find((p) => p.provider_name === "Ollama" || p.provider_name === "LMStudio");
      if (usable) setProvider(usable.provider_name);
    }).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const selected = systems.find((s) => s.key === system);
  const clientName = clients.find((c) => c.id === clientId)?.name || "";
  const scope = `provider=${encodeURIComponent(system)}${clientId ? `&client_id=${encodeURIComponent(clientId)}` : ""}`;

  // when the accounting system changes, fetch ONLY that system's clients
  useEffect(() => {
    if (!system) return;
    setClients([]); setClientsError(null); setDiagnosis(null);
    if (!selected?.ready) return;
    let stale = false;
    setLoadingClients(true);
    api<{ clients: Client[]; default: string | null }>(`/ledger/providers/${encodeURIComponent(system)}/clients`)
      .then((r) => {
        if (stale) return;
        setClients(r.clients);
        // keep the run's client when that run is on screen, otherwise the connection's default
        setClientId((prev) => r.clients.some((c) => c.id === prev) ? prev : (r.clients.find((c) => c.id === r.default) || r.clients[0])?.id || "");
      })
      .catch((e) => { if (!stale) setClientsError(e.message); })
      .finally(() => { if (!stale) setLoadingClients(false); });
    return () => { stale = true; };
  }, [system, selected?.ready]); // eslint-disable-line react-hooks/exhaustive-deps

  // rules belong to a system (and optionally one client), so reload them when either changes
  const loadRules = useCallback(() => {
    if (!system) return;
    api<Rule[]>(`/ledger/rules?${scope}`).then(setRules).catch(() => {});
  }, [system, scope]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadRules(); }, [loadRules]);

  const loadRun = useCallback(async (id: string) => {
    try { setRun(await api<Run>(`/ledger/runs/${id}`)); } catch (e: any) { setError(e.message); }
  }, []);

  // Selecting a client shows ITS ledger: the most recently updated run for that system + client (which is the
  // auto-synced one when auto-sync is on). Skipped while something is running so we never yank the screen away.
  const runRef = useRef<Run | null>(null);
  useEffect(() => { runRef.current = run; }, [run]);
  useEffect(() => { setInsights(null); setSupplierReport(null); }, [run?.id]);   // never show another run's evidence
  useEffect(() => {
    if (!system || !clientId || !selected?.ready) return;
    let stale = false;
    api<Run[]>(`/ledger/runs?${scope}`).then((rs) => {
      if (stale) return;
      const cur = runRef.current;
      if (cur && RUNNING.includes(cur.status)) return;
      if (rs.length) { if (rs[0].id !== cur?.id) loadRun(rs[0].id); }
      else if (cur && (cur.provider !== system || (cur.client_id && cur.client_id !== clientId))) setRun(null);
    }).catch(() => {});
    return () => { stale = true; };
  }, [system, clientId, selected?.ready]); // eslint-disable-line react-hooks/exhaustive-deps

  // auto-sync settings for the selected client; refreshed every 20 s so a webhook/nightly sync shows up by itself
  const loadTarget = useCallback(async () => {
    if (!system || !clientId || !selected?.ready || system.startsWith("manager")) { setTarget(null); return; }
    try {
      const ts = await api<SyncTarget[]>(`/sync/targets?${scope}`);
      setTarget(ts[0] || null);
    } catch { /* leave as is */ }
  }, [system, clientId, selected?.ready, scope]);
  useEffect(() => { loadTarget(); }, [loadTarget]);
  useEffect(() => { api<SyncStatus>("/sync/status").then(setSyncStatus).catch(() => {}); }, []);
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) loadTarget(); }, 20000);
    return () => clearInterval(t);
  }, [loadTarget]);
  // when a sync finishes for the run on screen, pull in the new lines (unless the user is mid-action)
  const syncedAt = target?.last_synced_at;
  useEffect(() => {
    const cur = runRef.current;
    if (!target?.run_id || busy) return;
    const noUsefulRunOnScreen = !cur || cur.status === "failed";           // never replace a run the user is working on
    const showingTheSyncedRun = cur?.id === target.run_id && !RUNNING.includes(cur.status);
    if (noUsefulRunOnScreen || showingTheSyncedRun) loadRun(target.run_id);
  }, [syncedAt, target?.run_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveTarget = async (patch: { enabled?: boolean; lookback_days?: number }) => {
    if (!system || !clientId) return;
    setSyncBusy(true); setError(null);
    try {
      setTarget(await api<SyncTarget>("/sync/targets", { method: "PUT", body: JSON.stringify({
        provider: system, client_id: clientId, enabled: patch.enabled ?? target?.enabled ?? true,
        lookback_days: patch.lookback_days ?? target?.lookback_days ?? 120, ai_provider: null,
      }) }));
    } catch (e: any) { setError(e.message); } finally { setSyncBusy(false); }
  };
  const syncNow = async () => {
    if (!target) return;
    setSyncBusy(true); setError(null);
    try {
      const res = await api<{ outcome?: any; target?: SyncTarget }>(`/sync/targets/${target.id}/sync`, { method: "POST" });
      if (res?.outcome && res.outcome.status !== "ok" && res.outcome.status !== "skipped") {
        setError(res.outcome.error || `Sync failed: ${res.outcome.status}`);
      }
      if (res?.target) {
        setTarget(res.target);
        if (res.target.run_id) {
          await loadRun(res.target.run_id);
        }
      } else {
        await loadTarget();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSyncBusy(false);
    }
  };

  // keep the selectors and date boxes in step with the run that is on screen
  const shownRunId = run?.id;
  useEffect(() => {
    if (run) {
      setFrom(run.date_from); setTo(run.date_to);
      if (run.provider) setSystem(run.provider);
      if (run.client_id) setClientId(run.client_id);
    }
  }, [shownRunId]); // eslint-disable-line react-hooks/exhaustive-deps

  // poll while a background job is running
  useEffect(() => {
    if (!run || !RUNNING.includes(run.status)) return;
    const t = setInterval(() => loadRun(run.id), 1500);
    return () => clearInterval(t);
  }, [run?.id, run?.status, loadRun]); // eslint-disable-line react-hooks/exhaustive-deps

  // open the result dialog when an apply finishes
  useEffect(() => {
    if (prevStatus.current === "applying" && run && run.status !== "applying") setShowResult(true);
    prevStatus.current = run?.status ?? null;
  }, [run?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  // The chart of the run on screen, unless that run belongs to another system/client than the one selected for rules.
  const runMatchesSelection = !!run && run.provider === system && (!clientId || !run.client_id || run.client_id === clientId);
  const accounts = useMemo(() => (runMatchesSelection && run?.accounts?.length ? run.accounts : chart), [runMatchesSelection, run?.accounts, chart]);
  useEffect(() => { setChart([]); }, [system, clientId]);
  useEffect(() => {
    if ((tab === "rules" || tab === "history") && !accounts.length && selected?.ready && clientId) {
      api<Account[]>(`/ledger/accounts?${scope}`).then(setChart).catch((e) => setError(e.message));
    }
  }, [tab, accounts.length, selected?.ready, clientId, scope]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- actions
  const start = async () => {
    setError(null); setBusy("start"); setPage(0);
    try {
      const { id } = await api<{ id: string }>("/ledger/runs", {
        method: "POST", body: JSON.stringify({ date_from: from, date_to: to, ai_provider: provider || null, provider: system, client_id: clientId || null }),
      });
      await loadRun(id);
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const act = async (path: string, label: string, body?: any, method = "POST") => {
    if (!run) return;
    setBusy(label); setError(null);
    try {
      setRun(await api<Run>(`/ledger/runs/${run.id}${path}`, { method, body: body ? JSON.stringify(body) : undefined }));
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const setCode = (item: Item, code: string) => act("/items", "edit", { changes: { [item.id]: code || null } }, "PATCH");

  // Payee rules: turn lines a PERSON coded or approved into "this payee -> this account" rules (never the system's own guesses)
  const remember = async (ids?: string[]) => {
    if (!run) return;
    setBusy("remember"); setError(null); setNotice(null);
    try {
      const r = await api<{ created: number; updated: number }>(`/payee-rules/from-run/${run.id}`, { method: "POST", body: JSON.stringify(ids ? { item_ids: ids } : {}) });
      setNotice(`Remembered ${r.created} new and updated ${r.updated} payee rule(s). Review or undo them on the Payee rules tab.`);
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const download = async () => {
    if (!run) return;
    const res = await fetch(`${getApiBase()}/ledger/runs/${run.id}/export`, { headers: authHeaders() });
    if (!res.ok) { setError("Export failed."); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = `general-ledger_${run.date_from}_${run.date_to}.xlsx`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const doApply = async () => {
    setConfirmApply(false);
    await act("/apply", "apply");
  };

  // ---------------------------------------------------------------- derived
  const items = run?.items || [];
  const filtered = useMemo(() => items.filter((i) => {
    if (filter === "history") return i.source === "history";
    if (filter === "ai") return i.source === "ai";
    if (filter === "review") return i.status === "review";
    if (filter === "unassigned") return i.status === "unassigned";
    if (filter === "locked") return i.status === "locked";
    if (filter === "changed") return i.status !== "locked" && i.proposed_code && i.proposed_code !== i.current_code;
    return true;
  }), [items, filter]);
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE));
  const shown = filtered.slice(page * PAGE, page * PAGE + PAGE);
  const report = run?.report;
  const ready = run?.status === "ready" || run?.status === "applied";
  const nameOf = (code: string | null) => accounts.find((a) => a.code === code)?.name || "";
  const aiFailure = (items.find((i) => i.status === "unassigned" && i.reason.startsWith("AI unavailable"))?.reason || "")
    .replace(/^AI unavailable:\s*/, "");
  const aiCount = items.filter((i) => i.source === "ai").length;
  const counts = {
    review: items.filter((i) => i.status === "review").length,
    unassigned: items.filter((i) => i.status === "unassigned").length,
    locked: items.filter((i) => i.status === "locked").length,
  };

  // ---------------------------------------------------------------- UI
  return (
    <main className="min-h-screen bg-slate-50 text-slate-800">
      <header className="h-14 bg-white border-b border-slate-200 px-6 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/" className="flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-blue-600">
            <ArrowLeft className="h-3.5 w-3.5" /> Statement Converter
          </Link>
          <div>
            <div className="text-sm font-bold text-slate-900">Ledger Coding</div>
            <div className="text-[10px] text-slate-400 uppercase tracking-wider">{selected?.label || "Accounting"} general ledger · AI categorisation</div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[11px] font-semibold">
          {selected?.ready
            ? <span className="px-2.5 py-1 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700">{selected.label}{clientName ? `: ${clientName}` : ""}</span>
            : selected
              ? <Link href="/" className="px-2.5 py-1 rounded-full bg-amber-50 border border-amber-200 text-amber-700">{selected.label} not connected — open Settings › Deployment</Link>
              : null}
        </div>
      </header>

      <div className="max-w-7xl mx-auto p-6 space-y-5">
        <div className="flex gap-6 border-b border-slate-200 text-xs font-bold">
          {(["review", "workflow", "rules", "payee", "history", "setup", "accuracy"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`pb-2 border-b-2 cursor-pointer ${tab === t ? "border-blue-600 text-blue-600" : "border-transparent text-slate-400 hover:text-slate-600"}`}>
              {t === "review" ? "Review & Apply" : t === "workflow" ? "Workflow" : t === "rules" ? `Rules (${rules.length})` : t === "payee" ? "Payee rules" : t === "history" ? "History" : t === "setup" ? "Setup" : "Accuracy"}
            </button>
          ))}
        </div>

        {notice && !error && (
          <div className="flex items-start gap-2 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl p-3">
            <CheckCircle2 className="h-4 w-4 shrink-0" /> <span className="break-words min-w-0">{notice}</span>
          </div>
        )}
        {error && (
          <div className="flex items-start gap-2 text-xs font-semibold text-red-700 bg-red-50 border border-red-200 rounded-xl p-3">
            <AlertCircle className="h-4 w-4 shrink-0" /> <span className="break-words min-w-0">{error}</span>
          </div>
        )}

        {tab === "rules" ? (
          <RulesTab rules={rules} accounts={accounts} reload={loadRules} setError={setError}
            provider={system} systemLabel={selected?.label || ""} clientId={clientId} clientName={clientName} connected={!!selected?.ready} />
        ) : tab === "workflow" ? (
          <WorkflowTab setError={setError} provider={system} systemLabel={selected?.label || ""} clientId={clientId} clientName={clientName} connected={!!selected?.ready}
            goToSetup={() => setTab("setup")} goToReview={() => setTab("review")} />
        ) : tab === "payee" ? (
          <PayeeRulesTab setError={setError} provider={system} systemLabel={selected?.label || ""} clientId={clientId} clientName={clientName} connected={!!selected?.ready} />
        ) : tab === "setup" ? (
          <SetupTab setError={setError} provider={system} systemLabel={selected?.label || ""} clientId={clientId} clientName={clientName} connected={!!selected?.ready} />
        ) : tab === "accuracy" ? (
          <AccuracyTab setError={setError} provider={system} systemLabel={selected?.label || ""} clientId={clientId} clientName={clientName} connected={!!selected?.ready} />
        ) : tab === "history" ? (
          <HistoryTab accounts={accounts} setError={setError} provider={system} systemLabel={selected?.label || ""}
            clientId={clientId} clientName={clientName} connected={!!selected?.ready} />
        ) : (
          <>
            {/* Step 1: choose the accounting system, then the client, then period + AI */}
            <section className="bg-white border border-slate-200 rounded-2xl p-5 flex flex-wrap items-end gap-4">
              <Field label="Accounting system">
                <select value={system} onChange={(e) => { setSystem(e.target.value); setClientId(""); }} className="input min-w-44"
                  disabled={!!busy || (run ? RUNNING.includes(run.status) : false)}>
                  {systems.map((s) => (
                    <option key={s.key} value={s.key}>{s.label}{s.ready ? "" : " (not connected)"}{s.ready && !s.verified ? " · beta" : ""}</option>
                  ))}
                </select>
              </Field>
              <Field label="Client">
                <select value={clientId} onChange={(e) => setClientId(e.target.value)} className="input min-w-52"
                  disabled={!selected?.ready || loadingClients || clients.length === 0}>
                  {loadingClients && <option value="">Loading clients…</option>}
                  {!loadingClients && clients.length === 0 && <option value="">{selected?.ready ? "No clients found" : "Connect first"}</option>}
                  {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </Field>
              <Field label="From"><input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="input" /></Field>
              <Field label="To"><input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="input" /></Field>
              <Field label="AI Coordinator">
                <select value={provider} onChange={(e) => setProvider(e.target.value)} className="input">
                  <option value="">Rules only (no AI)</option>
                  {providers.map((p) => <option key={p.provider_name} value={p.provider_name}>{p.provider_name}{p.has_key ? "" : p.provider_name === "Ollama" || p.provider_name === "LMStudio" ? " (local)" : " (no key)"}</option>)}
                </select>
              </Field>
              <button onClick={start} disabled={!!busy || (run ? RUNNING.includes(run.status) : false) || !selected?.ready || !clientId}
                className="flex items-center gap-1.5 px-5 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-xs font-bold cursor-pointer">
                <Play className="h-3.5 w-3.5" /> Fetch ledger &amp; categorise
              </button>
              <p className="text-[10px] text-slate-400 w-full">
                Downloads every bank transaction in the period from <b>{selected?.label || "your accounting system"}</b>{clientName ? <> for <b>{clientName}</b></> : null}, applies your rules, then asks the AI about the rest. Nothing is changed in {selected?.label || "the accounting system"} until you press Apply.
              </p>
              {clientsError && <p className="text-[11px] font-semibold text-red-600 w-full break-words">Could not load clients: {clientsError}</p>}

              {/* Auto-sync: keep this client's ledger fresh and apply rules as changes arrive */}
              {selected?.ready && clientId && !system.startsWith("manager") && (
                <div className="w-full border-t border-slate-100 pt-3 flex flex-wrap items-center gap-3 text-[11px]">
                  <button onClick={() => saveTarget({ enabled: !target?.enabled })} disabled={syncBusy}
                    role="switch" aria-checked={!!target?.enabled} title="Keep this client's ledger up to date automatically"
                    className={`relative w-9 h-5 rounded-full transition-colors cursor-pointer disabled:opacity-50 ${target?.enabled ? "bg-emerald-500" : "bg-slate-300"}`}>
                    <span className={`absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${target?.enabled ? "translate-x-4" : ""}`} />
                  </button>
                  <span className="font-bold text-slate-700">Auto-sync {target?.enabled ? "on" : "off"}</span>
                  {target?.enabled ? (
                    <>
                      <span className="text-slate-500">
                        {target.running || syncBusy ? "syncing now…" : `last synced ${ago(target.last_synced_at)}${target.last_trigger ? ` (${target.last_trigger})` : ""}`}
                        {target.last_summary && !target.running && !syncBusy && ` · ${target.last_summary.lines} lines, ${target.last_summary.new_lines} new, ${target.last_summary.rule_matched} matched by rules`}
                      </span>
                      <select value={target.lookback_days} onChange={(e) => saveTarget({ lookback_days: Number(e.target.value) })} disabled={syncBusy} className="input" title="How far back to keep in sync">
                        {[30, 60, 90, 120, 180, 365].map((d) => <option key={d} value={d}>last {d} days</option>)}
                      </select>
                      <button onClick={syncNow} disabled={syncBusy || target.running} className="font-bold text-blue-600 cursor-pointer disabled:opacity-40 inline-flex items-center gap-1">
                        {syncBusy && <RefreshCw className="h-3 w-3 animate-spin" />}
                        {syncBusy ? "Syncing…" : "Sync now"}
                      </button>
                    </>
                  ) : (
                    <span className="text-slate-400">Fetches this client&apos;s ledger every night and when {selected.label} reports a change, applying your rules. Nothing is written back to {selected.label}.</span>
                  )}
                  {target?.enabled && target.last_status && target.last_status !== "ok" && (
                    <span className={`w-full font-semibold ${target.last_status === "deferred" ? "text-slate-500" : "text-amber-700"}`}>
                      {target.last_status === "needs_reconnect" ? `Sync paused — reconnect ${selected.label} in Settings › Deployment. ` : target.last_status === "rate_limited" ? "Rate limited by the platform; will retry automatically. " : target.last_status === "deferred" ? "A sync was postponed because the run was busy; it will retry. " : "Last sync failed. "}
                      {target.last_error}
                    </span>
                  )}
                  {target?.enabled && syncStatus && (
                    <span className="w-full text-slate-400">
                      {syncStatus.webhooks[system] === undefined ? `${selected.label} has no webhooks: nightly sync only.`
                        : syncStatus.webhooks[system].configured ? "Webhook receiver is configured."
                        : "Webhooks are not configured yet (nightly sync only) — see backend/.env: XERO_WEBHOOK_KEY / QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN."}
                      {" "}{!syncStatus.worker_running && "Background worker is not running."}
                    </span>
                  )}
                  <SyncStatusPanel provider={system} clientId={clientId} label={selected.label} />
                </div>
              )}
              {selected && selected.ready && !selected.verified && selected.note && (
                <p className="text-[11px] font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 w-full">{selected.note}</p>
              )}
              {selected && !selected.ready && (
                <p className="text-[11px] font-semibold text-slate-500 w-full">
                  {selected.label} is not connected yet. <Link href="/" className="text-blue-600 underline">Open Settings › Deployment</Link> to connect it, then come back.
                </p>
              )}
              {selected?.key.startsWith("manager") && selected.ready && (
                <div className="w-full">
                  <button onClick={() => api<{ routes: any[] }>(`/ledger/providers/${system}/diagnose`).then((r) => setDiagnosis(r.routes)).catch((e) => setError(e.message))}
                    className="text-[11px] font-bold text-blue-600 cursor-pointer">Diagnose Manager.io API</button>
                  {diagnosis && (
                    <pre className="mt-2 max-h-48 overflow-auto text-[10px] bg-slate-50 border border-slate-200 rounded-lg p-2 whitespace-pre-wrap break-words">{JSON.stringify(diagnosis, null, 2)}</pre>
                  )}
                </div>
              )}
            </section>

            {/* progress */}
            {run && RUNNING.includes(run.status) && (
              <div className="bg-white border border-slate-200 rounded-2xl p-5 flex items-center gap-3 text-xs font-semibold text-slate-600">
                <RefreshCw className="h-4 w-4 animate-spin text-blue-600" /> {run.progress || "Working..."}
              </div>
            )}
            {run?.status === "failed" && (
              <div className="bg-red-50 border border-red-200 rounded-2xl p-4 text-xs text-red-700 font-semibold">Run failed: {run.error}</div>
            )}

            {run && ready && report && (
              <>
                {/* which period is on screen */}
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
                  <span className="font-bold text-slate-800">{run.organization}</span>
                  {run.auto_sync && <span className="px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700 text-[10px] font-bold">Auto-synced</span>}
                  <span>·</span><span>Showing <b>{run.date_from}</b> to <b>{run.date_to}</b></span>
                  <span>·</span><span>{report.totals.lines} bank-transaction lines found in {run.provider_label || "Xero"}</span>
                  {(from !== run.date_from || to !== run.date_to || system !== run.provider || (!!clientId && !!run.client_id && clientId !== run.client_id)) && (
                    <span className="text-amber-600 font-semibold">— your selection above differs from this view; press “Fetch ledger” to load it</span>
                  )}
                </div>

                {/* AI could not answer */}
                {aiFailure && (
                  <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 flex flex-wrap items-center gap-3">
                    <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0" />
                    <div className="text-xs text-amber-800 min-w-0 flex-1">
                      <b>The AI could not categorise {counts.unassigned} line(s).</b> <span className="break-words">{aiFailure}</span>
                    </div>
                    <select value={provider} onChange={(e) => setProvider(e.target.value)} className="input">
                      {providers.map((p) => <option key={p.provider_name} value={p.provider_name}>{p.provider_name}</option>)}
                    </select>
                    <Btn onClick={() => act("/retry-ai", "retry", { ai_provider: provider })} busy={busy === "retry"} disabled={!provider}>Retry AI</Btn>
                  </div>
                )}

                {/* no AI categories yet (run made without AI, or lines were set to "keep current") */}
                {!aiFailure && aiCount === 0 && (
                  <div className="bg-blue-50 border border-blue-200 rounded-2xl p-4 flex flex-wrap items-center gap-3">
                    <AlertTriangle className="h-4 w-4 text-blue-600 shrink-0" />
                    <div className="text-xs text-blue-800 flex-1 min-w-0">
                      <b>No AI categories on this run yet.</b> {counts.unassigned > 0 ? `${counts.unassigned} line(s) have no category.` : "Lines are still on their current Xero codes."} Run the AI to choose categories automatically — your rules and manual edits are kept.
                    </div>
                    <select value={provider} onChange={(e) => setProvider(e.target.value)} className="input">
                      {providers.map((p) => <option key={p.provider_name} value={p.provider_name}>{p.provider_name}</option>)}
                    </select>
                    <Btn onClick={() => act("/retry-ai", "retry", { ai_provider: provider, all_lines: true })} busy={busy === "retry"} disabled={!provider}>Run AI on all lines</Btn>
                  </div>
                )}

                {/* totals */}
                <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <Stat label="Ledger lines" value={String(report.totals.lines)} />
                  <Stat label="Received" value={money(report.totals.received)} tone="green" />
                  <Stat label="Spent" value={money(report.totals.spent)} tone="red" />
                  <Stat label="Net" value={money(report.totals.net)} />
                  <Stat label="Lines to recode" value={String(run.pending_changes ?? 0)} tone="blue" />
                </section>

                {/* checks */}
                <section className="bg-white border border-slate-200 rounded-2xl p-5">
                  <div className="flex items-center justify-between mb-3">
                    <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">Integrity checks</h2>
                    <span className={`text-[11px] font-bold px-2.5 py-1 rounded-full border ${report.balanced ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-red-50 text-red-700 border-red-200"}`}>
                      {report.balanced ? "Balanced — ready to apply" : "Not balanced yet"}
                    </span>
                  </div>
                  <ul className="space-y-1.5">
                    {report.checks.map((c) => (
                      <li key={c.name} className="flex items-start gap-2 text-xs">
                        {c.ok ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 mt-px shrink-0" />
                          : c.level === "warn" ? <AlertTriangle className="h-3.5 w-3.5 text-amber-500 mt-px shrink-0" />
                          : <AlertCircle className="h-3.5 w-3.5 text-red-500 mt-px shrink-0" />}
                        <span><b className="text-slate-700">{c.name}</b> <span className="text-slate-500">— {c.detail}</span></span>
                      </li>
                    ))}
                  </ul>
                  <details className="mt-4">
                    <summary className="text-[11px] font-bold text-blue-600 cursor-pointer">Totals by category ({report.categories.length})</summary>
                    <table className="w-full text-xs mt-2">
                      <thead><tr className="text-left text-[10px] uppercase text-slate-400"><th className="py-1">Code</th><th>Category</th><th className="text-right">Lines</th><th className="text-right">Received</th><th className="text-right">Spent</th><th className="text-right">Net</th></tr></thead>
                      <tbody>
                        {report.categories.map((c) => (
                          <tr key={c.code} className="border-t border-slate-100">
                            <td className="py-1 font-mono">{c.code}</td><td>{c.name}</td><td className="text-right">{c.count}</td>
                            <td className="text-right text-emerald-600">{money(c.received)}</td><td className="text-right text-red-600">{money(c.spent)}</td><td className="text-right font-semibold">{money(c.net)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                </section>

                {/* insights: the evidence behind history suggestions, tax findings, supplier/ABN checks */}
                <section className="bg-white border border-slate-200 rounded-2xl p-5">
                  <div className="flex flex-wrap items-center gap-3">
                    <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">Insights</h2>
                    <Btn busy={insightBusy === "analyse"} onClick={async () => {
                      setInsightBusy("analyse"); setError(null);
                      try { setInsights(await api<Insights>(`/ledger/runs/${run.id}/insights`)); } catch (e: any) { setError(e.message); } finally { setInsightBusy(""); }
                    }}>Analyse history &amp; tax</Btn>
                    {!run.provider?.startsWith("manager") && (
                      <Btn busy={insightBusy === "suppliers"} onClick={async () => {
                        setInsightBusy("suppliers"); setError(null);
                        try { setSupplierReport(await api<SupplierReport>(`/ledger/runs/${run.id}/suppliers`)); } catch (e: any) { setError(e.message); } finally { setInsightBusy(""); }
                      }}>Check suppliers (ABN)</Btn>
                    )}
                    <span className="text-[10px] text-slate-400">Nothing here changes {run.provider_label || "your books"}.</span>
                  </div>

                  {insights && (
                    <div className="mt-4 space-y-4 text-xs">
                      <div>
                        <div className="font-bold text-slate-700 mb-1">How well does this client&apos;s history predict its coding?</div>
                        <p className="text-slate-500">
                          Using {insights.history_lines} coded lines: history had comparable evidence for <b>{insights.evaluation.with_evidence}</b> of {insights.evaluation.coded_lines} ({insights.evaluation.coverage_pct ?? 0}%).
                          Lines a person would still review: <b>{insights.evaluation.needs_review_pct ?? "—"}%</b>.
                        </p>
                        <table className="mt-2 text-xs">
                          <thead><tr className="text-left text-[10px] uppercase text-slate-400"><th className="pr-6">Confidence</th><th className="pr-6 text-right">Lines</th><th className="text-right">Agrees with books</th></tr></thead>
                          <tbody>
                            {Object.entries(insights.evaluation.by_level).map(([lv, v]) => (
                              <tr key={lv} className="border-t border-slate-100"><td className="pr-6 capitalize">{lv}</td><td className="pr-6 text-right">{v.lines}</td><td className="text-right font-semibold">{v.agree_with_books_pct == null ? "—" : `${v.agree_with_books_pct}%`}</td></tr>
                            ))}
                          </tbody>
                        </table>
                        {insights.evaluation.with_evidence < 100 && (
                          <p className="mt-2 text-amber-700 font-semibold">Only {insights.evaluation.coded_lines} coded lines are available, which is little to learn from. Import a ledger export on the History tab, or turn on Auto-sync with a longer window (up to 365 days), to build more history.</p>
                        )}
                        {insights.evaluation.top_disagreements.length > 0 && (
                          <p className="mt-2 text-slate-500">Most common disagreements (books → history): {insights.evaluation.top_disagreements.map((d) => `${d.books} ${d.books_name} → ${d.history_says} ${d.history_name} (${d.lines})`).join("; ")}</p>
                        )}
                        <p className="mt-1 text-[10px] text-slate-400">{insights.evaluation.note}</p>
                      </div>

                      <div>
                        <div className="font-bold text-slate-700 mb-1">Tax codes ({insights.tax_findings.length})</div>
                        {insights.tax_findings.length === 0 ? <p className="text-slate-400">No inconsistencies found (or tax codes were not captured for these lines).</p> : (
                          <ul className="space-y-1 text-slate-600 list-disc pl-4">{insights.tax_findings.slice(0, 8).map((f, k) => <li key={k}>{f.explanation}</li>)}
                            {insights.tax_findings.length > 8 && <li className="list-none text-slate-400">…and {insights.tax_findings.length - 8} more</li>}</ul>
                        )}
                      </div>

                      {insights.conflicts.length > 0 && (
                        <div>
                          <div className="font-bold text-slate-700 mb-1">Rules that disagree with strong history ({insights.conflicts.length})</div>
                          <ul className="space-y-1 text-slate-600 list-disc pl-4">{insights.conflicts.slice(0, 8).map((c) => <li key={c.id}>{c.date} · {c.description}: {c.reason}</li>)}</ul>
                        </div>
                      )}
                    </div>
                  )}

                  {supplierReport && (
                    <div className="mt-4 text-xs">
                      <div className="font-bold text-slate-700 mb-1">Suppliers: {supplierReport.suppliers} checked, {supplierReport.findings.length} finding(s)</div>
                      {supplierReport.findings.length === 0 ? <p className="text-slate-400">No supplier issues found.</p> : (
                        <ul className="space-y-1 text-slate-600 list-disc pl-4">{supplierReport.findings.slice(0, 12).map((f, k) => <li key={k}>{f.explanation}</li>)}
                          {supplierReport.findings.length > 12 && <li className="list-none text-slate-400">…and {supplierReport.findings.length - 12} more</li>}</ul>
                      )}
                      <p className="mt-1 text-[10px] text-slate-400">{supplierReport.note}</p>
                    </div>
                  )}
                </section>

                {/* actions */}
                <section className="flex flex-wrap items-center gap-2">
                  <Btn onClick={() => act("/reapply-rules", "rules")} busy={busy === "rules"}>Re-apply rules</Btn>
                  <Btn onClick={() => act("/retry-ai", "retry", { ai_provider: provider, all_lines: true })} busy={busy === "retry"} disabled={!provider}>Re-run AI on all lines</Btn>
                  <span title="Approves every suggestion at once. It does NOT count as checking them, so it does not build the accuracy figures: use the ✓ on each line for that."><Btn onClick={() => act("/accept-review", "accept")} busy={busy === "accept"} disabled={!counts.review}>Accept suggestions ({counts.review})</Btn></span>
                  <Btn onClick={() => act("/keep-current", "keep")} busy={busy === "keep"} disabled={!counts.unassigned}>Keep current for unassigned ({counts.unassigned})</Btn>
                  <span title="Creates a payee rule for each line YOU coded or approved, so the same payee is coded the same way next time. Suggestions the system made on its own are never remembered.">
                    <Btn onClick={() => remember()} busy={busy === "remember"}>★ Remember reviewed lines</Btn></span>
                  <div className="flex-1" />
                  <Btn onClick={download}><Download className="h-3.5 w-3.5" /> General ledger (Excel)</Btn>
                  <button onClick={() => setConfirmApply(true)} title={systems.find((s) => s.key === run.provider)?.supports_apply === false ? "Writing back to this system is not supported yet" : undefined}
                    disabled={!report.balanced || !run.pending_changes || run.status !== "ready" || !!busy || systems.find((s) => s.key === run.provider)?.supports_apply === false}
                    className="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-lg text-xs font-bold cursor-pointer">
                    Apply {run.pending_changes ?? 0} changes to {run.provider_label || "Xero"}
                  </button>
                </section>

                {/* grid */}
                <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
                  <div className="p-3 border-b border-slate-100 flex items-center gap-2 text-[11px] font-bold">
                    {(["all", "history", "ai", "review", "unassigned", "changed", "locked"] as const).map((f) => (
                      <button key={f} onClick={() => { setFilter(f); setPage(0); }}
                        className={`px-2.5 py-1 rounded-full border cursor-pointer ${filter === f ? "bg-blue-600 border-blue-600 text-white" : "bg-white border-slate-200 text-slate-500 hover:bg-slate-50"}`}>
                        {f === "ai" ? "AI suggested" : f === "history" ? "History suggested" : f[0].toUpperCase() + f.slice(1)}
                      </button>
                    ))}
                    <span className="ml-auto text-slate-400 font-semibold">{filtered.length} lines</span>
                  </div>
                  <p className="px-3 py-2 text-[10px] text-slate-400 border-b border-slate-100">
                    Categories are chosen automatically (rules first, then AI). Amber rows are low-confidence guesses; red rows have no category yet. Change any line with its dropdown.
                  </p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400">
                        <tr><th className="p-2 text-left">Date</th><th className="p-2 text-left">Description</th><th className="p-2 text-right">Amount</th>
                          <th className="p-2 text-left">Current</th><th className="p-2 text-left w-64">Category</th><th className="p-2 text-left">Source</th><th className="p-2 text-left">Note</th></tr>
                      </thead>
                      <tbody>
                        {shown.map((i) => (
                          <tr key={i.id} className={`border-t border-slate-100 ${i.status === "review" ? "bg-amber-50/50" : i.status === "unassigned" ? "bg-red-50/40" : ""}`}>
                            <td className="p-2 whitespace-nowrap">{i.date}</td>
                            <td className="p-2 max-w-xs"><div className="truncate font-medium text-slate-700" title={i.description}>{i.description || "—"}</div><div className="truncate text-[10px] text-slate-400">{i.contact}</div></td>
                            <td className={`p-2 text-right font-semibold whitespace-nowrap ${i.amount < 0 ? "text-red-600" : "text-emerald-600"}`}>{money(i.amount)}</td>
                            <td className="p-2 font-mono text-slate-500" title={nameOf(i.current_code)}>{i.current_code || "—"}</td>
                            <td className="p-2">
                              {i.status === "locked" ? (
                                <span className="flex items-center gap-1 text-slate-400"><Lock className="h-3 w-3" /> {i.current_code} {nameOf(i.current_code)}</span>
                              ) : (
                                <div className="flex items-center gap-1.5">
                                  <select value={i.proposed_code || ""} disabled={run.status !== "ready" || !!busy} onChange={(e) => setCode(i, e.target.value)}
                                    className={`w-full border rounded-lg px-2 py-1 text-xs bg-white focus:outline-none focus:border-blue-500 ${i.status === "unassigned" ? "border-red-300" : i.status === "review" ? "border-amber-300" : "border-slate-200"}`}>
                                    <option value="">— choose —</option>
                                    {accounts.map((a) => <option key={a.code} value={a.code}>{a.code} · {a.name}</option>)}
                                  </select>
                                  {(i.source === "manual" || i.approved) && i.proposed_code && (i.contact || i.description) && (
                                    <button onClick={() => remember([i.id])} disabled={run.status !== "ready" || !!busy}
                                      title={`Remember: ${i.contact || "this payee"} (${i.direction}) → ${i.proposed_code}. Future lines from this payee will be coded this way first.`}
                                      className="shrink-0 h-6 w-6 rounded-md border border-indigo-300 text-indigo-600 hover:bg-indigo-50 disabled:opacity-40 cursor-pointer text-xs font-bold">★</button>
                                  )}
                                  {i.status === "review" && i.proposed_code && (
                                    <button onClick={() => act(`/items/${encodeURIComponent(i.id)}/confirm`, "confirm")} disabled={run.status !== "ready" || !!busy}
                                      title="Checked: keep this suggestion. Confirming line by line is what builds the accuracy figures."
                                      className="shrink-0 h-6 w-6 rounded-md border border-emerald-300 text-emerald-600 hover:bg-emerald-50 disabled:opacity-40 cursor-pointer text-xs font-bold">✓</button>
                                  )}
                                </div>
                              )}
                            </td>
                            <td className="p-2">{i.source && <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded-full border ${SOURCE[i.source] || ""}`}>{i.source}{i.source === "ai" && i.confidence != null ? ` ${Math.round(i.confidence * 100)}%` : ""}{i.source === "history" && i.level ? ` · ${i.level}` : ""}{i.auto_approved ? " · auto" : ""}</span>}{i.history_conflict && <span title={i.history_conflict} className="ml-1 text-[9px] font-bold px-1.5 py-0.5 rounded-full border bg-amber-50 text-amber-700 border-amber-200">≠ history</span>}</td>
                            <td className="p-2 text-[10px] text-slate-400 max-w-[14rem] truncate" title={i.reason}>{i.reason}</td>
                          </tr>
                        ))}
                        {shown.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-slate-400">No lines match this filter.</td></tr>}
                      </tbody>
                    </table>
                  </div>
                  {pages > 1 && (
                    <div className="p-3 border-t border-slate-100 flex items-center justify-center gap-3 text-xs font-semibold">
                      <button disabled={page === 0} onClick={() => setPage(page - 1)} className="px-3 py-1 rounded-lg border border-slate-200 disabled:opacity-40 cursor-pointer">Prev</button>
                      <span className="text-slate-500">Page {page + 1} / {pages}</span>
                      <button disabled={page >= pages - 1} onClick={() => setPage(page + 1)} className="px-3 py-1 rounded-lg border border-slate-200 disabled:opacity-40 cursor-pointer">Next</button>
                    </div>
                  )}
                </section>
              </>
            )}

            {!run && <p className="text-center text-xs text-slate-400 py-16">Pick a period and press “Fetch ledger &amp; categorise”.</p>}
          </>
        )}
      </div>

      {/* confirm apply */}
      {confirmApply && run && report && (
        <Modal>
          <h3 className="text-sm font-bold text-slate-900">Write {run.pending_changes} changes to {run.provider_label || "Xero"}?</h3>
          <p className="text-xs text-slate-500 mt-2 leading-relaxed">
            This recodes ledger lines in <b>{run.organization}</b> ({run.date_from} to {run.date_to}). Reconciled transactions and transfers are left alone.
            {run.provider_label || "Xero"} has no one-click undo, so download the general-ledger Excel first — it lists each line&apos;s previous code.
          </p>
          <div className="mt-5 flex gap-2">
            <button onClick={download} className="flex-1 py-2.5 bg-slate-100 hover:bg-slate-200 border border-slate-200 rounded-xl text-xs font-bold cursor-pointer">Download Excel first</button>
            <button onClick={doApply} className="flex-1 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-bold cursor-pointer">Apply to {run.provider_label || "Xero"}</button>
            <button onClick={() => setConfirmApply(false)} className="px-4 py-2.5 border border-slate-200 rounded-xl text-xs font-bold cursor-pointer">Cancel</button>
          </div>
        </Modal>
      )}
      {run?.status === "applying" && <Modal><div className="flex flex-col items-center py-4"><RefreshCw className="h-8 w-8 text-blue-600 animate-spin mb-3" /><p className="text-sm font-bold">Updating {run.provider_label || "Xero"}…</p><p className="text-xs text-slate-500 mt-1">{run.progress}</p></div></Modal>}
      {showResult && run?.apply_results && (
        <Modal>
          <h3 className="text-sm font-bold text-slate-900">{run.apply_results.transactions_failed ? "Applied with some failures" : `Ledger updated in ${run.provider_label || "Xero"}`}</h3>
          <div className="grid grid-cols-3 gap-2 mt-4 text-center">
            <Stat label="Lines recoded" value={String(run.apply_results.lines_changed)} tone="green" />
            <Stat label="Transactions" value={String(run.apply_results.transactions_updated)} />
            <Stat label="Failed" value={String(run.apply_results.transactions_failed)} tone="red" />
          </div>
          {run.apply_results.errors.length > 0 && (
            <ul className="mt-3 text-[10px] text-red-700 bg-red-50 border border-red-100 rounded-lg p-2 max-h-32 overflow-y-auto space-y-1 break-words">
              {run.apply_results.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
          <button onClick={() => setShowResult(false)} className="mt-5 w-full py-2.5 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-bold cursor-pointer">Close</button>
        </Modal>
      )}

      <style jsx global>{`
        .input { background:#fff; border:1px solid #e2e8f0; border-radius:0.5rem; padding:0.4rem 0.65rem; font-size:0.75rem; color:#1e293b; }
        .input:focus { outline:none; border-color:#3b82f6; }
      `}</style>
    </main>
  );
}

// ---------------------------------------------------------------- small pieces
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">{label}</span>{children}</label>;
}
function Stat({ label, value, tone }: { label: string; value: string; tone?: "green" | "red" | "blue" }) {
  const c = tone === "green" ? "text-emerald-600" : tone === "red" ? "text-red-600" : tone === "blue" ? "text-blue-600" : "text-slate-800";
  return <div className="bg-white border border-slate-200 rounded-xl px-4 py-3"><div className="text-[9px] font-bold text-slate-400 uppercase tracking-wider">{label}</div><div className={`text-lg font-bold ${c}`}>{value}</div></div>;
}
function Btn({ children, onClick, busy, disabled }: { children: React.ReactNode; onClick: () => void; busy?: boolean; disabled?: boolean }) {
  return <button onClick={onClick} disabled={disabled || busy} className="flex items-center gap-1.5 px-3.5 py-2 bg-white hover:bg-slate-50 disabled:opacity-40 border border-slate-200 rounded-lg text-xs font-bold text-slate-700 cursor-pointer">{busy && <RefreshCw className="h-3 w-3 animate-spin" />}{children}</button>;
}
function Modal({ children }: { children: React.ReactNode }) {
  return <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-sm flex items-center justify-center p-4"><div className="bg-white rounded-2xl p-6 max-w-md w-full shadow-2xl">{children}</div></div>;
}

// ---------------------------------------------------------------- rules tab
function RulesTab({ rules, accounts, reload, setError, provider, systemLabel, clientId, clientName, connected }: {
  rules: Rule[]; accounts: Account[]; reload: () => void; setError: (e: string | null) => void;
  provider: string; systemLabel: string; clientId: string; clientName: string; connected: boolean;
}) {
  const blank = { name: "", keywords: "", direction: "any", account_code: "", priority: 100, active: true };
  const [form, setForm] = useState(blank);
  const [onlyThisClient, setOnlyThisClient] = useState(false);

  const save = async () => {
    setError(null);
    try {
      await api("/ledger/rules", { method: "POST", body: JSON.stringify({
        ...form, priority: Number(form.priority) || 100, provider, client_id: onlyThisClient && clientId ? clientId : null,
      }) });
      setForm(blank); reload();
    } catch (e: any) { setError(e.message); }
  };
  const toggle = async (r: Rule) => { await api(`/ledger/rules/${r.id}`, { method: "PUT", body: JSON.stringify({ ...r, active: !r.active }) }); reload(); };
  const remove = async (r: Rule) => { await api(`/ledger/rules/${r.id}`, { method: "DELETE" }); reload(); };

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 leading-relaxed">
        Rules for <b>{systemLabel || "this accounting system"}</b>{clientName ? <> · {clientName}</> : null}. Account codes differ between systems, so each rule belongs to one system; tick the box to limit it to the selected client.
        Rules run <b>before</b> the AI and always win. A line matches when its description, contact or reference contains any keyword (comma-separated, not case-sensitive). Lower priority numbers run first.
      </p>
      <section className="bg-white border border-slate-200 rounded-2xl p-5 grid grid-cols-1 md:grid-cols-6 gap-3 items-end">
        <Field label="Rule name"><input className="input w-full" value={form.name} placeholder="Office rent" onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <div className="md:col-span-2"><Field label="Keywords"><input className="input w-full" value={form.keywords} placeholder="rent, lease, landlord" onChange={(e) => setForm({ ...form, keywords: e.target.value })} /></Field></div>
        <Field label="Money">
          <select className="input w-full" value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
            <option value="any">Spent or received</option><option value="spend">Spent only</option><option value="receive">Received only</option>
          </select>
        </Field>
        <Field label="Account">
          <select className="input w-full" value={form.account_code} onChange={(e) => setForm({ ...form, account_code: e.target.value })}>
            <option value="">{accounts.length ? "— choose —" : connected ? "Loading accounts…" : `Connect ${systemLabel || "the system"} to list`}</option>
            {accounts.map((a) => <option key={a.code} value={a.code}>{a.code} · {a.name}</option>)}
          </select>
        </Field>
        <button onClick={save} disabled={!form.name || !form.keywords || !form.account_code}
          className="py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white rounded-lg text-xs font-bold cursor-pointer">Add rule</button>
        {clientId && (
          <label className="md:col-span-6 flex items-center gap-2 text-[11px] font-semibold text-slate-500 cursor-pointer">
            <input type="checkbox" checked={onlyThisClient} onChange={(e) => setOnlyThisClient(e.target.checked)} />
            Only for {clientName || "this client"} (otherwise it applies to every {systemLabel} client)
          </label>
        )}
      </section>

      <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400">
            <tr><th className="p-2 text-left">Priority</th><th className="p-2 text-left">Name</th><th className="p-2 text-left">Keywords</th><th className="p-2 text-left">Money</th><th className="p-2 text-left">Account</th><th className="p-2 text-left">Applies to</th><th className="p-2" /></tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id} className={`border-t border-slate-100 ${r.active ? "" : "opacity-40"}`}>
                <td className="p-2 font-mono">{r.priority}</td><td className="p-2 font-semibold">{r.name}</td><td className="p-2 text-slate-500">{r.keywords}</td>
                <td className="p-2 capitalize">{r.direction}</td><td className="p-2 font-mono">{r.account_code} <span className="font-sans text-slate-400">{accounts.find((a) => a.code === r.account_code)?.name}</span></td>
                <td className="p-2 text-slate-500">{r.client_id ? (r.client_id === clientId && clientName ? clientName : "One client") : "All clients"}</td>
                <td className="p-2 text-right whitespace-nowrap">
                  <button onClick={() => toggle(r)} className="text-[11px] font-bold text-blue-600 mr-3 cursor-pointer">{r.active ? "Disable" : "Enable"}</button>
                  <button onClick={() => remove(r)} className="text-slate-400 hover:text-red-600 cursor-pointer" aria-label="Delete rule"><Trash2 className="h-3.5 w-3.5 inline" /></button>
                </td>
              </tr>
            ))}
            {rules.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-slate-400">No rules yet. Without rules the AI decides every line.</td></tr>}
          </tbody>
        </table>
      </section>
    </div>
  );
}
