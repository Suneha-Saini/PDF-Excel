# Export destinations

The review screen has a destination dropdown: **Download Excel, SharePoint, Manager.io (Local), Manager.io (Cloud), Xero.**
Settings (cog) > **Deployment** holds the connection cards with Test Connection.

Values can come from `backend/.env` (defaults) or be saved in the app (saved values win; secrets are stored encrypted).

## Setup

1. Fill the values you need in `backend/.env` (empty = integration skipped).
2. Backend: `cd backend && pip install -r requirements.txt && python run.py` (port 8000).
3. Frontend: `cd frontend && npm install && npm run dev` (port 3000).

### Xero
1. Create an app at developer.xero.com (type: web app, auth code flow).
2. Add redirect URI `http://localhost:8000/api/connections/xero/callback` (must equal `XERO_REDIRECT_URI`).
3. Put Client ID / Secret in `.env` (or the Xero card), then click **Connect Xero** and tick your organisation.
4. Bank transactions are created as SPEND (debit) / RECEIVE (credit) against a bank account found or created from the
   statement, with contact "Suspense Contact". Set `XERO_DEFAULT_ACCOUNT_CODE` to choose the category account.

### Manager.io (Local / Cloud)
Fill URL, API key (or username + password), Target Bank Account ID, optional Suspense Account ID.
NOTE: the Manager.io API v2 write payloads are unverified - see the "API MAPPING" block at the top of
`backend/app/services/manager_service.py`. Use Test Connection, then export one small statement and adjust the mapping
if Manager returns errors (the dialog lists them).

### Behaviour
- Rows without a debit/credit amount (e.g. CLOSING BALANCE) are skipped. The OPENING BALANCE row is imported.
- Already-exported transactions are remembered per destination and skipped on re-export.

## Webhooks, auto-sync and the keys they need

Xero and QuickBooks **call this app** when something changes, so they need a public **https** address that reaches the
backend (port 8000). While developing, use a tunnel (for example `ngrok http 8000` or `cloudflared tunnel --url http://localhost:8000`).
The two receivers are:

| Platform | Delivery URL to register | Signature secret (put it in `backend/.env`) | Where to get it |
|---|---|---|---|
| Xero | `https://<tunnel>/api/webhooks/xero` | `XERO_WEBHOOK_KEY` | developer.xero.com/app/manage > your app > **Webhooks** > add the URL; the **Webhook key** is shown there |
| QuickBooks | `https://<tunnel>/api/webhooks/quickbooks` | `QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN` | developer.intuit.com > Dashboard > your app > **Webhooks** (Development or Production) > **Verifier token** |

After saving `.env`, restart the backend. Xero sends a verification request when you save the URL; it only succeeds once the key
is in `.env` and the backend is running. Check delivery at `GET /api/sync/status` (recent events and whether each key is set).

- Xero webhooks do **not** cover bank transactions, so bank lines sync nightly or with **Sync now**.
- Manager.io has no webhooks: it is covered by the nightly sync only (once its ledger is mapped).
- Without the keys everything still works; the page just says "nightly sync only".

Optional tuning (defaults shown): `SYNC_ENABLED=true`, `SYNC_NIGHTLY_HOUR=2` (local hour after which a stale client is re-synced),
`SYNC_MAX_AGE_HOURS=24`, `SYNC_WEBHOOK_MIN_GAP_SECONDS=30` (bursts of webhook events become one sync).

### Other settings
- `SECRET_KEY`: signs the short-lived sign-in state during Xero/QuickBooks connect. Leave the generated value; never share it.
- Xero / QuickBooks client ID and secret can be saved in the app (Settings > Deployment); saved values win over `.env`.
- Manager.io: `MANAGER_CLOUD_URL` (your business address), `MANAGER_CLOUD_SECRET` (API access token: in Manager, Settings > **Access Tokens**,
  see manager.io/guides/access-tokens), optional `MANAGER_CLOUD_BUSINESS_ID`. Local server uses the `MANAGER_LOCAL_*` equivalents.
- AI provider keys (Gemini, Groq, OpenAI...) are saved in the app, not in `.env`.

### Tunnel to the BACKEND (port 8000), not the frontend
`ngrok http 8000` (or `ngrok http 8000 --url <your-reserved-domain>`). Forwarding port 3000 only serves the web page and never reaches
the webhook receiver. The app has no login, so it refuses to serve anything through a tunnel except `/api/webhooks/*`
(everything else returns 404; see `backend/app/security/public_guard.py`). Register the full path, for example
`https://<your-domain>.ngrok-free.dev/api/webhooks/quickbooks`. Keep "Enable cloud event payload format" OFF in the Intuit portal.
