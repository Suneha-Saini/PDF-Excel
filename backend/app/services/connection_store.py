"""Storage/lookup of export-destination settings.

Precedence for every value:  saved in the app (DB)  >  .env default.
Secrets are Fernet-encrypted in the DB and never returned to the browser.
"""
import json
import datetime
from typing import Any, Dict, Optional

from sqlmodel import Session

from app.config import settings
from app.database.models import IntegrationConnection, User
from app.security.encryption import encrypt_value, decrypt_value

SPECS: Dict[str, Dict[str, Any]] = {
    "manager_local": {
        "label": "Manager.io (Local)",
        "env_prefix": "MANAGER_LOCAL",
        "public": ["url", "username", "business_id", "bank_account_id", "suspense_account_id"],
        "secret": ["secret"],
        "required": ["url", "secret"],
    },
    "manager_cloud": {
        "label": "Manager.io (Cloud)",
        "env_prefix": "MANAGER_CLOUD",
        "public": ["url", "username", "business_id", "bank_account_id", "suspense_account_id"],
        "secret": ["secret"],
        "required": ["url", "secret"],
    },
    "xero": {
        "label": "Xero",
        "env_prefix": "XERO",
        # tenant_id / tenant_name / expires_at are written by the OAuth callback.
        "public": ["tenant_id", "tenant_name", "default_account_code", "expires_at"],
        "secret": ["client_id", "client_secret", "access_token", "refresh_token"],
        "required": ["client_id", "client_secret"],
    },
    "quickbooks": {
        "label": "QuickBooks Online",
        "env_prefix": "QUICKBOOKS",
        # realm_id / company_name / expires_at are written by the OAuth callback.
        "public": ["realm_id", "company_name", "environment", "default_account_id", "expires_at"],
        "secret": ["client_id", "client_secret", "access_token", "refresh_token"],
        "required": ["client_id", "client_secret"],
    },
}


def _env_default(integration: str, key: str) -> Optional[str]:
    prefix = SPECS[integration]["env_prefix"]
    value = getattr(settings, f"{prefix}_{key.upper()}", None)
    return value or None


def get_row(db: Session, user: User, integration: str) -> Optional[IntegrationConnection]:
    return (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.user_id == user.id, IntegrationConnection.integration == integration)
        .first()
    )


def _decrypt_secrets(row: Optional[IntegrationConnection]) -> Dict[str, str]:
    if not row or not row.secrets_encrypted:
        return {}
    try:
        return json.loads(decrypt_value(row.secrets_encrypted))
    except Exception:
        return {}


def resolve(db: Session, user: User, integration: str) -> Dict[str, Any]:
    """Return the effective settings (env defaults overridden by saved values), secrets included."""
    spec = SPECS[integration]
    row = get_row(db, user, integration)
    saved_public = (row.config or {}) if row else {}
    saved_secret = _decrypt_secrets(row)

    out: Dict[str, Any] = {}
    for key in spec["public"]:
        out[key] = saved_public.get(key) or _env_default(integration, key)
    for key in spec["secret"]:
        out[key] = saved_secret.get(key) or _env_default(integration, key)
    return out


def is_configured(integration: str, values: Dict[str, Any]) -> bool:
    return all(values.get(k) for k in SPECS[integration]["required"])


def public_view(db: Session, user: User, integration: str) -> Dict[str, Any]:
    """What the browser may see: public values, plus which secrets exist (never their values)."""
    spec = SPECS[integration]
    values = resolve(db, user, integration)
    row = get_row(db, user, integration)

    if integration in ("xero", "quickbooks"):
        identifier_key = "tenant_id" if integration == "xero" else "realm_id"
        connected = bool(values.get("refresh_token") and values.get(identifier_key))
        status = "connected" if connected else ("configured" if is_configured(integration, values) else "not_configured")
    else:
        status = "configured" if is_configured(integration, values) else "not_configured"

    return {
        "integration": integration,
        "label": spec["label"],
        "status": status,
        "values": {k: values.get(k) for k in spec["public"]},
        "has_secret": {k: bool(values.get(k)) for k in spec["secret"]},
        "last_test_status": row.last_test_status if row else None,
        "last_test_message": row.last_test_message if row else None,
    }


def save(db: Session, user: User, integration: str, public: Dict[str, Any], secret: Dict[str, Any]) -> None:
    """Merge submitted values into the stored row. Empty/None values keep what is already saved."""
    spec = SPECS[integration]
    row = get_row(db, user, integration)
    if not row:
        row = IntegrationConnection(user_id=user.id, integration=integration, config={}, secrets_encrypted=None)

    cfg = dict(row.config or {})
    for key in spec["public"]:
        if key in public and public[key] is not None:
            cfg[key] = str(public[key]).strip()
    row.config = cfg

    secrets = _decrypt_secrets(row)
    for key in spec["secret"]:
        if secret.get(key):
            secrets[key] = secret[key]
    row.secrets_encrypted = encrypt_value(json.dumps(secrets)) if secrets else None

    row.updated_at = datetime.datetime.utcnow()
    db.add(row)
    db.commit()


def set_test_result(db: Session, user: User, integration: str, ok: bool, message: str) -> None:
    row = get_row(db, user, integration)
    if not row:
        row = IntegrationConnection(user_id=user.id, integration=integration, config={})
    row.last_test_status = "ok" if ok else "error"
    row.last_test_message = message[:500]
    row.updated_at = datetime.datetime.utcnow()
    db.add(row)
    db.commit()


def clear_keys(db: Session, user: User, integration: str, public_keys: list, secret_keys: list) -> None:
    """Remove specific saved values (e.g. Xero tokens on Disconnect) while keeping the rest."""
    row = get_row(db, user, integration)
    if not row:
        return
    row.config = {k: v for k, v in (row.config or {}).items() if k not in public_keys}
    secrets = _decrypt_secrets(row)
    for k in secret_keys:
        secrets.pop(k, None)
    row.secrets_encrypted = encrypt_value(json.dumps(secrets)) if secrets else None
    row.updated_at = datetime.datetime.utcnow()
    db.add(row)
    db.commit()


def delete(db: Session, user: User, integration: str) -> None:
    row = get_row(db, user, integration)
    if row:
        db.delete(row)
        db.commit()
