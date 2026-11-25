"use client";

import React, { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

// "Webhooks & sync status": shows whether the platform can reach us, the exact URL to register, a live list of
// what arrived, and a Send-test-event button that proves tunnel -> receiver -> signature check -> processing.

interface Hook { configured: boolean; path: string; register_url: string | null }
interface Status {
  worker_running: boolean; sync_enabled: boolean; nightly_hour: number; public_url: string | null;
  webhooks: Record<string, Hook>;
  recent_events: { provider: string; detail: string | null; matched_targets: number; is_test: boolean; received_at: string }[];
}
interface TestResult { via: string; url: string; ok: boolean; status_code: number | null; explanation: string; recorded?: boolean; matched_targets?: number | null; note?: string }

const ago = (iso: string) => {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

export default function SyncStatusPanel({ provider, clientId, label }: { provider: string; clientId: string; label: string }) {
  const [st, setSt] = useState<Status | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<TestResult | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try { setSt(await api("/sync/status")); setErr(""); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  }, []);

  useEffect(() => {
    if (!open) return;
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [open, load]);

  const hook = st?.webhooks[provider];
  const sendTest = async () => {
    setBusy(true); setRes(null);
    try { setRes(await api("/sync/test-webhook", { method: "POST", body: JSON.stringify({ provider, client_id: clientId }) })); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    setBusy(false); load();
  };

  return (
    <details className="w-full text-[11px]" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer font-bold text-slate-600 select-none">Webhooks &amp; sync status</summary>
      <div className="mt-2 space-y-2 bg-slate-50 border border-slate-200 rounded-lg p-3">
        {err && <p className="font-semibold text-red-600 break-words">{err}</p>}
        {st && (
          <>
            <p>Background worker: <b className={st.worker_running ? "text-emerald-600" : "text-red-600"}>{st.worker_running ? "running" : "not running"}</b>
              {" · "}nightly sync at {String(st.nightly_hour).padStart(2, "0")}:00
              {" · "}public tunnel: {st.public_url ? <b className="text-emerald-600">{st.public_url}</b> : <b className="text-amber-700">not detected (start ngrok on port 8000)</b>}</p>
            {hook ? (
              <>
                <p>{label} signing key in .env: <b className={hook.configured ? "text-emerald-600" : "text-red-600"}>{hook.configured ? "set ✓" : "missing ✗"}</b></p>
                <p className="break-all">Register this URL in the {label} developer portal: <code className="bg-white border border-slate-200 rounded px-1">{hook.register_url ?? `<your public address>${hook.path}`}</code></p>
                <div className="flex items-center gap-3">
                  <button onClick={sendTest} disabled={busy || !hook.configured} className="px-3 py-1 rounded-lg bg-blue-600 text-white font-bold cursor-pointer disabled:opacity-40">
                    {busy ? "Sending…" : "Send test event"}
                  </button>
                  <span className="text-slate-400">Sends a signed fake change for the selected client through the tunnel. Nothing is written to {label}.</span>
                </div>
              </>
            ) : <p className="text-slate-500">{label} has no webhooks: it syncs nightly only.</p>}
            {res && (
              <div className={`rounded-lg border px-3 py-2 ${res.ok ? "bg-emerald-50 border-emerald-200 text-emerald-800" : "bg-red-50 border-red-200 text-red-700"}`}>
                <p className="font-bold">{res.ok ? "✓ Test passed" : "✗ Test failed"}{res.status_code != null && ` (HTTP ${res.status_code})`} · via {res.via}</p>
                <p>{res.explanation}</p>
                {res.ok && <p>{res.matched_targets ? `Matched ${res.matched_targets} auto-sync client(s) — a sync was queued.` : "No auto-sync client matched, so no sync ran. Switch Auto-sync on for this client to see it fetch."}</p>}
                {res.note && <p className="text-slate-500">{res.note}</p>}
              </div>
            )}
            <div>
              <p className="font-bold text-slate-500 uppercase text-[9px] tracking-wider mb-1">Recent events received</p>
              {st.recent_events.length === 0 ? <p className="text-slate-400">Nothing received yet. Real events only arrive after you change data in {label} and it has the URL above registered.</p> : (
                <table className="w-full text-left">
                  <tbody>
                    {st.recent_events.map((e, i) => (
                      <tr key={i} className="border-t border-slate-200">
                        <td className="py-1 pr-3 whitespace-nowrap text-slate-400">{ago(e.received_at)}</td>
                        <td className="pr-3 font-semibold">{e.provider}{e.is_test && <span className="ml-1 px-1 rounded bg-slate-200 text-slate-600 text-[9px]">TEST</span>}</td>
                        <td className="pr-3 break-all">{e.detail}</td>
                        <td className="whitespace-nowrap text-slate-500">{e.matched_targets} synced</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </div>
    </details>
  );
}
