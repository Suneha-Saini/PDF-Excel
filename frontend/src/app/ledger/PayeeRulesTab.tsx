"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { api } from "../../lib/api";

// Remembered payee rules: "this payee, in this direction, goes to this account" for one client. They run FIRST,
// before keyword rules, history and AI. Created only by a person: Remember on a line, or ticking suggestions here.

interface Rule {
  id: number; payee: string; payee_key: string; direction: string; account_code: string; account_name: string | null;
  tax_label: string | null; abn: string | null; source: string; active: boolean; overrides: number; batch_id: string;
  times: number; agree: number; last_seen: string | null; created_at: string;
}
interface Listing { rules: Rule[]; payees_known: number; history_lines: number; last_batch: { id: string; rules: number; created_at: string } | null }
interface Candidate {
  payee_key: string; payee_display: string; direction: string; account_code: string; account_name: string;
  lines: number; agree_pct: number; last_seen: string | null; tax_label: string | null;
}

const SOURCE_LABEL: Record<string, string> = { remembered: "Reviewer rule (Remember)", history: "From history", manual: "Manual" };
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);

export default function PayeeRulesTab({ setError, provider, systemLabel, clientId, clientName, connected }: {
  setError: (e: string | null) => void; provider: string; systemLabel: string; clientId: string; clientName: string; connected: boolean;
}) {
  const [data, setData] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(false);
  const [q, setQ] = useState("");
  const [cands, setCands] = useState<Candidate[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"" | "find" | "create" | "undo">("");
  const [notice, setNotice] = useState<string | null>(null);

  const scope = `provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}`;
  const load = useCallback(async () => {
    if (!provider || !clientId || !connected) return;
    setLoading(true);
    try { setData(await api<Listing>(`/payee-rules?${scope}`)); } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, [provider, clientId, connected, scope, setError]);
  useEffect(() => { setData(null); setCands(null); setNotice(null); load(); }, [load]);

  const keyOf = (c: Candidate) => `${c.payee_key}|${c.direction}`;
  const find = async () => {
    setBusy("find"); setError(null); setNotice(null);
    try {
      const r = await api<{ candidates: Candidate[] }>(`/payee-rules/candidates?${scope}`);
      setCands(r.candidates); setPicked(new Set(r.candidates.map(keyOf)));
    } catch (e: any) { setError(e.message); } finally { setBusy(""); }
  };
  const create = async () => {
    if (!cands) return;
    setBusy("create"); setError(null);
    try {
      const entries = cands.filter((c) => picked.has(keyOf(c))).map((c) => ({
        payee_key: c.payee_key, payee_display: c.payee_display, direction: c.direction, account_code: c.account_code, tax_label: c.tax_label,
      }));
      const r = await api<{ created: number; skipped: number }>("/payee-rules", { method: "POST", body: JSON.stringify({ provider, client_id: clientId, entries }) });
      setNotice(`Created ${r.created} payee rule(s)${r.skipped ? `; ${r.skipped} already existed` : ""}. You can undo this batch below.`);
      setCands(null); load();
    } catch (e: any) { setError(e.message); } finally { setBusy(""); }
  };
  const undo = async () => {
    if (!data?.last_batch || !window.confirm(`Undo the last batch: remove ${data.last_batch.rules} rule(s)?`)) return;
    setBusy("undo"); setError(null);
    try {
      const r = await api<{ removed: number }>("/payee-rules/undo-last-batch", { method: "POST", body: JSON.stringify({ provider, client_id: clientId }) });
      setNotice(`Removed ${r.removed} rule(s).`); load();
    } catch (e: any) { setError(e.message); } finally { setBusy(""); }
  };
  const toggle = async (r: Rule) => { try { await api(`/payee-rules/${r.id}`, { method: "PATCH", body: JSON.stringify({ active: !r.active }) }); load(); } catch (e: any) { setError(e.message); } };
  const remove = async (r: Rule) => {
    if (!window.confirm(`Delete the rule for ${r.payee}?`)) return;
    try { await api(`/payee-rules/${r.id}`, { method: "DELETE" }); load(); } catch (e: any) { setError(e.message); }
  };

  const rows = useMemo(() => (data?.rules || []).filter((r) => !q || `${r.payee} ${r.account_code} ${r.account_name || ""}`.toLowerCase().includes(q.toLowerCase())), [data, q]);

  if (!connected || !clientId) return <p className="text-xs text-slate-500">Pick a connected accounting system and a client on the Review &amp; Apply tab first.</p>;

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 leading-relaxed">
        Payee rules for <b>{clientName}</b> ({systemLabel}): “this payee, in this direction, goes to this account”. They run <b>first</b>: before keyword rules, history and AI.
        Only a person creates them, with <b>★ Remember</b> on a line you coded, <b>Remember reviewed lines</b> on the review screen, or by ticking payees below. You can undo any batch.
      </p>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[["Payee rules", data?.rules.length], ["Payees known", data?.payees_known], ["History lines", data?.history_lines], ["Overridden rules", data?.rules.filter((r) => r.overrides > 0).length]].map(([l, v]) => (
          <div key={String(l)} className="bg-white border border-slate-200 rounded-xl px-4 py-3">
            <div className="text-[9px] font-bold text-slate-400 uppercase tracking-wider">{l}</div>
            <div className="text-xl font-bold text-slate-800">{v == null ? "—" : Number(v).toLocaleString()}</div>
          </div>
        ))}
      </section>

      {notice && <p className="text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">{notice}</p>}

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <button onClick={find} disabled={busy !== ""} className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-xs font-bold cursor-pointer">
            {busy === "find" ? "Looking…" : "Suggest rules from history"}
          </button>
          <span className="text-[11px] text-slate-400">Payees that history always codes the same way and that have no rule yet. Nothing is created until you confirm.</span>
          {data?.last_batch && (
            <button onClick={undo} disabled={busy !== ""} className="ml-auto text-xs font-bold text-slate-500 hover:text-red-600 cursor-pointer disabled:opacity-50">
              Undo last batch ({data.last_batch.rules} rule{data.last_batch.rules === 1 ? "" : "s"})
            </button>
          )}
        </div>

        {cands && (
          <div className="space-y-2">
            {cands.length === 0 ? <p className="text-xs text-slate-400">No more consistent payees to suggest (or none have enough history yet).</p> : (
              <>
                <div className="overflow-x-auto max-h-80 overflow-y-auto border border-slate-100 rounded-lg">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400 sticky top-0">
                      <tr><th className="p-2 w-8"><input type="checkbox" checked={picked.size === cands.length} onChange={(e) => setPicked(e.target.checked ? new Set(cands.map(keyOf)) : new Set())} /></th>
                        <th className="p-2 text-left">Payee</th><th className="p-2 text-left">Direction</th><th className="p-2 text-left">Account</th><th className="p-2 text-right">Lines</th><th className="p-2 text-right">Agree</th><th className="p-2 text-left">Last seen</th></tr>
                    </thead>
                    <tbody>{cands.map((c) => (
                      <tr key={keyOf(c)} className="border-t border-slate-100">
                        <td className="p-2"><input type="checkbox" checked={picked.has(keyOf(c))} onChange={(e) => { const n = new Set(picked); e.target.checked ? n.add(keyOf(c)) : n.delete(keyOf(c)); setPicked(n); }} /></td>
                        <td className="p-2 font-medium">{c.payee_display}</td><td className="p-2">{cap(c.direction)}</td>
                        <td className="p-2">{c.account_code} <span className="text-slate-500">{c.account_name}</span></td>
                        <td className="p-2 text-right">{c.lines}</td><td className="p-2 text-right">{c.agree_pct}%</td><td className="p-2 text-slate-500">{c.last_seen || "—"}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
                <button onClick={create} disabled={busy !== "" || picked.size === 0} className="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 text-white rounded-lg text-xs font-bold cursor-pointer">
                  {busy === "create" ? "Creating…" : `Create ${picked.size} rule${picked.size === 1 ? "" : "s"}`}
                </button>
              </>
            )}
          </div>
        )}
      </section>

      <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
        <div className="p-3 border-b border-slate-100 flex items-center gap-3">
          <span className="text-xs font-bold text-slate-700">Rules</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter payee or account…" className="input w-64" />
          {loading && <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-400" />}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400">
              <tr><th className="p-2 text-left">ABN</th><th className="p-2 text-left">Payee</th><th className="p-2 text-left">Direction</th><th className="p-2 text-left">Account</th>
                <th className="p-2 text-left">Tax rate</th><th className="p-2 text-right">Times</th><th className="p-2 text-left">Source</th><th className="p-2 text-left">Last seen</th><th className="p-2" /></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={`border-t border-slate-100 ${r.active ? "" : "opacity-40"}`}>
                  <td className="p-2 font-mono text-[10px] text-slate-500">{r.abn || "—"}</td>
                  <td className="p-2 font-medium">{r.payee}{r.overrides > 0 && <span title="A person changed lines this rule coded: check it still holds" className="ml-1.5 text-[9px] font-bold px-1.5 py-0.5 rounded-full border bg-amber-50 text-amber-700 border-amber-200">overridden {r.overrides}×</span>}</td>
                  <td className="p-2">{cap(r.direction)}</td>
                  <td className="p-2">{r.account_code} <span className="text-slate-500">{r.account_name}</span></td>
                  <td className="p-2 text-slate-600">{r.tax_label || "—"}</td>
                  <td className="p-2 text-right" title={`${r.agree} of ${r.times} matching history lines were coded to this account`}>{r.times}{r.times > 0 && r.agree < r.times && <span className="text-amber-600"> ({r.agree} agree)</span>}</td>
                  <td className="p-2 text-slate-500">{SOURCE_LABEL[r.source] || r.source}</td><td className="p-2 text-slate-500">{r.last_seen || "—"}</td>
                  <td className="p-2 text-right whitespace-nowrap">
                    <button onClick={() => toggle(r)} className="text-[11px] font-bold text-blue-600 mr-3 cursor-pointer">{r.active ? "Disable" : "Enable"}</button>
                    <button onClick={() => remove(r)} className="text-slate-400 hover:text-red-600 cursor-pointer" aria-label="Delete rule"><Trash2 className="h-3.5 w-3.5 inline" /></button>
                  </td>
                </tr>
              ))}
              {data && rows.length === 0 && <tr><td colSpan={9} className="p-8 text-center text-slate-400">{data.rules.length === 0 ? "No payee rules yet. Use ★ Remember on a line you coded, or suggest some from history above." : "No rules match the filter."}</td></tr>}
            </tbody>
          </table>
        </div>
        <p className="p-2 text-[10px] text-slate-400 border-t border-slate-100">“Times” is how many history lines for this payee and direction exist; the bracket shows how many were coded to the rule&apos;s account. ABN is filled in when the platform lists one for that supplier.</p>
      </section>
    </div>
  );
}
