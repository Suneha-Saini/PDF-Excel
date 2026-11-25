import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, Relationship, JSON, Column

class User(SQLModel, table=True):
    __tablename__: str = "users"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    username: str = Field(unique=True, index=True, nullable=False)
    hashed_password: str = Field(nullable=False)
    role: str = Field(default="user")  # "admin" or "user"
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    documents: List["Document"] = Relationship(back_populates="user")
    settings: List["ProviderSetting"] = Relationship(back_populates="user")

class Document(SQLModel, table=True):
    __tablename__: str = "documents"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False)
    filename: str = Field(nullable=False)
    file_path: str = Field(nullable=False)
    file_hash: str = Field(nullable=False, index=True)
    mime_type: str = Field(nullable=False)
    status: str = Field(default="uploaded")  # "uploaded", "processing", "completed", "failed"
    ocr_engine: Optional[str] = Field(default=None)
    ai_provider: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    extracted_data: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    user: User = Relationship(back_populates="documents")

class ProviderSetting(SQLModel, table=True):
    __tablename__: str = "provider_settings"
    model_config = {"protected_namespaces": ()}
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False)
    provider_name: str = Field(nullable=False)  # "Ollama", "OpenAI", "Claude", etc.
    api_key_encrypted: Optional[str] = Field(default=None)
    base_url: Optional[str] = Field(default=None)
    model_name: Optional[str] = Field(default=None)
    additional_params: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    user: User = Relationship(back_populates="settings")

class IntegrationConnection(SQLModel, table=True):
    """Saved settings for an export destination (manager_local, manager_cloud, xero, quickbooks).

    Non-secret values live in `config`; secrets (passwords, API keys, OAuth tokens)
    are stored as one Fernet-encrypted JSON blob in `secrets_encrypted`.
    """
    __tablename__: str = "integration_connections"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    integration: str = Field(nullable=False, index=True)
    config: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    secrets_encrypted: Optional[str] = Field(default=None)
    last_test_status: Optional[str] = Field(default=None)  # "ok" | "error"
    last_test_message: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExportRecord(SQLModel, table=True):
    """One row per transaction already pushed to a destination, so re-exports skip duplicates."""
    __tablename__: str = "export_records"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    document_id: uuid.UUID = Field(nullable=False, index=True)
    destination: str = Field(nullable=False, index=True)
    tx_hash: str = Field(nullable=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CodingRule(SQLModel, table=True):
    """User-defined rule: if a ledger line matches, assign this Xero account code (rules beat AI)."""
    __tablename__: str = "coding_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    # Account codes differ per accounting system (and per client), so a rule belongs to one provider and,
    # optionally, one client of it. client_id NULL = applies to every client of that provider.
    provider: str = Field(default="xero")
    client_id: Optional[str] = Field(default=None)
    name: str = Field(nullable=False)
    keywords: str = Field(nullable=False)          # comma-separated, case-insensitive "contains"
    direction: str = Field(default="any")          # any | spend | receive
    account_code: str = Field(nullable=False)
    priority: int = Field(default=100)             # lower runs first
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LedgerRun(SQLModel, table=True):
    """One 'fetch ledger from Xero -> categorise -> review -> apply' session."""
    __tablename__: str = "ledger_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(default="xero")           # xero | quickbooks | manager_cloud | manager_local
    client_id: Optional[str] = Field(default=None)  # Xero tenantId / QuickBooks realmId / Manager business
    auto_sync: bool = Field(default=False)          # True = the run kept up to date by webhooks / the nightly sync
    organization: Optional[str] = Field(default=None)  # client display name
    date_from: str = Field(nullable=False)
    date_to: str = Field(nullable=False)
    ai_provider: Optional[str] = Field(default=None)
    # fetching | categorising | ready | applying | applied | failed
    status: str = Field(default="fetching")
    progress: Optional[str] = Field(default=None)
    error: Optional[str] = Field(default=None)
    items: Optional[List[Dict[str, Any]]] = Field(default=None, sa_column=Column(JSON))
    accounts: Optional[List[Dict[str, Any]]] = Field(default=None, sa_column=Column(JSON))
    txs: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    apply_results: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SyncTarget(SQLModel, table=True):
    """A client the user switched 'Auto-sync' on for. Webhooks and the nightly job only ever touch these, which
    keeps API calls (and rate/client limits) bounded to what the user chose."""
    __tablename__: str = "sync_targets"
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)       # xero | quickbooks | ...
    client_id: str = Field(nullable=False, index=True)      # Xero tenantId / QuickBooks realmId
    client_name: Optional[str] = Field(default=None)
    enabled: bool = Field(default=True)
    lookback_days: int = Field(default=120)                 # window kept in sync: today - N days .. today
    ai_provider: Optional[str] = Field(default=None)        # None = rules only (the default; no data leaves the machine)
    last_synced_at: Optional[datetime] = Field(default=None)
    last_trigger: Optional[str] = Field(default=None)       # webhook | nightly | manual | enable
    last_status: Optional[str] = Field(default=None)        # ok | error | needs_reconnect | rate_limited | deferred
    last_error: Optional[str] = Field(default=None)
    last_summary: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    backoff_until: Optional[datetime] = Field(default=None)  # don't retry before this (rate limit / broken auth)
    lease_until: Optional[datetime] = Field(default=None)    # a sync is running (guards against double runs)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HistoryImport(SQLModel, table=True):
    """A ledger export the user imported to teach the system how this client has always been coded.
    Kept as its own batch so it can be undone, and so it never appears as a ledger run on the Ledger page."""
    __tablename__: str = "history_imports"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: str = Field(nullable=False, index=True)
    client_name: Optional[str] = Field(default=None)
    filename: str = Field(nullable=False)
    layout: str = Field(default="grouped")                 # grouped | flat
    lines: int = Field(default=0)
    ignored: int = Field(default=0)
    date_from: Optional[str] = Field(default=None)
    date_to: Optional[str] = Field(default=None)
    mapping: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))   # what the user confirmed
    items: Optional[List[Dict[str, Any]]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SuggestionDecision(SQLModel, table=True):
    """What a person did with a suggestion. This is the ONLY source of accuracy figures: the system never marks its
    own work, so a line counts as 'checked' only when someone decided it individually.

    outcome: accepted | corrected                 a person confirmed / changed it        (counted)
             auto | auto_corrected                approved automatically / later changed (reported separately)
    how:     line | edit                          individual decision                    (counted)
             bulk                                 'Accept suggestions' for many at once  (NOT counted)
             apply                                went through Apply without a decision  (NOT counted)"""
    __tablename__: str = "suggestion_decisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: Optional[str] = Field(default=None, index=True)
    client_name: Optional[str] = Field(default=None)
    run_id: uuid.UUID = Field(nullable=False, index=True)
    item_id: str = Field(nullable=False, index=True)
    source: str = Field(nullable=False)                     # history | ai | rule
    level: Optional[str] = Field(default=None)              # history: high | medium | low
    confidence: Optional[float] = Field(default=None)       # ai: the model's own 0..1
    suggested_code: str = Field(nullable=False)
    suggested_name: Optional[str] = Field(default=None)
    final_code: Optional[str] = Field(default=None)         # None = the suggestion was cleared
    final_name: Optional[str] = Field(default=None)
    outcome: str = Field(nullable=False)
    how: str = Field(nullable=False)
    item_date: Optional[str] = Field(default=None)
    item_description: Optional[str] = Field(default=None)
    item_contact: Optional[str] = Field(default=None)
    item_amount: Optional[float] = Field(default=None)
    decided_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PayeeRule(SQLModel, table=True):
    """'This payee, in this direction, is coded to this account' for one client. Learned from a reviewer's corrections
    ('Remember'), from consistent history the user approved, or entered by hand. It is the most specific signal the
    system has, so it is applied before keyword rules, history and AI. Times / last-seen are computed from history,
    never stored, so they cannot drift."""
    __tablename__: str = "payee_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: str = Field(nullable=False, index=True)
    payee_key: str = Field(nullable=False, index=True)      # "c:<contact>" or "d:<description tokens>"
    payee_display: str = Field(default="")
    direction: str = Field(nullable=False)                  # spend | receive
    account_code: str = Field(nullable=False)
    account_name: Optional[str] = Field(default=None)
    tax_label: Optional[str] = Field(default=None)          # shown, not applied (tax is not written back)
    abn: Optional[str] = Field(default=None)                # best effort, from the platform's supplier list
    source: str = Field(default="remembered")               # remembered | history | manual
    batch_id: str = Field(default="", index=True)           # one batch = one Undo
    overrides: int = Field(default=0)                       # times a person changed a line this rule coded
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClientSettings(SQLModel, table=True):
    """Facts about a client that change what counts as a problem. Set by the accountant, never guessed."""
    __tablename__: str = "client_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: str = Field(nullable=False, index=True)
    entity_type: str = Field(default="unknown")           # company | sole_trader | partnership | trust | smsf | individual | unknown
    gst_registration: str = Field(default="unknown")      # not_registered | cash | accrual | unknown
    bas_frequency: str = Field(default="unknown")         # monthly | quarterly | annual | unknown (kept for the BAS review step)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClientWorkflow(SQLModel, table=True):
    """One review of one client for one period (a BAS period): files -> coding check -> GL review -> BAS review -> sign-off.
    Stage results are computed from the data; only what a person decides is stored here."""
    __tablename__: str = "client_workflows"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: str = Field(nullable=False, index=True)
    client_name: Optional[str] = Field(default=None)
    kind: str = Field(default="bas")
    period_start: str = Field(nullable=False)
    period_end: str = Field(nullable=False)
    label: str = Field(default="")
    due_date: Optional[str] = Field(default=None)            # computed default, editable (tax-agent lodgment dates differ)
    status: str = Field(default="not_started")               # not_started | in_review | signed_off
    assigned_to: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    checklist: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))     # pack items a person confirms
    bas_figures: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))   # G1 / 1A / 1B from the activity statement
    checks: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))        # when GL / BAS review last ran
    signed_off_by: Optional[str] = Field(default=None)
    signed_off_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WorkflowFinding(SQLModel, table=True):
    """A problem the GL or BAS review found. Its status survives re-runs: the key is stable, so a finding you accepted
    stays accepted, and one that is no longer produced (fixed in the books) is marked fixed."""
    __tablename__: str = "workflow_findings"

    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_id: uuid.UUID = Field(nullable=False, index=True)
    key: str = Field(nullable=False, index=True)
    stage: str = Field(nullable=False)                        # gl | bas
    severity: str = Field(nullable=False)                     # High | Medium | Info
    kind: str = Field(default="")
    text: str = Field(default="")
    detail: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    status: str = Field(default="open")                       # open | resolved | accepted | fixed
    note: Optional[str] = Field(default=None)
    seen: bool = Field(default=True)                          # produced by the latest check
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = Field(default=None)


class CodingPolicy(SQLModel, table=True):
    """Per-client switch for the shadow-period override (see services/accuracy.policy_status)."""
    __tablename__: str = "coding_policies"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    provider: str = Field(nullable=False, index=True)
    client_id: str = Field(nullable=False, index=True)
    auto_override: bool = Field(default=False)              # allow auto-approval now, skipping the shadow period
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WebhookEvent(SQLModel, table=True):
    """Inbox of webhook deliveries. Platforms retry and batch, so dedupe_key makes handling idempotent."""
    __tablename__: str = "webhook_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(nullable=False, index=True)
    dedupe_key: str = Field(nullable=False, unique=True, index=True)
    client_id: Optional[str] = Field(default=None, index=True)
    detail: str = Field(default="")
    matched_targets: int = Field(default=0)
    received_at: datetime = Field(default_factory=datetime.utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__: str = "audit_logs"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", nullable=True)
    action: str = Field(nullable=False)  # "login", "upload", "export", etc.
    details: str = Field(nullable=False)
    ip_address: Optional[str] = Field(default=None)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
