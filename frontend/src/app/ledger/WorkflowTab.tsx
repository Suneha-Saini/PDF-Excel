"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../../lib/api";

// Client workflow: one review of one client for one BAS period.
//   Files -> Coding check -> GL review -> BAS review -> Sign-off
// Stage results are computed by the backend from the data; this screen only shows them and records people's decisions.

interface Stage { key: string; label: string; status: "done" | "needs_you" | "waiting" | "not_run" | "na"; summary: string; rating?: string; detail?: any }
interface Finding { id: number; stage: string; severity: "High" | "Medium" | "Info"; kind: string; text: string; status: string; note: string | null }
interface Review {
  id: string; client_name: string; kind: string; period: { start: string; end: string; label: string };
  due_date: string | null; days_left: number | null; status: string; assigned_to: string | null; notes: string | null;
  checklist: Record<string, boolean>; checklist_labels: Record<string, string>;
  bas_figures: Record<string, number | null>; bas_estimate: Record<string, any> | null;
  settings: { entity_type: string; gst_registration: string; bas_frequency: string };
  stages: Stage[]; findings: Finding[]; findings_summary: { open: number; high_open: number; total: number };
  signed_off: { by: string | null; at: string | null } | null; can_sign_off: boolean; blockers: string[];
}
interface Summary { id: string; kind: string; period: { label: string }; due_date: string | null; days_left: number | null; status: string; assigned_to: string | null;
  findings_summary: { open: number; high_open: number }; stages: { key: string; status: string }[] }
interface Listing { workflows: Summary[]; bas_frequency: string; gst_registration: string }

const ICON: Record<string, { sym: string; cls: string; label: string }> = {
  done: { sym: "✓", cls: "bg-emerald-50 text-emerald-600 border-emerald-200", label: "Done" },
  needs_you: { sym: "●", cls: "bg-amber-50 text-amber-600 border-amber-200", label: "Needs you" },
  waiting: { sym: "◔", cls: "bg-slate-100 text-slate-500 border-slate-200", label: "Waiting" },
  not_run: { sym: "○", cls: "bg-blue-50 text-blue-600 border-blue-200", label: "Ready to run" },
  na: { sym: "–", cls: "bg-slate-50 text-slate-400 border-slate-200", label: "Not applicable" },
};
const SEV: Record<string, string> = { High: "bg-red-50 text-red-700 border-red-200", Medium: "bg-amber-50 text-amber-700 border-amber-200", Info: "bg-slate-100 text-slate-600 border-slate-200" };
const RATING: Record<string, string> = { RED: "text-red-600", AMBER: "text-amber-600", GREEN: "text-emerald-600" };
const money = (n: number | null | undefined) => (n == null ? "" : n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const STATUS_LABEL: Record<string, string> = { not_started: "Not started", in_review: "In review", signed_off: "Signed off" };

function dueText(due: string | null, left: number | null) {
  if (!due) return null;
  const d = new Date(due + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return left == null ? `Due ${d}` : left >= 0 ? `Due ${d} · due in ${left} day${left === 1 ? "" : "s"}` : `Due ${d} · ${-left} day${left === -1 ? "" : "s"} overdue`;
}

// the last few BAS periods, as first-day-of-period dates, for "start an earlier review"
function recentPeriodStarts(freq: string): { start: string; label: string }[] {
  const out: { start: string; label: string }[] = [];
  const today = new Date();
  let y = today.getFullYear(), m = today.getMonth(); // 0-based
  const step = freq === "monthly" ? 1 : freq === "quarterly" ? 3 : 12;
  if (freq === "quarterly") m = m - (m % 3);
  if (freq === "annual") { if (m < 6) y -= 1; m = 6; }
  for (let i = 0; i < 8; i++) {
    m -= step;
    while (m < 0) { m += 12; y -= 1; }
    const s = new Date(Date.UTC(y, m, 1));
    out.push({ start: s.toISOString().slice(0, 10), label: freq === "monthly" ? s.toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" })
      : freq === "quarterly" ? `${s.toLocaleDateString(undefined, { month: "short", timeZone: "UTC" })}-${new Date(Date.UTC(y, m + 2, 1)).toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" })}` : `FY ${y}-${String(y + 1).slice(2)}` });
  }
  return out;
}

export default function WorkflowTab({ setError, provider, systemLabel, clientId, clientName, connected, goToSetup, goToReview }: {
  setError: (e: string | null) => void; provider: string; systemLabel: string; clientId: string; clientName: string; connected: boolean;
  goToSetup: () => void; goToReview: () => void;
}) {
  const [list, setList] = useState<Listing | null>(null);
  const [rev, setRev] = useState<Review | null>(null);
  const [busy, setBusy] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [form, setForm] = useState({ assigned_to: "", notes: "", due_date: "", status: "not_started" });
  const [figs, setFigs] = useState<Record<string, string>>({});

  const scope = `provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}`;
  const loadList = useCallback(async () => {
    if (!provider || !clientId || !connected) return;
    try { setList(await api<Listing>(`/workflow?${scope}`)); } catch (e: any) { setError(e.message); }
  }, [provider, clientId, connected, scope, setError]);
  useEffect(() => { setRev(null); setList(null); loadList(); }, [loadList]);

  const adopt = (r: Review) => {
    setRev(r);
    setForm({ assigned_to: r.assigned_to || "", notes: r.notes || "", due_date: r.due_date || "", status: r.status === "signed_off" ? "in_review" : r.status });
    setFigs(Object.fromEntries(Object.entries(r.bas_figures).map(([k, v]) => [k, v == null ? "" : String(v)])));
  };
  const call = async (label: string, fn: () => Promise<Review>) => {
    setBusy(label); setError(null);
    try { adopt(await fn()); loadList(); } catch (e: any) { setError(e.message); } finally { setBusy(""); }
  };
  const open = (id: string) => call("open", () => api<Review>(`/workflow/${id}`));
  const create = (start?: string) => call("create", () => api<Review>("/workflow", { method: "POST", body: JSON.stringify({ provider, client_id: clientId, period_start: start || null }) }));
  const patch = (body: object) => rev && call("save", () => api<Review>(`/workflow/${rev.id}`, { method: "PATCH", body: JSON.stringify(body) }));
  const run = (stage: string) => rev && call(`run-${stage}`, () => api<Review>(`/workflow/${rev.id}/run`, { method: "POST", body: JSON.stringify({ stage }) }));
  const refresh = () => rev && call("refresh", () => api<Review>(`/workflow/${rev.id}`));
  // Stored lines from before tax capture have no tax code: read them again (read-only), then redo the BAS review on them
  const refreshTaxAndRun = () => rev && call("tax", async () => {
    await api("/ledger/refresh-tax", { method: "POST", body: JSON.stringify({ provider, client_id: clientId }) });
    return api<Review>(`/workflow/${rev.id}/run`, { method: "POST", body: JSON.stringify({ stage: "all" }) });
  });
  const finding = (f: Finding, status: string) => {
    if (!rev) return;
    let note: string | undefined;
    if (status === "accepted") {
      const n = window.prompt("Why is this fine? (recorded with the finding)");
      if (!n || !n.trim()) return;
      note = n.trim();
    }
    call("finding", () => api<Review>(`/workflow/${rev.id}/findings/${f.id}`, { method: "POST", body: JSON.stringify({ status, note }) }));
  };
  const signOff = () => rev && window.confirm(`Sign off the ${rev.period.label} review for ${rev.client_name}?\nThis records who and when, and locks the review until it is reopened.`) &&
    call("signoff", () => api<Review>(`/workflow/${rev.id}/sign-off`, { method: "POST", body: JSON.stringify({ by: form.assigned_to || null }) }));
  const reopen = () => rev && call("reopen", () => api<Review>(`/workflow/${rev.id}/reopen`, { method: "POST" }));
  const remove = async () => {
    if (!rev || !window.confirm(`Delete the ${rev.period.label} review and its findings?`)) return;
    try { await api(`/workflow/${rev.id}`, { method: "DELETE" }); setRev(null); loadList(); } catch (e: any) { setError(e.message); }
  };

  if (!connected || !clientId) return <p className="text-xs text-slate-500">Pick a connected accounting system and a client on the Review &amp; Apply tab first.</p>;

  // ------------------------------------------------------------ list view
  if (!rev) {
    const freq = list?.bas_frequency;
    return (
      <div className="space-y-4">
        <p className="text-xs text-slate-500 leading-relaxed">
          Reviews for <b>{clientName}</b> ({systemLabel}): one per BAS period, from files through coding, GL and BAS review to sign-off. Results are worked out from the ledger each time;
          nothing here lodges anything or changes your books.
        </p>
        {list && !["monthly", "quarterly", "annual"].includes(freq || "") && (
          <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 text-xs text-amber-800 flex flex-wrap items-center gap-3">
            <span><b>BAS frequency is not set</b> for this client; the review period depends on it.</span>
            <button onClick={goToSetup} className="font-bold underline cursor-pointer">Open Setup › Client details</button>
          </div>
        )}
        {list && ["monthly", "quarterly", "annual"].includes(freq || "") && (
          <section className="bg-white border border-slate-200 rounded-2xl p-5 flex flex-wrap items-center gap-3">
            <button onClick={() => create()} disabled={busy !== ""} className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-xs font-bold cursor-pointer">Start the latest {freq} review</button>
            <select className="input" defaultValue="" onChange={(e) => { if (e.target.value) create(e.target.value); e.target.value = ""; }} disabled={busy !== ""}>
              <option value="">Start an earlier period…</option>
              {recentPeriodStarts(freq!).map((p) => <option key={p.start} value={p.start}>{p.label}</option>)}
            </select>
            <span className="text-[11px] text-slate-400">Frequency: {freq}. GST: {list.gst_registration === "cash" ? "registered, cash basis" : list.gst_registration === "accrual" ? "registered, accruals basis" : list.gst_registration === "not_registered" ? "not registered" : "not set"}.</span>
          </section>
        )}
        <section className="space-y-3">
          {list?.workflows.map((w) => (
            <button key={w.id} onClick={() => open(w.id)} className="w-full text-left bg-white border border-slate-200 hover:border-blue-300 rounded-2xl p-4 cursor-pointer">
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-sm font-bold text-slate-900">{clientName}</span>
                <span className="text-xs text-slate-500">{w.kind} · {w.period.label}</span>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${w.status === "signed_off" ? "bg-emerald-50 text-emerald-700 border-emerald-200" : w.status === "in_review" ? "bg-blue-50 text-blue-700 border-blue-200" : "bg-slate-100 text-slate-500 border-slate-200"}`}>{STATUS_LABEL[w.status]}</span>
                {w.due_date && <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${w.days_left != null && w.days_left < 0 && w.status !== "signed_off" ? "bg-red-50 text-red-700 border-red-200" : "bg-slate-50 text-slate-600 border-slate-200"}`}>{dueText(w.due_date, w.days_left)}</span>}
                {w.assigned_to && <span className="text-[10px] font-bold px-2 py-0.5 rounded-full border bg-slate-50 text-slate-600 border-slate-200">{w.assigned_to}</span>}
                <span className="ml-auto flex gap-1">{w.stages.map((s) => <span key={s.key} title={`${s.key}: ${ICON[s.status].label}`} className={`h-5 w-5 text-[10px] font-bold rounded-full border flex items-center justify-center ${ICON[s.status].cls}`}>{ICON[s.status].sym}</span>)}</span>
              </div>
              {w.findings_summary.open > 0 && <div className="mt-2 text-[11px] text-slate-500">{w.findings_summary.open} open finding(s){w.findings_summary.high_open > 0 && <span className="text-red-600 font-semibold">, {w.findings_summary.high_open} High</span>}</div>}
            </button>
          ))}
          {list && list.workflows.length === 0 && <p className="text-center text-xs text-slate-400 py-10">No reviews yet for this client.</p>}
        </section>
      </div>
    );
  }

  // ------------------------------------------------------------ detail view
  const locked = rev.status === "signed_off";
  const findings = rev.findings.filter((f) => !onlyOpen || f.status === "open");
  const est = rev.bas_estimate;
  return (
    <div className="space-y-4">
      <button onClick={() => { setRev(null); loadList(); }} className="text-xs font-bold text-blue-600 cursor-pointer">← All reviews</button>

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-base font-bold text-slate-900">{rev.client_name}</span>
          <span className="text-xs text-slate-500">{rev.kind} · {rev.period.label} ({rev.period.start} to {rev.period.end})</span>
          <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${locked ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-blue-50 text-blue-700 border-blue-200"}`}>{STATUS_LABEL[rev.status]}</span>
          {rev.due_date && <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${rev.days_left != null && rev.days_left < 0 && !locked ? "bg-red-50 text-red-700 border-red-200" : "bg-slate-50 text-slate-600 border-slate-200"}`}>{dueText(rev.due_date, rev.days_left)}</span>}
          {rev.assigned_to && <span className="text-[10px] font-bold px-2 py-0.5 rounded-full border bg-slate-50 text-slate-600 border-slate-200">{rev.assigned_to}</span>}
          <button onClick={refresh} className="ml-auto flex items-center gap-1 text-xs font-bold text-blue-600 cursor-pointer" title="Re-read the ledger and recompute the stages"><RefreshCw className={`h-3.5 w-3.5 ${busy === "refresh" ? "animate-spin" : ""}`} /> Check again</button>
        </div>

        {locked && rev.signed_off && (
          <div className="flex flex-wrap items-center gap-3 text-xs bg-emerald-50 border border-emerald-200 rounded-xl px-3 py-2 text-emerald-800">
            <span>Signed off by <b>{rev.signed_off.by}</b> on {rev.signed_off.at ? new Date(rev.signed_off.at).toLocaleString() : ""}. The review is locked.</span>
            <button onClick={reopen} className="font-bold underline cursor-pointer">Reopen</button>
          </div>
        )}

        <ol className="divide-y divide-slate-100">
          {rev.stages.map((s) => (
            <li key={s.key} className="py-3 flex flex-wrap items-start gap-3">
              <span className={`mt-0.5 h-6 w-6 shrink-0 text-xs font-bold rounded-full border flex items-center justify-center ${ICON[s.status].cls}`}>{ICON[s.status].sym}</span>
              <div className="w-28 shrink-0"><div className="text-xs font-bold text-slate-800">{s.label}</div><div className="text-[10px] text-slate-400">{ICON[s.status].label}</div></div>
              <div className="flex-1 min-w-[16rem] text-xs text-slate-600">
                {s.rating && <b className={`${RATING[s.rating]} mr-1`}>{s.rating}</b>}{s.summary}
                {s.key === "signoff" && !locked && rev.blockers.length > 0 && <ul className="mt-1 list-disc pl-4 text-[11px] text-slate-400">{rev.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
              </div>
              <div className="flex gap-2">
                {s.key === "coding" && <button onClick={goToReview} className="text-xs font-bold px-3 py-1.5 border border-slate-200 rounded-lg bg-white hover:bg-slate-50 cursor-pointer">Open ledger</button>}
                {s.key === "gl" && !locked && <button onClick={() => run("gl")} disabled={busy !== ""} className="text-xs font-bold px-3 py-1.5 bg-slate-900 text-white rounded-lg disabled:opacity-50 cursor-pointer">{busy === "run-gl" ? "Running…" : s.status === "not_run" ? "Run GL review" : "Run again"}</button>}
                {s.key === "bas" && !locked && s.status !== "na" && rev.settings.gst_registration === "cash" && <button onClick={() => run("bas")} disabled={busy !== ""} className="text-xs font-bold px-3 py-1.5 bg-slate-900 text-white rounded-lg disabled:opacity-50 cursor-pointer">{busy === "run-bas" ? "Running…" : s.status === "not_run" ? "Run BAS review" : "Run again"}</button>}
                {s.key === "bas" && !locked && s.status === "needs_you" && /tax information/.test(s.summary) && (
                  <button onClick={refreshTaxAndRun} disabled={busy !== ""} title="Re-reads the transactions from the accounting system (read-only) to add tax codes, then re-runs the reviews"
                    className="text-xs font-bold px-3 py-1.5 bg-slate-900 text-white rounded-lg disabled:opacity-50 cursor-pointer">{busy === "tax" ? "Reading…" : "Refresh tax data"}</button>
                )}
                {s.key === "bas" && !locked && rev.settings.gst_registration === "unknown" &&<button onClick={goToSetup} className="text-xs font-bold px-3 py-1.5 border border-slate-200 rounded-lg bg-white hover:bg-slate-50 cursor-pointer">Set GST registration</button>}
                {s.key === "signoff" && !locked && <button onClick={signOff} disabled={!rev.can_sign_off || busy !== ""} className="text-xs font-bold px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg disabled:opacity-40 cursor-pointer">Sign off</button>}
              </div>
            </li>
          ))}
        </ol>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 pt-2 border-t border-slate-100">
          <label className="block"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Status</span>
            <select className="input w-full" disabled={locked} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}><option value="not_started">Not started</option><option value="in_review">In review</option></select></label>
          <label className="block"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Assigned to</span>
            <input className="input w-full" disabled={locked} value={form.assigned_to} onChange={(e) => setForm({ ...form, assigned_to: e.target.value })} placeholder="Name" /></label>
          <label className="block"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Due date</span>
            <input type="date" className="input w-full" disabled={locked} value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} /></label>
          <div className="flex items-end"><button disabled={locked || busy !== ""} onClick={() => patch({ status: form.status, assigned_to: form.assigned_to, notes: form.notes, due_date: form.due_date || undefined })}
            className="w-full py-2 bg-slate-900 text-white rounded-lg text-xs font-bold disabled:opacity-40 cursor-pointer">Save</button></div>
          <label className="block md:col-span-4"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Notes</span>
            <textarea className="input w-full" rows={2} disabled={locked} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></label>
          <p className="md:col-span-4 text-[10px] text-slate-400">Standard ATO due dates are filled in; a tax agent&apos;s lodgment program is later, so edit the date if it applies.</p>
        </div>
      </section>

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-2">
        <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">Files in the pack</h2>
        {Object.entries(rev.checklist_labels).map(([k, label]) => (
          <label key={k} className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
            <input type="checkbox" checked={!!rev.checklist[k]} disabled={locked || busy !== ""} onChange={(e) => patch({ checklist: { [k]: e.target.checked } })} /> {label}
          </label>
        ))}
        <p className="text-[10px] text-slate-400">Tick what you have received. The ledger for the period is checked automatically. A client not registered for GST does not need the BAS figures.</p>
      </section>

      {est && (
        <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
          <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">BAS: ledger estimate vs the BAS</h2>
          <table className="text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="pr-8 text-left">Figure</th><th className="pr-8 text-right">Ledger estimate</th><th className="text-right">BAS figure</th></tr></thead>
            <tbody>
              {([["G1", "Total sales (G1)"], ["1A", "GST on sales (1A)"], ["1B", "GST on purchases (1B)"]] as const).map(([k, label]) => (
                <tr key={k} className="border-t border-slate-100"><td className="pr-8 py-1">{label}</td><td className="pr-8 text-right font-semibold">{money(est[k])}</td>
                  <td className="text-right"><input className="input w-32 text-right" disabled={locked} value={figs[k] ?? ""} onChange={(e) => setFigs({ ...figs, [k]: e.target.value })} placeholder="from the BAS" /></td></tr>
              ))}
              {([["G3", "GST-free sales (G3)"], ["G10", "Capital purchases (G10)"], ["G11", "Other purchases (G11)"]] as const).map(([k, label]) => (
                <tr key={k} className="border-t border-slate-100 text-slate-400"><td className="pr-8 py-1">{label}</td><td className="pr-8 text-right">{money(est[k])}</td><td /></tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center gap-3">
            <button disabled={locked || busy !== ""} onClick={() => patch({ bas_figures: Object.fromEntries(["G1", "1A", "1B"].map((k) => [k, figs[k] === "" || figs[k] == null ? null : Number(figs[k])])) })}
              className="px-4 py-1.5 bg-slate-900 text-white rounded-lg text-xs font-bold disabled:opacity-40 cursor-pointer">Reconcile</button>
            <span className="text-[10px] text-slate-400">{est.lines_with_tax} of {est.lines} lines had tax information. An estimate from {est.basis}: it can differ from a lodged BAS (timing, journals). More than 2% apart is a Medium finding, more than 10% High.</span>
          </div>
        </section>
      )}

      <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
        <div className="p-3 border-b border-slate-100 flex flex-wrap items-center gap-3">
          <span className="text-xs font-bold text-slate-700">Findings ({rev.findings_summary.open} open{rev.findings_summary.high_open > 0 ? `, ${rev.findings_summary.high_open} High` : ""})</span>
          <label className="flex items-center gap-1.5 text-[11px] text-slate-500 cursor-pointer ml-auto"><input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} /> Open only</label>
        </div>
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">Severity</th><th className="p-2 text-left">Stage</th><th className="p-2 text-left">Finding</th><th className="p-2 text-left">Status</th><th className="p-2" /></tr></thead>
          <tbody>
            {findings.map((f) => (
              <tr key={f.id} className={`border-t border-slate-100 align-top ${f.status !== "open" ? "opacity-60" : ""}`}>
                <td className="p-2"><span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${SEV[f.severity]}`}>{f.severity}</span></td>
                <td className="p-2 uppercase text-slate-400 text-[10px]">{f.stage}</td>
                <td className="p-2 max-w-xl">{f.text}{f.note && <div className="text-[10px] text-slate-400 mt-0.5">Note: {f.note}</div>}</td>
                <td className="p-2 capitalize">{f.status}</td>
                <td className="p-2 text-right whitespace-nowrap">
                  {!locked && f.status === "open" && <><button onClick={() => finding(f, "resolved")} className="text-[11px] font-bold text-blue-600 mr-3 cursor-pointer">Resolved</button><button onClick={() => finding(f, "accepted")} className="text-[11px] font-bold text-slate-500 cursor-pointer">Accept…</button></>}
                  {!locked && (f.status === "resolved" || f.status === "accepted") && <button onClick={() => finding(f, "open")} className="text-[11px] font-bold text-slate-400 cursor-pointer">Reopen</button>}
                </td>
              </tr>
            ))}
            {findings.length === 0 && <tr><td colSpan={5} className="p-8 text-center text-slate-400">{rev.findings.length === 0 ? "No findings yet. Run the GL review (and the BAS review)." : "No open findings."}</td></tr>}
          </tbody>
        </table>
        <p className="p-2 text-[10px] text-slate-400 border-t border-slate-100">Resolve a finding after fixing it in the books, or accept it with a reason if it is fine. A finding that stops appearing is marked fixed automatically. Open High findings block sign-off.</p>
      </section>

      {!locked && <button onClick={remove} className="text-[11px] font-bold text-slate-400 hover:text-red-600 cursor-pointer">Delete this review</button>}
    </div>
  );
}
