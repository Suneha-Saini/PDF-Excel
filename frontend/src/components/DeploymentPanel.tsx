"use client";

import React, { useCallback, useEffect, useState } from "react";
import { ChevronRight, CheckCircle2, AlertCircle, RefreshCw, Link2, Server, Cloud, Building2, ShieldCheck } from "lucide-react";

export interface Connection {
  integration: "manager_local" | "manager_cloud" | "xero" | "quickbooks";
  label: string;
  status: "not_configured" | "configured" | "connected";
  values: Record<string, string | null>;
  has_secret: Record<string, boolean>;
  last_test_status: "ok" | "error" | null;
  last_test_message: string | null;
}

interface FieldDef {
  key: string;
  label: string;
  placeholder?: string;
  secret?: boolean;
  hint?: boolean;
}

const FIELDS: Record<Connection["integration"], FieldDef[]> = {
  manager_local: [
    { key: "url", label: "Local Server URL", placeholder: "http://127.0.0.1:8090" },
    { key: "username", label: "Username (if using Basic Auth)", placeholder: "administrator" },
    { key: "secret", label: "API Key / Token / Password", secret: true },
    { key: "business_id", label: "Business Identifier (UUID)" },
    { key: "bank_account_id", label: "Target Bank Account ID (UUID)", placeholder: "e.g. 5d8e7c10-09bc-4dfc-bf96-b3334d45d8bf" },
    { key: "suspense_account_id", label: "Default Suspense/Category Account ID (Optional UUID)", placeholder: "Defaults to Suspense if empty" },
  ],
  manager_cloud: [
    { key: "url", label: "Cloud Instance URL", placeholder: "https://yourcompany.manager.io/businesses" },
    { key: "username", label: "Username (if using Basic Auth)", placeholder: "administrator" },
    { key: "secret", label: "API Key / Access Token", secret: true },
    { key: "business_id", label: "Business Identifier (UUID)" },
    { key: "bank_account_id", label: "Target Bank Account ID (UUID)", placeholder: "e.g. 5d8e7c10-09bc-4dfc-bf96-b3334d45d8bf" },
    { key: "suspense_account_id", label: "Default Suspense/Category Account ID (Optional UUID)", placeholder: "Defaults to Suspense if empty" },
  ],
  xero: [
    { key: "client_id", label: "Xero Client ID", secret: true },
    { key: "client_secret", label: "Xero Client Secret", secret: true },
    { key: "default_account_code", label: "Offset / Suspense Account Code", placeholder: "e.g. 850 — leave empty to auto-pick" },
  ],
  quickbooks: [
    { key: "client_id", label: "QuickBooks Client ID", secret: true },
    { key: "client_secret", label: "QuickBooks Client Secret", secret: true },
    { key: "environment", label: "Environment (sandbox / production)", placeholder: "sandbox (default) or production" },
    { key: "default_account_id", label: "Offset / Suspense Account ID (Optional)", placeholder: "Leave empty to auto-pick Suspense / Expense account" },
  ],
};

type Badge = { text: string; cls: string };
const GREEN = "bg-emerald-50 text-emerald-600 border-emerald-200";
const BLUE = "bg-blue-50 text-blue-600 border-blue-200";
const GREY = "bg-slate-100 text-slate-500 border-slate-200";

interface Props {
  apiBaseUrl: string;
  authHeaders: Record<string, string>;
  sharepointStatus: "not_configured" | "configured" | "connected";
  sharepointSite?: string;
  onOpenSharePoint: () => void;
  notice?: { kind: "ok" | "error"; text: string } | null;
}

export default function DeploymentPanel({ apiBaseUrl, authHeaders, sharepointStatus, sharepointSite, onOpenSharePoint, notice }: Props) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [editing, setEditing] = useState<Connection["integration"] | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/connections/`, { headers: authHeaders });
      if (res.ok) setConnections(await res.json());
    } catch {
      /* backend offline: cards stay empty */
    }
  }, [apiBaseUrl, authHeaders]);

  useEffect(() => {
    load();
  }, [load]);

  const current = connections.find((c) => c.integration === editing) || null;

  const open = (c: Connection) => {
    setEditing(c.integration);
    setResult(null);
    // Secret inputs start empty; leaving them empty keeps the saved value.
    const initial: Record<string, string> = {};
    FIELDS[c.integration].forEach((f) => {
      initial[f.key] = f.secret ? "" : (c.values[f.key] as string) || "";
    });
    setForm(initial);
  };

  const split = (integration: Connection["integration"]) => {
    const values: Record<string, string> = {};
    const secrets: Record<string, string> = {};
    FIELDS[integration].forEach((f) => (f.secret ? (secrets[f.key] = form[f.key] || "") : (values[f.key] = form[f.key] || "")));
    return { values, secrets };
  };

  const save = async (): Promise<boolean> => {
    if (!editing) return false;
    try {
      const res = await fetch(`${apiBaseUrl}/connections/${editing}`, {
        method: "PUT",
        headers: authHeaders,
        body: JSON.stringify(split(editing)),
      });
      if (!res.ok) throw new Error(await res.text());
      await load();
      return true;
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || "Save failed." });
      return false;
    }
  };

  // Like the reference UI: Test Connection saves first, then tests.
  const test = async () => {
    if (!editing) return;
    setBusy("test");
    try {
      if (!(await save())) return;
      const res = await fetch(`${apiBaseUrl}/connections/${editing}/test`, { method: "POST", headers: authHeaders });
      const data = await res.json();
      setResult({ ok: data.status === "ok", text: data.message });
      await load();
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || "Test failed." });
    } finally {
      setBusy(null);
    }
  };

  const back = async () => {
    setBusy("save");
    await save();
    setBusy(null);
    setEditing(null);
  };

  const connectXero = async () => {
    setBusy("connect");
    try {
      if (!(await save())) return;
      const res = await fetch(`${apiBaseUrl}/connections/xero/authorize-url`, { headers: authHeaders });
      if (!res.ok) throw new Error((await res.json()).detail || "Could not start Xero sign-in.");
      window.location.href = (await res.json()).url;
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || "Could not start Xero sign-in." });
      setBusy(null);
    }
  };

  const disconnectXero = async () => {
    setBusy("disconnect");
    try {
      await fetch(`${apiBaseUrl}/connections/xero/disconnect`, { method: "POST", headers: authHeaders });
      await load();
      setResult(null);
    } finally {
      setBusy(null);
    }
  };

  const connectQuickbooks = async () => {
    setBusy("connect");
    try {
      if (!(await save())) return;
      const res = await fetch(`${apiBaseUrl}/connections/quickbooks/authorize-url`, { headers: authHeaders });
      if (!res.ok) throw new Error((await res.json()).detail || "Could not start QuickBooks sign-in.");
      window.location.href = (await res.json()).url;
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || "Could not start QuickBooks sign-in." });
      setBusy(null);
    }
  };

  const disconnectQuickbooks = async () => {
    setBusy("disconnect");
    try {
      await fetch(`${apiBaseUrl}/connections/quickbooks/disconnect`, { method: "POST", headers: authHeaders });
      await load();
      setResult(null);
    } finally {
      setBusy(null);
    }
  };

  const clearSaved = async () => {
    if (!editing) return;
    if (!window.confirm("Remove all saved settings for this connection?")) return;
    await fetch(`${apiBaseUrl}/connections/${editing}`, { method: "DELETE", headers: authHeaders });
    await load();
    setEditing(null);
  };

  // ---------------------------------------------------------------- form view
  if (editing && current) {
    const isManager = editing !== "xero" && editing !== "quickbooks";
    const title =
      editing === "manager_local"
        ? "Manager.io (Local)"
        : editing === "manager_cloud"
        ? "Manager.io (Cloud)"
        : editing === "xero"
        ? "Xero"
        : "QuickBooks Online";

    return (
      <div className="space-y-3 bg-slate-50 p-4 border border-slate-200 rounded-2xl">
        <h3 className="text-xs font-bold text-slate-900 uppercase tracking-wide">Configure {title}</h3>
        {isManager && (
          <h4 className="text-[10px] font-bold text-slate-700 uppercase tracking-wide">
            {editing === "manager_local" ? "Manager.io Local Configuration" : "Manager.io Cloud Configuration"}
          </h4>
        )}

        {editing === "xero" && current.status === "connected" && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-center space-y-1.5">
            <div className="text-xs font-bold text-emerald-700 flex items-center justify-center gap-1.5">
              <CheckCircle2 className="h-3.5 w-3.5" /> Connected to Xero
            </div>
            <div className="text-[11px] text-emerald-600">Connected to: {current.values.tenant_name || "Organisation"}</div>
            <button onClick={disconnectXero} disabled={!!busy}
              className="px-3 py-1 bg-red-600 hover:bg-red-700 disabled:opacity-60 text-white rounded-lg text-[11px] font-bold cursor-pointer">
              {busy === "disconnect" ? "Disconnecting..." : "Disconnect"}
            </button>
          </div>
        )}

        {editing === "quickbooks" && current.status === "connected" && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-center space-y-1.5">
            <div className="text-xs font-bold text-emerald-700 flex items-center justify-center gap-1.5">
              <CheckCircle2 className="h-3.5 w-3.5" /> Connected to QuickBooks
            </div>
            <div className="text-[11px] text-emerald-600">Company: {current.values.company_name || current.values.realm_id || "Company"}</div>
            <div className="text-[10px] text-slate-500">Realm ID: {current.values.realm_id} ({current.values.environment || "sandbox"})</div>
            <button onClick={disconnectQuickbooks} disabled={!!busy}
              className="px-3 py-1 bg-red-600 hover:bg-red-700 disabled:opacity-60 text-white rounded-lg text-[11px] font-bold cursor-pointer">
              {busy === "disconnect" ? "Disconnecting..." : "Disconnect"}
            </button>
          </div>
        )}

        {FIELDS[editing].map((f) => (
          <div key={f.key}>
            <label className="block text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">{f.label}</label>
            <input
              type={f.secret ? "password" : "text"}
              autoComplete="off"
              value={form[f.key] ?? ""}
              onChange={(e) => setForm((p) => ({ ...p, [f.key]: e.target.value }))}
              placeholder={f.secret && current.has_secret[f.key] ? "•••••••• (saved — leave empty to keep)" : f.placeholder}
              className="w-full bg-white border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs text-slate-800 focus:border-blue-500 focus:outline-none"
            />
          </div>
        ))}

        {editing === "xero" && current.status !== "connected" && (
          <>
            <div className="text-[10px] text-slate-500 leading-relaxed bg-white border border-slate-200 rounded-lg p-2.5">
              Register this redirect URI in your Xero app:
              <code className="block mt-1 break-all text-slate-700">http://localhost:8000/api/connections/xero/callback</code>
            </div>
            <button onClick={connectXero} disabled={!!busy}
              className="w-full flex items-center justify-center gap-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white rounded-lg py-2 text-xs font-bold cursor-pointer">
              <Link2 className="h-3.5 w-3.5" />
              {busy === "connect" ? "Opening Xero..." : "Connect Xero"}
            </button>
          </>
        )}

        {editing === "quickbooks" && current.status !== "connected" && (
          <>
            <div className="text-[10px] text-slate-500 leading-relaxed bg-white border border-slate-200 rounded-lg p-2.5">
              Register this redirect URI in your Intuit Developer App (Keys &amp; credentials):
              <code className="block mt-1 break-all text-slate-700">http://localhost:8000/api/connections/quickbooks/callback</code>
            </div>
            <button onClick={connectQuickbooks} disabled={!!busy}
              className="w-full flex items-center justify-center gap-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-60 text-white rounded-lg py-2 text-xs font-bold cursor-pointer">
              <Link2 className="h-3.5 w-3.5" />
              {busy === "connect" ? "Opening QuickBooks..." : "Connect QuickBooks"}
            </button>
          </>
        )}

        {result && (
          <div className={`flex items-start gap-2 text-[11px] font-semibold rounded-lg p-2.5 border ${
            result.ok ? "text-emerald-700 bg-emerald-50 border-emerald-200" : "text-red-700 bg-red-50 border-red-200"
          }`}>
            {result.ok ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 mt-px" /> : <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />}
            <span className="break-words min-w-0">{result.text}</span>
          </div>
        )}

        <button onClick={test} disabled={!!busy}
          className="w-full flex items-center justify-center gap-1.5 bg-slate-100 hover:bg-slate-200 disabled:opacity-60 border border-slate-200 text-slate-700 rounded-lg py-2 text-xs font-semibold cursor-pointer">
          {busy === "test" && <RefreshCw className="h-3 w-3 animate-spin" />}
          Test Connection
        </button>
        <button onClick={back} disabled={!!busy}
          className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white rounded-lg py-2 text-xs font-bold cursor-pointer">
          Back to Deployments
        </button>
        <button onClick={clearSaved} className="w-full text-[10px] text-slate-400 hover:text-red-600 cursor-pointer">
          Clear saved settings
        </button>
      </div>
    );
  }

  // ---------------------------------------------------------------- list view
  const get = (key: Connection["integration"]) => connections.find((c) => c.integration === key);

  const card = (key: string, icon: React.ReactNode, title: string, sub: string, badge: Badge, onClick: () => void) => (
    <button key={key} onClick={onClick}
      className="w-full text-left bg-white border border-slate-200 hover:border-blue-300 rounded-xl p-3 flex items-center justify-between gap-2 transition cursor-pointer">
      <div className="flex items-center gap-2.5 min-w-0">
        <div className="text-slate-400 shrink-0">{icon}</div>
        <div className="min-w-0">
          <div className="text-xs font-bold text-slate-800">{title}</div>
          <div className="text-[10px] text-slate-400 truncate">{sub}</div>
        </div>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        <span className={`text-[9px] font-bold px-2 py-0.5 rounded-full border ${badge.cls}`}>{badge.text}</span>
        <ChevronRight className="h-3.5 w-3.5 text-slate-300" />
      </div>
    </button>
  );

  const local = get("manager_local");
  const cloud = get("manager_cloud");
  const xero = get("xero");
  const quickbooks = get("quickbooks");

  const spBadge: Badge =
    sharepointStatus === "connected" ? { text: "Token Loaded", cls: GREEN }
    : sharepointStatus === "configured" ? { text: "Configured", cls: BLUE }
    : { text: "Not configured", cls: GREY };

  return (
    <div className="space-y-2.5">
      {notice && (
        <div className={`text-[11px] font-semibold rounded-lg p-2.5 border ${
          notice.kind === "ok" ? "text-emerald-700 bg-emerald-50 border-emerald-200" : "text-red-700 bg-red-50 border-red-200"
        }`}>{notice.text}</div>
      )}

      {card("sharepoint", <Building2 className="h-4 w-4" />, "SharePoint",
        sharepointSite ? `Site: ${sharepointSite}` : "Azure AD & document library", spBadge, onOpenSharePoint)}

      {local && card("manager_local", <Server className="h-4 w-4" />, "Manager.io (Local)",
        local.values.url ? `URL: ${local.values.url}` : "Click to set up",
        local.status === "not_configured" ? { text: "Not configured", cls: GREY } : { text: "Local Node", cls: BLUE },
        () => open(local))}

      {cloud && card("manager_cloud", <Cloud className="h-4 w-4" />, "Manager.io (Cloud)",
        cloud.values.url ? `URL: ${cloud.values.url}` : "Click to set up",
        cloud.status === "not_configured" ? { text: "Not configured", cls: GREY } : { text: "Configured", cls: GREEN },
        () => open(cloud))}

      {xero && card("xero", <ShieldCheck className="h-4 w-4" />, "Xero",
        xero.values.tenant_name ? `Org: ${xero.values.tenant_name}` : "Click to set up",
        xero.status === "connected" ? { text: "Connected", cls: GREEN }
          : xero.status === "configured" ? { text: "Configured", cls: BLUE }
          : { text: "Not configured", cls: GREY },
        () => open(xero))}

      {quickbooks && card("quickbooks", <Building2 className="h-4 w-4 text-emerald-600" />, "QuickBooks Online",
        quickbooks.values.company_name ? `Company: ${quickbooks.values.company_name}` : "Click to set up",
        quickbooks.status === "connected" ? { text: "Connected", cls: GREEN }
          : quickbooks.status === "configured" ? { text: "Configured", cls: BLUE }
          : { text: "Not configured", cls: GREY },
        () => open(quickbooks))}

      {connections.length === 0 && <p className="text-[11px] text-slate-400">Loading connections…</p>}
    </div>
  );
}
