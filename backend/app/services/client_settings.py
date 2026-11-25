"""Per-client facts (entity type, GST registration, BAS frequency).

'unknown' is a real, default value: nothing is assumed about a client until the accountant says so, and checks that
depend on a setting fall back to cautious behaviour (or are skipped) while it is unknown."""
import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.database.models import ClientSettings

CHOICES: Dict[str, List[Dict[str, str]]] = {
    "entity_type": [
        {"value": "unknown", "label": "Not set"}, {"value": "company", "label": "Company"},
        {"value": "sole_trader", "label": "Sole trader"}, {"value": "partnership", "label": "Partnership"},
        {"value": "trust", "label": "Trust"}, {"value": "smsf", "label": "SMSF"}, {"value": "individual", "label": "Individual"},
    ],
    "gst_registration": [
        {"value": "unknown", "label": "Not set"}, {"value": "not_registered", "label": "Not registered for GST"},
        {"value": "cash", "label": "Registered - cash basis"}, {"value": "accrual", "label": "Registered - accruals basis"},
    ],
    "bas_frequency": [
        {"value": "unknown", "label": "Not set"}, {"value": "monthly", "label": "Monthly"},
        {"value": "quarterly", "label": "Quarterly"}, {"value": "annual", "label": "Annually"},
    ],
}
FIELDS = tuple(CHOICES)

#: What each setting changes, shown next to the control so nobody has to guess.
EFFECTS = {
    "entity_type": "Adds entity-specific account checks (for example drawings on a company, or dividends on a sole trader).",
    "gst_registration": "Registered: GST checks run. Not registered: any GST code is flagged as High. Not set: guessed from the tax codes in use.",
    "bas_frequency": "Saved for the upcoming BAS review. It does not change any check yet.",
}


class InvalidSetting(ValueError):
    pass


def defaults() -> Dict[str, str]:
    return {f: "unknown" for f in FIELDS}


def get(db: Session, user_id, provider: str, client_id: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = defaults()
    if client_id:
        row = (db.query(ClientSettings).filter(ClientSettings.user_id == user_id, ClientSettings.provider == provider,
                                               ClientSettings.client_id == client_id).first())
        if row:
            out.update({f: getattr(row, f) for f in FIELDS})
            out["updated_at"] = row.updated_at.isoformat() + "Z"
    return out


def validate(values: Dict[str, Any]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for field, value in values.items():
        if field not in CHOICES:
            raise InvalidSetting(f"Unknown setting '{field}'.")
        if value not in {c["value"] for c in CHOICES[field]}:
            raise InvalidSetting(f"'{value}' is not a valid {field.replace('_', ' ')}.")
        clean[field] = value
    return clean


def save(db: Session, user_id, provider: str, client_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Update only the fields given. Caller commits."""
    clean = validate(values)
    row = (db.query(ClientSettings).filter(ClientSettings.user_id == user_id, ClientSettings.provider == provider,
                                           ClientSettings.client_id == client_id).first())
    if row is None:
        row = ClientSettings(user_id=user_id, provider=provider, client_id=client_id)
    for f, v in clean.items():
        setattr(row, f, v)
    row.updated_at = datetime.datetime.utcnow()
    db.add(row)
    return clean
