"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../../lib/api";

// Accuracy tab: how often the suggestions were right, measured ONLY from people's own per-line decisions,
// and the per-client shadow period that decides whether auto-approval is allowed.

interface Tally { checked: number; right: number; accuracy_pct: number | null }
interface Policy {
  active: boolean; eligible: boolean; override: boolean; suspended: boolean; why: string;
  min_checked: number; min_accuracy_pct: number; high_checked: number; high_right: number; high_accuracy_pct: number | null;
  auto_lines: number; auto_corrected: number;
}
interface Summary {
  days: number; note: string; policy: Policy | null;
  totals: Tally & { bulk_accepted: number; auto_approved: number; auto_corrected: number };
  by_confidence: (Tally & { bucket: string })[];
  by_source: Record<string, Tally>;
  by_client: (Tally & { provider: string; client_id: string; client_name: string | null })[];
  by_month: (Tally & { month: string })[];
  recent_corrections: { date: string | null; description: string | null; contact: string | null; amount: number | null; source: string; level: string | null; suggested: string; final: string; client_name: string | null }[];
}

const pct = (n: number | null) => (n == null ? "—" : `${n}%`);
const money = (n: number | null) => (n == null ? "" : n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "green" | "amber" | "red" }) {
  const c = tone === "green" ? "text-emerald-600" : tone === "amber" ? "text-amber-600" : tone === "red" ? "text-red-600" : "text-slate-800";
  return (
    <div className="bg-white border border-slate-200 rounded-xl px-4 py-3">
      <div className="text-[9px] font-bold text-slate-400 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold ${c}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-400 mt-0.5">{sub}</div>}
    </div>
  );
}

export default function AccuracyTab({ setError, provider, systemLabel, clientId, clientName, connected }: {
  setError: (e: string | null) => void; provider: string; systemLabel: string; clientId: string; clientName: string; connected: boolean;
}) {
  const [days, setDays] = useState(180);
  const [scope, setScope] = useState<"client" | "all">("client");
  const [data, setData] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const scoped = scope === "client" && !!provider && !!clientId;
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = `days=${days}${scoped ? `&provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}` : ""}`;
      setData(await api<Summary>(`/accuracy?${q}`));
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, [days, scoped, provider, clientId, setError]);
  useEffect(() => { load(); }, [load]);

  const toggleOverride = async (value: boolean) => {
    if (value && !window.confirm(
      `Switch on auto-approval for ${clientName} now, skipping the shadow period?\n\n` +
      "High-confidence history suggestions that change a code will be pre-approved (still shown, still logged, and only written when you press Apply). " +
      "It pauses itself if people keep correcting auto-approved lines.")) return;
    setBusy(true); setError(null);
    try {
      await api("/accuracy/policy", { method: "PUT", body: JSON.stringify({ provider, client_id: clientId, auto_override: value }) });
      await load();
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  const p = data?.policy;
  const progress = p ? Math.min(100, Math.round((100 * p.high_checked) / p.min_checked)) : 0;

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 leading-relaxed">
        How often the suggestions were right, from <b>decisions people made line by line</b> (the ✓ on a suggestion, or changing it). The system never marks its own work:
        “Accept suggestions” for many lines at once and lines approved automatically are <b>not counted</b>.
      </p>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <div className="flex rounded-lg border border-slate-200 overflow-hidden font-bold">
          {([[90, "3 months"], [180, "6 months"], [365, "12 months"]] as const).map(([d, l]) => (
            <button key={d} onClick={() => setDays(d)} className={`px-3 py-1.5 cursor-pointer ${days === d ? "bg-blue-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}>{l}</button>
          ))}
        </div>
        <div className="flex rounded-lg border border-slate-200 overflow-hidden font-bold">
          <button disabled={!provider || !clientId} onClick={() => setScope("client")} className={`px-3 py-1.5 cursor-pointer disabled:opacity-40 ${scope === "client" ? "bg-blue-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}>{clientName || "This client"}</button>
          <button onClick={() => setScope("all")} className={`px-3 py-1.5 cursor-pointer ${scope === "all" ? "bg-blue-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}>All clients</button>
        </div>
        {loading && <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-400" />}
      </div>

      {data && (
        <>
          <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Suggestions right" value={pct(data.totals.accuracy_pct)} sub={`${data.totals.right} of ${data.totals.checked} checked by a person`}
              tone={data.totals.accuracy_pct == null ? undefined : data.totals.accuracy_pct >= 95 ? "green" : data.totals.accuracy_pct >= 85 ? "amber" : "red"} />
            <Stat label="Checked line by line" value={String(data.totals.checked)} sub="the only lines that count" />
            <Stat label="Bulk-accepted" value={String(data.totals.bulk_accepted)} sub="not counted as checks" />
            <Stat label="Auto-approved" value={String(data.totals.auto_approved)} sub={`${data.totals.auto_corrected} later corrected`} tone={data.totals.auto_corrected > 0 ? "amber" : undefined} />
          </section>

          {scoped && p && (
            <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">Auto-approval for {clientName}</h2>
                <span className={`text-[11px] font-bold px-2.5 py-1 rounded-full border ${p.suspended ? "bg-red-50 text-red-700 border-red-200" : p.active ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-slate-50 text-slate-500 border-slate-200"}`}>
                  {p.suspended ? "Paused" : p.active ? "On" : "Shadow period"}
                </span>
              </div>
              <div>
                <div className="h-2 rounded-full bg-slate-100 overflow-hidden"><div className={`h-full ${p.eligible ? "bg-emerald-500" : "bg-blue-500"}`} style={{ width: `${progress}%` }} /></div>
                <p className="text-xs text-slate-600 mt-1.5">
                  <b>{p.high_checked}</b> of {p.min_checked} High suggestions checked{p.high_accuracy_pct != null && <>, <b>{p.high_accuracy_pct}%</b> right (need {p.min_accuracy_pct}%)</>}
                </p>
              </div>
              <p className="text-xs text-slate-500">{p.why}</p>
              <p className="text-[11px] text-slate-400 leading-relaxed">
                Once earned, High-confidence history suggestions that change a code are pre-approved. They stay visible (marked “auto”), are logged, and nothing is written to {systemLabel} until you press Apply.
                It pauses itself if more than 5% of recent auto-approved lines get corrected.
              </p>
              <label className="flex items-center gap-2 text-[11px] font-semibold text-slate-600 cursor-pointer">
                <input type="checkbox" checked={p.override} disabled={busy} onChange={(e) => toggleOverride(e.target.checked)} />
                Allow auto-approval now for this client (skip the shadow period)
              </label>
            </section>
          )}

          <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
            <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">By confidence <span className="font-normal text-slate-400">High should be near 100%: it is what auto-approval relies on.</span></div>
            <table className="w-full text-xs">
              <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">Confidence</th><th className="p-2 text-right">Checked</th><th className="p-2 text-right">Right</th><th className="p-2 text-right">Accuracy</th></tr></thead>
              <tbody>
                {data.by_confidence.map((r) => (
                  <tr key={r.bucket} className="border-t border-slate-100"><td className="p-2 capitalize">{r.bucket}</td><td className="p-2 text-right">{r.checked}</td><td className="p-2 text-right">{r.right}</td><td className="p-2 text-right font-semibold">{pct(r.accuracy_pct)}</td></tr>
                ))}
                {data.by_confidence.length === 0 && <tr><td colSpan={4} className="p-6 text-center text-slate-400">Nothing checked yet. On the Review tab, use ✓ on a suggestion once you have checked it, or change it if it is wrong.</td></tr>}
              </tbody>
            </table>
          </section>

          {!scoped && data.by_client.length > 0 && (
            <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
              <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">By client</div>
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">Client</th><th className="p-2 text-left">System</th><th className="p-2 text-right">Checked</th><th className="p-2 text-right">Right</th><th className="p-2 text-right">Accuracy</th></tr></thead>
                <tbody>{data.by_client.map((c) => (
                  <tr key={`${c.provider}-${c.client_id}`} className="border-t border-slate-100"><td className="p-2 font-medium">{c.client_name || c.client_id}</td><td className="p-2 text-slate-500">{c.provider}</td><td className="p-2 text-right">{c.checked}</td><td className="p-2 text-right">{c.right}</td><td className="p-2 text-right font-semibold">{pct(c.accuracy_pct)}</td></tr>
                ))}</tbody>
              </table>
            </section>
          )}

          {data.by_month.length > 0 && (
            <section className="bg-white border border-slate-200 rounded-2xl p-5">
              <div className="text-xs font-bold text-slate-700 mb-3">Share right by month</div>
              <div className="flex items-end gap-3 h-24">
                {data.by_month.map((m) => (
                  <div key={m.month} className="flex flex-col items-center gap-1" title={`${m.month}: ${m.right} of ${m.checked} right`}>
                    <div className="w-8 bg-slate-100 rounded-t flex items-end" style={{ height: "64px" }}>
                      <div className="w-full bg-blue-500 rounded-t" style={{ height: `${m.accuracy_pct ?? 0}%` }} />
                    </div>
                    <span className="text-[9px] text-slate-400">{m.month.slice(2)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {data.recent_corrections.length > 0 && (
            <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
              <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">Recent corrections <span className="font-normal text-slate-400">where a person changed the suggestion</span></div>
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">Date</th><th className="p-2 text-left">Line</th><th className="p-2 text-right">Amount</th><th className="p-2 text-left">Suggested</th><th className="p-2 text-left">Became</th></tr></thead>
                <tbody>{data.recent_corrections.map((c, i) => (
                  <tr key={i} className="border-t border-slate-100"><td className="p-2 whitespace-nowrap">{c.date}</td>
                    <td className="p-2 max-w-xs truncate" title={c.description || ""}>{c.contact ? `${c.contact} · ` : ""}{c.description}</td>
                    <td className="p-2 text-right">{money(c.amount)}</td>
                    <td className="p-2 text-red-600">{c.suggested} <span className="text-[9px] text-slate-400">({c.source}{c.level ? ` ${c.level}` : ""})</span></td>
                    <td className="p-2 text-emerald-700">{c.final}</td></tr>
                ))}</tbody>
              </table>
            </section>
          )}
          <p className="text-[10px] text-slate-400">{data.note}</p>
        </>
      )}
      {!connected && <p className="text-[11px] text-slate-400">Pick a connected system and client on Review &amp; Apply to see one client's figures; “All clients” works without one.</p>}
    </div>
  );
}
