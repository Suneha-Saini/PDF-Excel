# Bootstrap PyTorch/EasyOCR DLL loading on Windows before other imports
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
try:
    import torch
except Exception:
    pass

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.database.connection import init_db
from app.services.queue import start_queue_worker, stop_queue_worker
from app.api.endpoints import auth, documents, extract, export, connections, ledger, settings as settings_endpoints
from app.api.endpoints import sync as sync_endpoints, webhooks, history as history_endpoints, accuracy as accuracy_endpoints
from app.api.endpoints import payee_rules as payee_rules_endpoints, client_settings as client_settings_endpoints
from app.api.endpoints import workflow as workflow_endpoints
from app.services.autosync import worker as sync_worker

# Configure logging style
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup Events
    logger.info("Initializing database schemas...")
    init_db()
    
    logger.info("Starting background execution queues...")
    start_queue_worker()
    sync_worker.start()

    yield

    # Shutdown Events
    logger.info("Stopping background queue threads...")
    sync_worker.stop()
    stop_queue_worker()
    logger.info("System successfully shut down.")

app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan,
    version="1.0.0"
)

# Set CORS origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def keep_private_behind_tunnels(request, call_next):
    """The app has no login. Through ngrok/cloudflared only the signed webhook paths may answer; all else is a 404."""
    from fastapi.responses import PlainTextResponse
    from app.security.public_guard import allowed_when_public, is_public_request
    if is_public_request(request.headers) and not allowed_when_public(request.url.path):
        return PlainTextResponse("Not Found", status_code=404)
    return await call_next(request)


# Route registrations
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(documents.router, prefix=f"{settings.API_V1_STR}/documents", tags=["Documents"])
app.include_router(extract.router, prefix=f"{settings.API_V1_STR}/extract", tags=["Extraction"])
app.include_router(export.router, prefix=f"{settings.API_V1_STR}/export", tags=["Export"])
app.include_router(connections.router, prefix=f"{settings.API_V1_STR}/connections", tags=["Connections"])
app.include_router(ledger.router, prefix=f"{settings.API_V1_STR}/ledger", tags=["Ledger Coding"])
app.include_router(settings_endpoints.router, prefix=f"{settings.API_V1_STR}/settings", tags=["Settings"])
app.include_router(sync_endpoints.router, prefix=f"{settings.API_V1_STR}/sync", tags=["Auto-sync"])
app.include_router(history_endpoints.router, prefix=f"{settings.API_V1_STR}/history", tags=["History import"])
app.include_router(accuracy_endpoints.router, prefix=f"{settings.API_V1_STR}/accuracy", tags=["Accuracy"])
app.include_router(payee_rules_endpoints.router, prefix=f"{settings.API_V1_STR}/payee-rules", tags=["Payee rules"])
app.include_router(client_settings_endpoints.router, prefix=f"{settings.API_V1_STR}/client-settings", tags=["Client settings"])
app.include_router(workflow_endpoints.router, prefix=f"{settings.API_V1_STR}/workflow", tags=["Client workflow"])
# Public (signature-verified, no login): Xero / QuickBooks call these.
app.include_router(webhooks.router, prefix=f"{settings.API_V1_STR}/webhooks", tags=["Webhooks"])

@app.get("/")
def read_root():
    return {
        "status": "online",
        "app": settings.PROJECT_NAME,
        "docs_url": "/docs"
    }
