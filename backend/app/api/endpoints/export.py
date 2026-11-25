from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session

from app.database.connection import get_db
from app.database.models import User, Document, AuditLog, ExportRecord
from app.api import schemas
from app.security import auth
from app.excel.generator import generate_bank_excel
from app.services.sharepoint_service import SharePointService
from app.services import connection_store as store
from app.services.ledger import build_rows, bank_account_label
from app.services.manager_service import ManagerService, ManagerError
from app.services.xero_service import XeroError
from app.services.quickbooks_service import QuickBooksError
from app.api.endpoints.connections import xero_for, quickbooks_for

router = APIRouter()

DESTINATIONS = {"excel", "sharepoint", "manager_local", "manager_cloud", "xero", "quickbooks"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _audit(db: Session, user: User, action: str, details: str) -> None:
    db.add(AuditLog(user_id=user.id, action=action, details=details))
    db.commit()


def _export_sharepoint(payload: schemas.ExportRequest, doc: Document, db: Session, user: User):
    excel_bytes = generate_bank_excel(payload.edited_data)
    safe_name = doc.filename.rsplit(".", 1)[0] + ".xlsx"
    sp_service = SharePointService(
        tenant_id=payload.sharepoint_tenant_id,
        client_id=payload.sharepoint_client_id,
        client_secret=payload.sharepoint_client_secret,
        site_url=payload.sharepoint_site_url,
        library_name=payload.sharepoint_library_name,
        base_folder=payload.sharepoint_base_folder,
        create_year_folder=payload.sharepoint_create_year_folder,
        create_month_folder=payload.sharepoint_create_month_folder,
    )
    sp_url = sp_service.upload_excel(safe_name, excel_bytes, provided_token=payload.sharepoint_token)
    _audit(db, user, "export_excel_sharepoint", f"Exported '{doc.filename}' transactions spreadsheet directly to SharePoint")
    return JSONResponse(status_code=200, content={
        "destination": "sharepoint",
        "message": "File successfully uploaded to SharePoint.",
        "filename": safe_name,
        "sharepoint_url": sp_url,
        "url": sp_url,
    })


def _export_accounting(payload: schemas.ExportRequest, doc: Document, db: Session, user: User):
    dest = payload.destination
    ledger = payload.edited_data
    rows, skipped = build_rows(ledger)

    already = {
        r.tx_hash for r in db.query(ExportRecord).filter(
            ExportRecord.user_id == user.id, ExportRecord.destination == dest
        ).all()
    }
    fresh = [r for r in rows if r.tx_hash not in already]
    duplicates = len(rows) - len(fresh)
    if not fresh:
        return JSONResponse(status_code=200, content={
            "destination": dest, "message": "Nothing new to export.",
            "imported": 0, "skipped": len(skipped) + duplicates, "failed": 0, "errors": [],
            "bank_account": bank_account_label(ledger), "organization": None, "url": None,
        })

    if dest == "xero":
        result = xero_for(db, user).export_rows(ledger, fresh)
    elif dest == "quickbooks":
        result = quickbooks_for(db, user).export_rows(ledger, fresh)
    else:
        values = store.resolve(db, user, dest)
        if not store.is_configured(dest, values):
            raise HTTPException(status_code=400, detail=f"{store.SPECS[dest]['label']} is not configured. Open Settings > Deployment.")
        result = ManagerService(values).export_rows(fresh, bank_account_label(ledger))

    for tx_hash in result.pop("imported_hashes"):
        db.add(ExportRecord(user_id=user.id, document_id=doc.id, destination=dest, tx_hash=tx_hash))
    db.commit()

    imported = len(fresh) - result["failed"]
    _audit(db, user, f"export_{dest}", f"Exported {imported} transactions from '{doc.filename}' to {store.SPECS[dest]['label']}")
    return JSONResponse(status_code=200, content={
        "destination": dest,
        "message": f"Exported to {store.SPECS[dest]['label']}.",
        "imported": imported,
        "skipped": len(skipped) + duplicates,
        "failed": result["failed"],
        "errors": result["errors"],
        "organization": result.get("organization") or result.get("business"),
        "bank_account": result.get("bank_account"),
        "url": result.get("url"),
    })


@router.post("/")
def export_document(
    payload: schemas.ExportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    """Send the reviewed statement to the chosen destination.

    destination: excel (download) | sharepoint | manager_local | manager_cloud | xero
    """
    if payload.destination not in DESTINATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown destination '{payload.destination}'.")

    doc = db.query(Document).filter(
        Document.id == payload.document_id, Document.user_id == current_user.id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    try:
        if payload.destination == "excel":
            excel_bytes = generate_bank_excel(payload.edited_data)
            filename = doc.filename.rsplit(".", 1)[0] + ".xlsx"
            _audit(db, current_user, "export_excel_download", f"Downloaded Excel for '{doc.filename}'")
            return Response(
                content=excel_bytes, media_type=XLSX_MIME,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        if payload.destination == "sharepoint":
            return _export_sharepoint(payload, doc, db, current_user)
        return _export_accounting(payload, doc, db, current_user)
    except HTTPException:
        raise
    except (ManagerError, XeroError, QuickBooksError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{payload.destination} export failed: {exc}")
