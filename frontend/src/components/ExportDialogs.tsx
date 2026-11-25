"use client";

import React from "react";
import { RefreshCw, CheckCircle2, AlertCircle, ArrowRight } from "lucide-react";

export type Destination = "excel" | "sharepoint" | "manager_local" | "manager_cloud" | "xero" | "quickbooks";

export const DESTINATIONS: { value: Destination; label: string; button: string }[] = [
  { value: "excel", label: "Download Excel", button: "Download Excel" },
  { value: "sharepoint", label: "Send to SharePoint", button: "Send to SharePoint" },
  { value: "manager_local", label: "Send to Manager.io (Local)", button: "Send to Manager Local" },
  { value: "manager_cloud", label: "Send to Manager.io (Cloud)", button: "Send to Manager Cloud" },
  { value: "xero", label: "Send to Xero", button: "Send to Xero" },
  { value: "quickbooks", label: "Send to QuickBooks", button: "Send to QuickBooks" },
];

export interface ExportResult {
  destination: Destination;
  message: string;
  imported: number;
  skipped: number;
  failed: number;
  errors: string[];
  organization?: string | null;
  bank_account?: string | null;
  url?: string | null;
}

const NAMES: Record<string, string> = {
  manager_local: "Manager.io",
  manager_cloud: "Manager.io",
  xero: "Xero",
  quickbooks: "QuickBooks Online",
};

export function ExportLoading({ destination }: { destination: Destination | null }) {
  if (!destination) return null;
  const name = NAMES[destination] || "destination";
  return (
    <div className="fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-white rounded-3xl p-8 max-w-xs w-full text-center shadow-2xl flex flex-col items-center">
        <RefreshCw className="h-9 w-9 text-blue-600 animate-spin mb-4" />
        <h3 className="text-sm font-bold text-slate-900">{destination === "xero" ? "Routing to Xero" : `Uploading to ${name}`}</h3>
        <p className="text-xs text-slate-500 mt-1 leading-relaxed">
          Please wait while we establish a connection and post to {name}...
        </p>
      </div>
    </div>
  );
}

export function ExportResultDialog({ result, onClose }: { result: ExportResult | null; onClose: () => void }) {
  if (!result) return null;
  const name = NAMES[result.destination] || "destination";
  const allFailed = result.imported === 0 && result.failed > 0;
  const nothingNew = result.imported === 0 && result.failed === 0;

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-md flex items-center justify-center p-4">
      <div className="bg-white rounded-3xl max-w-md w-full p-8 shadow-2xl border border-slate-150">
        <div className="flex flex-col items-center text-center">
          <div className={`p-4 rounded-2xl mb-4 ${allFailed ? "bg-red-50 text-red-600" : "bg-blue-50 text-blue-600"}`}>
            {allFailed ? <AlertCircle className="h-9 w-9" /> : <CheckCircle2 className="h-9 w-9" />}
          </div>
          <h3 className="text-md font-bold text-slate-900">
            {allFailed ? `Export to ${name} Failed` : nothingNew ? "Nothing New to Export" : `Exported to ${name} Successfully`}
          </h3>
          <p className="text-xs text-slate-500 mt-2 max-w-xs leading-relaxed">
            {nothingNew
              ? "Every transaction in this statement was already sent to this destination."
              : `Your bank statement transactions were imported into your ${name} ${
                  result.destination === "xero"
                    ? "organisation"
                    : result.destination === "quickbooks"
                    ? "company"
                    : "business"
                }.`}
          </p>
        </div>

        <div className="mt-5 bg-slate-50 border border-slate-200 rounded-2xl p-4 space-y-3 text-left">
          {result.organization && (
            <div>
              <span className="text-[9px] font-bold text-slate-400 uppercase tracking-widest block">
                {result.destination === "xero"
                  ? "Xero Organization"
                  : result.destination === "quickbooks"
                  ? "QuickBooks Company"
                  : "Business"}
              </span>
              <span className="text-xs font-semibold text-slate-800 break-all">{result.organization}</span>
            </div>
          )}
          {result.bank_account && (
            <div>
              <span className="text-[9px] font-bold text-slate-400 uppercase tracking-widest block">Bank Account</span>
              <span className="text-xs font-semibold text-slate-800">{result.bank_account}</span>
            </div>
          )}
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-xl bg-emerald-50 border border-emerald-100 py-2">
              <div className="text-[9px] font-bold text-emerald-600 uppercase">Imported</div>
              <div className="text-lg font-bold text-emerald-700">{result.imported}</div>
            </div>
            <div className="rounded-xl bg-amber-50 border border-amber-100 py-2">
              <div className="text-[9px] font-bold text-amber-600 uppercase">Skipped</div>
              <div className="text-lg font-bold text-amber-700">{result.skipped}</div>
            </div>
            <div className="rounded-xl bg-red-50 border border-red-100 py-2">
              <div className="text-[9px] font-bold text-red-600 uppercase">Failed</div>
              <div className="text-lg font-bold text-red-700">{result.failed}</div>
            </div>
          </div>
          {result.errors.length > 0 && (
            <ul className="text-[10px] text-red-700 bg-red-50 border border-red-100 rounded-lg p-2 max-h-28 overflow-y-auto space-y-1 break-words">
              {result.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>

        <div className="mt-6 flex gap-3">
          {result.url && !allFailed && (
            <a href={result.url} target="_blank" rel="noopener noreferrer"
              className="flex-1 flex items-center justify-center gap-2 py-3 px-4 bg-purple-600 hover:bg-purple-700 text-white rounded-xl font-bold text-xs transition cursor-pointer">
              View in {name} <ArrowRight className="h-4 w-4" />
            </a>
          )}
          <button onClick={onClose}
            className="px-5 py-3 bg-slate-100 hover:bg-slate-200 border border-slate-200 text-slate-700 rounded-xl text-xs font-bold transition cursor-pointer">
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
