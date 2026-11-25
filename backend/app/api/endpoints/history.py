"""Import a ledger export to teach the system how a client has always been coded.

Two steps so nothing is stored blind:
  POST /preview  detect the layout + columns, propose which chart account each section means. Stores nothing.
  POST /import   same file + the mapping the user confirmed -> one undoable batch of history lines.
The file is sent with both calls (no server-side temp state to expire or leak)."""
import datetime
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app.config import settings
from app.database.connection import get_db
from app.database.models import AuditLog, HistoryImport, User
from app.security import auth
from app.services import providers as prov
from app.services.coding import ledger_import as li
from app.services.coding.store import load_history
from app.services.providers.base import ProviderError

router = APIRouter()


# ------------------------------------------------------------------ helpers
def _context(db: Session, user: User, provider: str, client_id: str):
    """The system, the client (validated: it must be one this login can access) and its chart of accounts."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if provider not in overview:
        raise HTTPException(status_code=400, detail="Unknown accounting system.")
    if not overview[provider]["ready"]:
        raise HTTPException(status_code=400, detail=f"{overview[provider]['label']} is not connected.")
    system = prov.provider_for(provider, db, user)
    try:
        client = next((c for c in system.list_clients() if c["id"] == client_id), None)
        if client is None:
            raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
        return system, client, system.list_accounts(client_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _read(file: UploadFile) -> tuple:
    data = await file.read()
    if len(data) > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File is larger than {settings.MAX_FILE_SIZE_MB} MB.")
    try:
        return file.filename or "export", li.read_rows(file.filename or "export", data)
    except li.ImportFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - corrupt/encrypted workbook etc.
        raise HTTPException(status_code=400, detail=f"Could not read this file: {type(exc).__name__}: {str(exc)[:160]}")


def _json(text: str, what: str) -> Dict[str, Any]:
    try:
        v = json.loads(text) if text else {}
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{what} is not valid JSON.")
    if not isinstance(v, dict):
        raise HTTPException(status_code=400, detail=f"{what} must be an object.")
    return v


def infer_dayfirst(rows: List[List[Any]], header_idx: int, col: int) -> Optional[bool]:
    """Slash dates are ambiguous (03/04/2026). A value like 25/03 or 03/25 settles it for the whole file."""
    for row in rows[header_idx + 1:header_idx + 4000]:
        v = row[col] if col < len(row) else None
        m = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}\s*$", str(v)) if isinstance(v, str) else None
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return True
            if b > 12:
                return False
    return None


def _has_slash_dates(rows: List[List[Any]], header_idx: int, col: int) -> bool:
    return any(isinstance(r[col], str) and re.match(r"^\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*$", r[col])
               for r in rows[header_idx + 1:header_idx + 200] if col < len(r))


def _analyse(rows: List[List[Any]], mapping: Dict[str, Any]) -> Dict[str, Any]:
    """Layout + columns + parsed lines for a mapping (auto-detected unless the user overrode it)."""
    header_idx = mapping.get("header_row")
    if header_idx is None:
        found = li.detect_columns(rows)
        if found is None:
            return {"detected": False, "preview_rows": [[str(c) if c is not None else "" for c in r[:12]] for r in rows[:15]]}
        header_idx, cols = found
    else:
        if not (0 <= header_idx < len(rows)):
            raise HTTPException(status_code=400, detail="header_row is outside the file.")
        cols = li.guess_columns(rows[header_idx])
    for field, idx in (mapping.get("columns") or {}).items():
        if field not in li.FIELD_SYNONYMS:
            raise HTTPException(status_code=400, detail=f"Unknown column field '{field}'.")
        if idx is None:
            cols.pop(field, None)
        else:
            cols[field] = int(idx)

    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    valid = "date" in cols and ("amount" in cols or "debit" in cols or "credit" in cols)
    inferred = infer_dayfirst(rows, header_idx, cols["date"]) if "date" in cols else None
    dayfirst = mapping.get("dayfirst")
    dayfirst = (True if inferred is None else inferred) if dayfirst is None else bool(dayfirst)
    sign = mapping.get("amount_sign") or "as_is"
    if sign not in ("as_is", "flip", "by_account_class"):
        raise HTTPException(status_code=400, detail="amount_sign must be as_is, flip or by_account_class.")
    out: Dict[str, Any] = {"detected": True, "valid": valid, "header_row": header_idx, "headers": headers,
                           "columns": cols, "amount_sign": sign, "dayfirst": dayfirst,
                           "dayfirst_inferred": inferred, "rows_total": len(rows)}
    if valid:
        out["parsed"] = li.parse_rows(rows, header_idx, cols, out["amount_sign"], dayfirst)
    return out


# ------------------------------------------------------------------ preview
@router.post("/preview")
async def preview(file: UploadFile = File(...), provider: str = Form(...), client_id: str = Form(...), mapping: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    _, client, chart = _context(db, user, provider, client_id)
    filename, rows = await _read(file)
    a = _analyse(rows, _json(mapping, "mapping"))
    if not a["detected"]:
        return {**a, "message": "Could not find a header row with a Date column and Amount (or Debit/Credit) columns. "
                                "Pick the header row and columns below."}
    if not a["valid"]:
        return {**a, "message": "Choose the Date column and either an Amount column or Debit/Credit columns."}

    parsed = a.pop("parsed")
    lines = parsed["lines"]
    codes_in_file: Dict[str, str] = {}
    for ln in lines:
        if ln["code_in_file"] and ln["account"] not in codes_in_file:
            codes_in_file[ln["account"]] = ln["code_in_file"]
    suggestions = li.suggest_sections(list(parsed["sections"]), chart, codes_in_file)

    # Preview what the amounts will look like once each section's proposed account is known.
    class_by_code = {str(c["code"]): c.get("class") for c in chart}
    proposed = {n: s["code"] for n, s in suggestions.items()}
    hint = None
    if "amount" in a["columns"] and "debit" not in a["columns"] and "credit" not in a["columns"]:
        if a["amount_sign"] == "by_account_class":
            lines = li.apply_sign_by_class(lines, proposed, class_by_code)
        elif a["amount_sign"] == "as_is":
            hint = li.sign_hint(lines, proposed, class_by_code)

    dates = sorted(ln["date"] for ln in lines)
    warnings: List[str] = []
    if not lines:
        warnings.append("No transaction rows were found with the current mapping.")
    if "contact" not in a["columns"]:
        warnings.append("No payee/name column: matching will rely on the description only.")
    if dates and dates[-1] > datetime.date.today().isoformat():
        warnings.append(f"Some dates are in the future (latest {dates[-1]}). Check the day/month order.")
    if a["dayfirst_inferred"] is None and _has_slash_dates(rows, a["header_row"], a["columns"]["date"]):
        warnings.append("Dates like 03/04/2026 are ambiguous in this file; confirm the day/month order below.")
    unmapped = [n for n, s in suggestions.items() if not s["code"] and not s["ignore"]]
    if unmapped:
        warnings.append(f"{len(unmapped)} account name(s) did not match your chart: choose an account for each, or ignore it.")

    return {
        **a, "client": client["name"], "filename": filename, "layout": "grouped" if parsed["grouped"] else "flat",
        "lines": len(lines), "date_from": dates[0] if dates else None, "date_to": dates[-1] if dates else None,
        "distinct_contacts": len({ln["contact"].lower() for ln in lines if ln["contact"]}),
        "skipped": parsed["skipped"],
        "sections": [{"name": n, "lines": c, **suggestions[n]} for n, c in sorted(parsed["sections"].items(), key=lambda kv: -kv[1])],
        "sample": lines[:6], "warnings": warnings, "sign_hint": hint,
    }


# ------------------------------------------------------------------ import
@router.post("/import")
async def import_history(file: UploadFile = File(...), provider: str = Form(...), client_id: str = Form(...),
                         mapping: str = Form(""), section_map: str = Form(...),
                         db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    _, client, chart = _context(db, user, provider, client_id)
    filename, rows = await _read(file)
    m = _json(mapping, "mapping")
    a = _analyse(rows, m)
    if not a.get("valid"):
        raise HTTPException(status_code=400, detail="The column mapping is incomplete: choose Date and Amount (or Debit/Credit).")

    smap = _json(section_map, "section_map")
    known = {str(c["code"]) for c in chart}
    bad = sorted({str(v) for v in smap.values() if v and str(v) not in known})
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown account code(s) for this client: {', '.join(bad[:5])}.")

    parsed = a["parsed"]
    section_map = {k: (str(v) if v else None) for k, v in smap.items()}
    lines = parsed["lines"]
    if a["amount_sign"] == "by_account_class" and "debit" not in a["columns"] and "credit" not in a["columns"]:
        lines = li.apply_sign_by_class(lines, section_map, {str(c["code"]): c.get("class") for c in chart})
    items, stats = li.build_items(lines, section_map)
    if not items:
        raise HTTPException(status_code=400, detail="Nothing to import: no section was mapped to an account.")

    dates = sorted(i["date"] for i in items)
    batch = HistoryImport(
        user_id=user.id, provider=provider, client_id=client_id, client_name=client["name"], filename=filename[:200],
        layout="grouped" if parsed["grouped"] else "flat", lines=len(items), ignored=stats.get("ignored_lines", 0),
        date_from=dates[0], date_to=dates[-1], items=items,
        mapping={"header_row": a["header_row"], "columns": a["columns"], "amount_sign": a["amount_sign"],
                 "dayfirst": a["dayfirst"], "sections": {k: v for k, v in smap.items()}})
    db.add(batch)
    db.add(AuditLog(user_id=user.id, action="history_import",
                    details=f"Imported {len(items)} history lines for {provider}/{client['name']} from {filename[:80]}"))
    db.commit()
    db.refresh(batch)

    total = len(load_history(db, user.id, provider, client_id, client["name"]))
    return {"id": str(batch.id), "imported": len(items), "ignored": stats.get("ignored_lines", 0),
            "date_from": dates[0], "date_to": dates[-1],
            "distinct_contacts": len({i["contact"].lower() for i in items if i["contact"]}), "history_lines_total": total}


# ------------------------------------------------------------------ list / undo
@router.get("")
def list_imports(provider: str, client_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    rows = (db.query(HistoryImport).filter(HistoryImport.user_id == user.id, HistoryImport.provider == provider,
                                           HistoryImport.client_id == client_id).order_by(HistoryImport.created_at.desc()).all())
    name = rows[0].client_name if rows else None
    return {
        "imports": [{"id": str(r.id), "filename": r.filename, "layout": r.layout, "lines": r.lines, "ignored": r.ignored,
                     "date_from": r.date_from, "date_to": r.date_to, "created_at": r.created_at.isoformat() + "Z"} for r in rows],
        "history_lines_total": len(load_history(db, user.id, provider, client_id, name)),
    }


@router.delete("/{import_id}")
def undo_import(import_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    try:
        batch = db.get(HistoryImport, uuid.UUID(import_id))
    except ValueError:
        batch = None
    if not batch or batch.user_id != user.id:
        raise HTTPException(status_code=404, detail="Import not found.")
    db.add(AuditLog(user_id=user.id, action="history_import_undo", details=f"Removed {batch.lines} imported lines ({batch.filename[:80]})"))
    db.delete(batch)
    db.commit()
    return {"deleted": import_id}
