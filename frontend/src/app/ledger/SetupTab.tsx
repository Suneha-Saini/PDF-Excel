"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../../lib/api";

// Ledger setup: potential problems in how this client's ledger has been coded (learned from its whole history),
// ranked High / Medium / Info. They are questions for an accountant, not verdicts.

interface Issue {
  severity: "High" | "Medium" | "Info"; kind: string; issue: string; payee: string; direction: string;
  account: string; account_name: string; tax: string; times: number; line_ids: string[];
}
interface Report {
  client: string; history_lines: number; truncated: boolean; note: string;
  issues: Issue[]; summary: { High: number; Medium: number; Info: number };
  multi_tax_accounts: { account: string; account_name: string; lines: number; tax_codes: { tax: string; lines: number }[] }[];
  coverage: { coded_lines: number; lines_with_tax_info: number; gst_checks_ran: boolean; gst_registration?: string; entity_type?: string };
}

interface ClientCfg {
  settings: { entity_type: string; gst_registration: string; bas_frequency: string };
  choices: Record<"entity_type" | "gst_registration" | "bas_frequency", { value: string; label: string }[]>;
  effects: Record<"entity_type" | "gst_registration" | "bas_frequency", string>;
}

const SEV: Record<string, string> = {
  High: "bg-red-50 text-red-700 border-red-200", Medium: "bg-amber-50 text-amber-700 border-amber-200", Info: "bg-slate-100 text-slate-600 border-slate-200",
};

export default function SetupTab({ setError, provider, systemLabel, clientId, clientName, connected }: {
  setError: (e: string | null) => void; provider: string; systemLabel: string; clientId: string; clientName: string; connected: boolean;
}) {
  const [data, setData] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<"All" | "High" | "Medium" | "Info">("All");
  const [onlyFlaggedPayee, setOnlyFlaggedPayee] = useState("");

  // Client details: facts the accountant sets, which change what counts as a problem
  const [cfg, setCfg] = useState<ClientCfg | null>(null);
  const [savingCfg, setSavingCfg] = useState(false);
  const cfgQuery = `provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}`;
  useEffect(() => {
    setCfg(null);
    if (!provider || !clientId || !connected) return;
    api<ClientCfg>(`/client-settings?${cfgQuery}`).then(setCfg).catch((e) => setError(e.message));
  }, [provider, clientId, connected]); // eslint-disable-line react-hooks/exhaustive-deps

  const load = useCallback(async () => {
    if (!provider || !clientId || !connected) return;
    setLoading(true); setError(null);
    try { setData(await api<Report>(`/ledger/setup-issues?provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}`)); }
    catch (e: any) { setError(e.message); setData(null); } finally { setLoading(false); }
  }, [provider, clientId, connected, setError]);
  useEffect(() => { setData(null); load(); }, [load]);

  // Older stored lines have no tax code. Re-read them from the platform (read-only) and add it: nothing else changes.
  const [taxMsg, setTaxMsg] = useState<string | null>(null);
  const [taxBusy, setTaxBusy] = useState(false);
  const refreshTax = async () => {
    setTaxBusy(true); setError(null); setTaxMsg(null);
    try {
      const r = await api<{ lines_updated: number; lines_not_found: number; fetched: number }>("/ledger/refresh-tax", { method: "POST", body: JSON.stringify({ provider, client_id: clientId }) });
      setTaxMsg(r.fetched === 0 ? "Every stored line already has its tax information." :
        `Added tax codes to ${r.lines_updated} stored line(s)${r.lines_not_found ? `; ${r.lines_not_found} are no longer on ${systemLabel} so stay without` : ""}.`);
      await load();
    } catch (e: any) { setError(e.message); } finally { setTaxBusy(false); }
  };

  const saveCfg = async (field: string, value: string) => {
    setSavingCfg(true); setError(null);
    try {
      setCfg(await api<ClientCfg>("/client-settings", { method: "PUT", body: JSON.stringify({ provider, client_id: clientId, [field]: value }) }));
      await load();                                  // the checks depend on these, so run them again
    } catch (e: any) { setError(e.message); } finally { setSavingCfg(false); }
  };

  if (!connected || !clientId) {
    return <p className="text-xs text-slate-500">Pick a connected accounting system and a client on the Review &amp; Apply tab first.</p>;
  }

  const rows = (data?.issues || []).filter((i) => (filter === "All" || i.severity === filter) && (!onlyFlaggedPayee || i.payee.toLowerCase().includes(onlyFlaggedPayee.toLowerCase())));
  const cov = data?.coverage;
  const taxPct = cov && cov.coded_lines ? Math.round((100 * cov.lines_with_tax_info) / cov.coded_lines) : 0;

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 leading-relaxed">
        Potential problems in how <b>{clientName}</b> ({systemLabel}) has been coded, learned from its whole history (live ledgers and imported exports).
        These are <b>questions for an accountant, not errors</b>: several tax codes on one account, or a payment to an income account, can be legitimate.
      </p>

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
        <div className="flex items-center gap-3">
          <h2 className="text-xs font-bold uppercase tracking-wide text-slate-700">Client details</h2>
          <span className="text-[11px] text-slate-400">Set by you. Nothing is assumed while a setting is “Not set”.</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Accounting system</span>
            <div className="input w-full bg-slate-50 text-slate-500">{systemLabel}</div>
          </div>
          {(["entity_type", "gst_registration", "bas_frequency"] as const).map((f) => (
            <label key={f} className="block">
              <span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">{f === "entity_type" ? "Entity type" : f === "gst_registration" ? "GST registration" : "BAS frequency"}</span>
              <select className="input w-full" disabled={!cfg || savingCfg} value={cfg?.settings[f] || "unknown"} onChange={(e) => saveCfg(f, e.target.value)}>
                {(cfg?.choices[f] || []).map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
              </select>
              <span className="block text-[10px] text-slate-400 mt-1 leading-snug">{cfg?.effects[f]}</span>
            </label>
          ))}
        </div>
      </section>

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          {(["High", "Medium", "Info", "All"] as const).map((s) => (
            <button key={s} onClick={() => setFilter(s)}
              className={`px-3 py-1.5 rounded-lg border text-xs font-bold cursor-pointer ${filter === s ? "bg-blue-600 border-blue-600 text-white" : "bg-white border-slate-200 text-slate-600 hover:bg-slate-50"}`}>
              {s}{data && s !== "All" ? ` (${data.summary[s]})` : data && s === "All" ? ` (${data.issues.length})` : ""}
            </button>
          ))}
          <input value={onlyFlaggedPayee} onChange={(e) => setOnlyFlaggedPayee(e.target.value)} placeholder="Filter by payee…" className="input w-48" />
          <button onClick={load} disabled={loading} className="ml-auto flex items-center gap-1.5 text-xs font-bold text-blue-600 cursor-pointer disabled:opacity-50">
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> Check again
          </button>
        </div>
        {data && taxPct < 100 && (
          <div className="flex flex-wrap items-center gap-3">
            <button onClick={refreshTax} disabled={taxBusy} title={`Re-reads this client's transactions from ${systemLabel} (read-only) and adds the tax code to stored lines that lack one. Nothing else changes.`}
              className="px-3 py-1.5 bg-white border border-slate-200 hover:bg-slate-50 disabled:opacity-50 rounded-lg text-xs font-bold text-slate-700 cursor-pointer">
              {taxBusy ? "Reading tax codes…" : "Refresh tax data"}
            </button>
            <span className="text-[10px] text-slate-400">Read-only from {systemLabel}. Adds the tax code to older stored lines; suggestions, approvals and decisions are untouched.</span>
          </div>
        )}
        {taxMsg && <p className="text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">{taxMsg}</p>}
        {data && (
          <p className="text-[11px] text-slate-500">
            Learned from <b>{data.history_lines.toLocaleString()}</b> coded lines. Tax information is available for <b>{cov?.lines_with_tax_info.toLocaleString()}</b> of {cov?.coded_lines.toLocaleString()} ({taxPct}%)
            {taxPct < 100 && <span className="text-amber-700 font-semibold"> — tax checks only cover those. Refresh the tax data (below), or import an export that has a tax column, to include the rest.</span>}
            {cov && !cov.gst_checks_ran && taxPct > 0 && (cov.gst_registration === "not_registered"
              ? " GST-free-account checks are off because the client is not registered for GST."
              : " GST-specific checks were skipped because these tax codes do not look like GST. Set GST registration above if the client is registered.")}
          </p>
        )}
      </section>

      <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400">
              <tr><th className="p-2 text-left">Severity</th><th className="p-2 text-left">Issue</th><th className="p-2 text-left">Payee</th><th className="p-2 text-left">Direction</th>
                <th className="p-2 text-left">Account</th><th className="p-2 text-left">Tax rate</th><th className="p-2 text-right">Times</th></tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.kind}-${r.payee}-${r.account}-${r.tax}-${i}`} className="border-t border-slate-100 align-top">
                  <td className="p-2"><span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${SEV[r.severity]}`}>{r.severity}</span></td>
                  <td className="p-2 max-w-md">{r.issue}</td>
                  <td className="p-2 font-medium">{r.payee}</td><td className="p-2">{r.direction}</td>
                  <td className="p-2">{r.account} <span className="text-slate-500">{r.account_name}</span></td>
                  <td className="p-2 text-slate-600">{r.tax || "—"}</td><td className="p-2 text-right font-semibold">{r.times}</td>
                </tr>
              ))}
              {data && rows.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-slate-400">{data.issues.length === 0 ? "No issues found in the history available." : "No issues match this filter."}</td></tr>}
              {!data && loading && <tr><td colSpan={7} className="p-8 text-center text-slate-400">Reading this client&apos;s history…</td></tr>}
            </tbody>
          </table>
        </div>
        {data?.truncated && <p className="p-2 text-[10px] text-slate-400 border-t border-slate-100">Showing the first 500 issues.</p>}
      </section>

      {data && data.multi_tax_accounts.length > 0 && (
        <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
          <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">Accounts with more than one tax code</div>
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">Account</th><th className="p-2 text-right">Lines</th><th className="p-2 text-left">Tax codes (lines)</th></tr></thead>
            <tbody>{data.multi_tax_accounts.map((m) => (
              <tr key={m.account} className="border-t border-slate-100"><td className="p-2 font-medium">{m.account} {m.account_name}</td><td className="p-2 text-right">{m.lines}</td>
                <td className="p-2 text-slate-600">{m.tax_codes.map((t) => `${t.tax} (${t.lines})`).join("; ")}</td></tr>
            ))}</tbody>
          </table>
        </section>
      )}
      {data && <p className="text-[10px] text-slate-400">{data.note}</p>}
    </div>
  );
}
