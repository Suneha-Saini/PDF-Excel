"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw, AlertTriangle, Upload } from "lucide-react";
import { api, getApiBase } from "../../lib/api";

// History tab: import a ledger export so the system learns how this client has always been coded.
// Two steps: preview (detect layout + columns, propose account mapping; stores nothing) then import (confirmed mapping).

interface Account { code: string; name: string }
interface HistImport { id: string; filename: string; layout: string; lines: number; ignored: number; date_from: string | null; date_to: string | null; created_at: string }
interface HistSection { name: string; lines: number; code: string | null; chart_name: string | null; ignore: boolean; how: string }
interface HistPreview {
  detected: boolean; valid?: boolean; message?: string; preview_rows?: string[][];
  header_row?: number; headers?: string[]; columns?: Record<string, number>; amount_sign?: string; dayfirst?: boolean;
  layout?: string; lines?: number; date_from?: string | null; date_to?: string | null; distinct_contacts?: number;
  skipped?: Record<string, number>; sections?: HistSection[];
  sample?: { date: string; description: string; contact: string; amount: number; account: string }[];
  warnings?: string[]; client?: string; filename?: string; sign_hint?: string | null;
}

const IGNORE = "__ignore__";
const COLUMN_FIELDS: [string, string][] = [
  ["date", "Date"], ["description", "Description"], ["contact", "Payee / name"], ["reference", "Reference"],
  ["debit", "Debit"], ["credit", "Credit"], ["amount", "Amount"], ["account", "Account (flat files)"], ["tax", "Tax"],
];

const money = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ago = (iso: string) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">{label}</span>{children}</label>;
}

async function postForm<T = any>(path: string, form: FormData): Promise<T> {
  // Not api(): that helper forces a JSON content type, but a file upload must let the browser set the multipart boundary.
  const res = await fetch(`${getApiBase()}${path}`, { method: "POST", body: form, headers: { Authorization: "Bearer local-session" } });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export default function HistoryTab({ accounts, setError, provider, systemLabel, clientId, clientName, connected }: {
  accounts: Account[]; setError: (e: string | null) => void; provider: string; systemLabel: string;
  clientId: string; clientName: string; connected: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<HistPreview | null>(null);
  const [sectionMap, setSectionMap] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"" | "preview" | "import">("");
  const [list, setList] = useState<{ imports: HistImport[]; history_lines_total: number } | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const loadList = useCallback(() => {
    if (!provider || !clientId) return;
    api<{ imports: HistImport[]; history_lines_total: number }>(`/history?provider=${encodeURIComponent(provider)}&client_id=${encodeURIComponent(clientId)}`)
      .then(setList).catch(() => {});
  }, [provider, clientId]);
  useEffect(() => { loadList(); setFile(null); setPreview(null); setDone(null); }, [loadList]);

  const mappingOf = (p: HistPreview) => ({ header_row: p.header_row, columns: p.columns, amount_sign: p.amount_sign, dayfirst: p.dayfirst });

  const runPreview = async (f: File, mapping?: object) => {
    setBusy("preview"); setError(null); setDone(null);
    try {
      const form = new FormData();
      form.append("file", f); form.append("provider", provider); form.append("client_id", clientId);
      if (mapping) form.append("mapping", JSON.stringify(mapping));
      const p = await postForm<HistPreview>("/history/preview", form);
      setPreview(p);
      if (p.sections) {   // keep the user's earlier choices for sections that are still there; otherwise take the suggestion
        setSectionMap((prev) => Object.fromEntries(p.sections!.map((s) => [s.name, prev[s.name] !== undefined ? prev[s.name] : (s.code || (s.ignore ? IGNORE : ""))])));
      }
    } catch (e: any) { setError(e.message); setPreview(null); } finally { setBusy(""); }
  };

  const changeMapping = (patch: Record<string, any>) => {
    if (!file || !preview) return;
    const base = mappingOf(preview);
    runPreview(file, { ...base, ...patch, columns: { ...(base.columns || {}), ...(patch.columns || {}) } });
  };

  const mappedLines = (preview?.sections || []).filter((s) => sectionMap[s.name] && sectionMap[s.name] !== IGNORE).reduce((n, s) => n + s.lines, 0);
  const unmapped = (preview?.sections || []).filter((s) => !sectionMap[s.name]).length;

  const doImport = async () => {
    if (!file || !preview) return;
    setBusy("import"); setError(null);
    try {
      const form = new FormData();
      form.append("file", file); form.append("provider", provider); form.append("client_id", clientId);
      form.append("mapping", JSON.stringify(mappingOf(preview)));
      form.append("section_map", JSON.stringify(Object.fromEntries(Object.entries(sectionMap).map(([k, v]) => [k, v && v !== IGNORE ? v : null]))));
      const r = await postForm<{ imported: number; ignored: number; date_from: string; date_to: string; history_lines_total: number }>("/history/import", form);
      setDone(`Imported ${r.imported.toLocaleString()} lines (${r.date_from} to ${r.date_to}); ${r.ignored} ignored. This client now has ${r.history_lines_total.toLocaleString()} lines of history.`);
      setFile(null); setPreview(null); loadList();
    } catch (e: any) { setError(e.message); } finally { setBusy(""); }
  };

  const undo = async (id: string) => {
    try { await api(`/history/${id}`, { method: "DELETE" }); loadList(); } catch (e: any) { setError(e.message); }
  };

  if (!connected || !clientId) {
    return <p className="text-xs text-slate-500">Pick a connected accounting system and a client on the Review &amp; Apply tab first.</p>;
  }
  const headerOptions = (preview?.headers || []).map((h, i) => ({ i, h: h || `(column ${i + 1})` }));

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 leading-relaxed">
        Teach the system how <b>{clientName}</b> ({systemLabel}) has always been coded. Upload a ledger export covering the last 12 months:
        {" "}<b>QuickBooks</b> “Transaction Detail by Account”, <b>Xero</b> “Account Transactions” or a general ledger detail (Excel or CSV).
        You confirm the columns and which account each section means before anything is saved. Nothing is sent to {systemLabel}.
      </p>

      <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-bold cursor-pointer">
            <Upload className="h-3.5 w-3.5" /> {file ? "Choose another file" : "Choose ledger export"}
            <input type="file" accept=".xlsx,.xlsm,.xls,.csv,.tsv,.txt" className="hidden" onChange={(e) => {
              const f = e.target.files?.[0]; e.target.value = "";
              if (f) { setFile(f); setSectionMap({}); runPreview(f); }
            }} />
          </label>
          {file && <span className="text-xs text-slate-600 font-semibold">{file.name} ({(file.size / 1024).toFixed(0)} KB)</span>}
          {busy === "preview" && <span className="flex items-center gap-1 text-xs text-slate-500"><RefreshCw className="h-3 w-3 animate-spin" /> Reading…</span>}
          {list && <span className="ml-auto text-[11px] text-slate-500">History available for this client: <b>{list.history_lines_total.toLocaleString()}</b> lines</span>}
        </div>
        {done && <p className="text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">{done}</p>}
      </section>

      {/* could not find the header: let the user point at it */}
      {preview && !preview.detected && (
        <section className="bg-white border border-amber-200 rounded-2xl p-5 space-y-3">
          <p className="text-xs font-semibold text-amber-800">{preview.message}</p>
          <div className="overflow-x-auto"><table className="text-[10px] text-slate-600"><tbody>
            {preview.preview_rows?.map((r, ri) => (
              <tr key={ri} className="border-t border-slate-100">
                <td className="pr-3"><button className="underline cursor-pointer text-blue-600" onClick={() => file && runPreview(file, { header_row: ri })}>use row {ri + 1}</button></td>
                {r.map((c, ci) => <td key={ci} className="pr-3 whitespace-nowrap max-w-[10rem] truncate">{c}</td>)}
              </tr>
            ))}
          </tbody></table></div>
        </section>
      )}

      {preview?.detected && (
        <>
          <section className="bg-white border border-slate-200 rounded-2xl p-5 space-y-3">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
              <span className="font-bold text-slate-800">{preview.filename}</span>
              {preview.valid && <>
                <span>layout: <b>{preview.layout === "grouped" ? "one section per account" : "account on every row"}</b></span>
                <span><b>{preview.lines?.toLocaleString()}</b> lines</span><span>{preview.date_from} → {preview.date_to}</span><span>{preview.distinct_contacts} payees</span>
              </>}
            </div>
            {preview.message && <p className="text-xs font-semibold text-amber-700">{preview.message}</p>}
            {preview.skipped && Object.keys(preview.skipped).length > 0 && (
              <p className="text-[11px] text-slate-400">Skipped: {Object.entries(preview.skipped).map(([k, v]) => `${v} ${k}`).join(", ")}.</p>
            )}
            {preview.warnings?.map((w, i) => <p key={i} className="text-[11px] font-semibold text-amber-700 flex gap-1.5"><AlertTriangle className="h-3.5 w-3.5 shrink-0" />{w}</p>)}
            {preview.sign_hint && <p className="text-[11px] font-semibold text-blue-700 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">{preview.sign_hint}</p>}

            <details className="text-xs" open={!preview.valid}>
              <summary className="font-bold text-blue-600 cursor-pointer">Columns &amp; options (check these)</summary>
              <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-3">
                {COLUMN_FIELDS.map(([field, label]) => (
                  <Field key={field} label={label}>
                    <select className="input w-full" value={preview.columns?.[field] ?? ""} disabled={busy !== ""}
                      onChange={(e) => changeMapping({ columns: { [field]: e.target.value === "" ? null : Number(e.target.value) } })}>
                      <option value="">(none)</option>
                      {headerOptions.map((o) => <option key={o.i} value={o.i}>{o.h}</option>)}
                    </select>
                  </Field>
                ))}
                <Field label="Amount sign">
                  <select className="input w-full" value={preview.amount_sign} disabled={busy !== ""} onChange={(e) => changeMapping({ amount_sign: e.target.value })}>
                    <option value="as_is">As in the file</option><option value="flip">Flip (+ ↔ −)</option>
                    <option value="by_account_class">By account type (expenses positive in the file)</option>
                  </select>
                </Field>
                <Field label="Date order">
                  <select className="input w-full" value={preview.dayfirst ? "d" : "m"} disabled={busy !== ""} onChange={(e) => changeMapping({ dayfirst: e.target.value === "d" })}>
                    <option value="d">Day first (31/12/2026)</option><option value="m">Month first (12/31/2026)</option>
                  </select>
                </Field>
              </div>
              <p className="mt-2 text-[10px] text-slate-400">Money out is stored as negative. With Debit/Credit columns a debit is money out and a credit is money in; with a single Amount column, check the samples below and flip if needed.</p>
            </details>

            {preview.sample && preview.sample.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="text-left p-1">Date</th><th className="text-left p-1">Description</th><th className="text-left p-1">Payee</th><th className="text-right p-1">Amount</th><th className="text-left p-1">Account in file</th></tr></thead>
                  <tbody>{preview.sample.map((s, i) => (
                    <tr key={i} className="border-t border-slate-100"><td className="p-1 whitespace-nowrap">{s.date}</td><td className="p-1 max-w-xs truncate">{s.description}</td><td className="p-1">{s.contact}</td>
                      <td className={`p-1 text-right font-semibold ${s.amount < 0 ? "text-red-600" : "text-emerald-600"}`}>{money(s.amount)}</td><td className="p-1">{s.account}</td></tr>
                  ))}</tbody>
                </table>
                <p className="text-[10px] text-slate-400 mt-1">First {preview.sample.length} parsed lines. Check the dates and the sign of the amounts.</p>
              </div>
            )}
          </section>

          {preview.valid && preview.sections && (
            <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
              <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">Which account does each section mean? <span className="font-normal text-slate-400">Bank and card accounts should be ignored: only category accounts teach coding.</span></div>
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">In the file</th><th className="p-2 text-right">Lines</th><th className="p-2 text-left w-80">Account in {systemLabel}</th><th className="p-2 text-left">Match</th></tr></thead>
                <tbody>
                  {preview.sections.map((s) => (
                    <tr key={s.name} className={`border-t border-slate-100 ${!sectionMap[s.name] ? "bg-amber-50/50" : ""}`}>
                      <td className="p-2 font-medium text-slate-700">{s.name}</td><td className="p-2 text-right">{s.lines.toLocaleString()}</td>
                      <td className="p-2">
                        <select className="input w-full" value={sectionMap[s.name] ?? ""} onChange={(e) => setSectionMap({ ...sectionMap, [s.name]: e.target.value })}>
                          <option value="">— choose —</option><option value={IGNORE}>Ignore (bank / not a category)</option>
                          {accounts.map((a) => <option key={a.code} value={a.code}>{a.code} · {a.name}</option>)}
                        </select>
                      </td>
                      <td className="p-2 text-[10px] text-slate-400">{s.how}</td>
                    </tr>
                  ))}
                  {preview.sections.length === 0 && <tr><td colSpan={4} className="p-6 text-center text-slate-400">No transaction rows found with this mapping.</td></tr>}
                </tbody>
              </table>
              <div className="p-3 border-t border-slate-100 flex flex-wrap items-center gap-3">
                <button onClick={doImport} disabled={busy !== "" || mappedLines === 0}
                  className="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 text-white rounded-lg text-xs font-bold cursor-pointer">
                  {busy === "import" ? "Importing…" : `Import ${mappedLines.toLocaleString()} lines`}
                </button>
                {unmapped > 0 && <span className="text-[11px] font-semibold text-amber-700">{unmapped} section(s) still unchosen: they will be skipped.</span>}
                <span className="text-[10px] text-slate-400">Re-importing an overlapping export does not double count, and you can undo any import below.</span>
              </div>
            </section>
          )}
        </>
      )}

      {list && list.imports.length > 0 && (
        <section className="bg-white border border-slate-200 rounded-2xl overflow-hidden">
          <div className="p-3 border-b border-slate-100 text-xs font-bold text-slate-700">Imports for this client</div>
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-400"><tr><th className="p-2 text-left">File</th><th className="p-2 text-left">Period</th><th className="p-2 text-right">Lines</th><th className="p-2 text-right">Ignored</th><th className="p-2 text-left">Imported</th><th className="p-2" /></tr></thead>
            <tbody>{list.imports.map((i) => (
              <tr key={i.id} className="border-t border-slate-100"><td className="p-2 font-medium">{i.filename}</td><td className="p-2">{i.date_from} → {i.date_to}</td>
                <td className="p-2 text-right">{i.lines.toLocaleString()}</td><td className="p-2 text-right text-slate-400">{i.ignored}</td><td className="p-2 text-slate-500">{ago(i.created_at)}</td>
                <td className="p-2 text-right"><button onClick={() => undo(i.id)} className="text-[11px] font-bold text-slate-400 hover:text-red-600 cursor-pointer">Undo</button></td></tr>
            ))}</tbody>
          </table>
        </section>
      )}
    </div>
  );
}
