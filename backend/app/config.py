import os
from dotenv import load_dotenv
root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
backend_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(root_env):
    load_dotenv(root_env)
else:
    load_dotenv(backend_env)
from pydantic_settings import BaseSettings
from typing import List, Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Local AI Bank Statement Converter"
    API_V1_STR: str = "/api"
    
    # Security
    # In production, change this secret key
    SECRET_KEY: str = os.getenv("SECRET_KEY") or "super-secret-key-for-local-ai-bank-converter-1234567890"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days for ease of local desktop use
    
    # Storage
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "./uploads")
    TEMP_DIR: str = os.getenv("TEMP_DIR", "./temp")
    MAX_FILE_SIZE_MB: int = 150  # Handles large PDF bank statements
    ALLOWED_EXTENSIONS: List[str] = [
        "pdf", "jpg", "jpeg", "png", "tiff", "bmp",      # OCR / native text
        "xlsx", "xlsm", "xls", "csv", "tsv", "txt", "docx",  # read directly, no OCR
    ]
    
    # CORS
    BACKEND_CORS_ORIGINS: List[str] = ["*"]
    
    # OCR Settings
    TESSERACT_CMD: Optional[str] = os.getenv("TESSERACT_CMD", None)  # path to tesseract.exe on Windows if needed
    
    # Ollama settings
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    
    # SharePoint Integration
    AZURE_TENANT_ID: Optional[str] = os.getenv("AZURE_TENANT_ID")
    AZURE_CLIENT_ID: Optional[str] = os.getenv("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET: Optional[str] = os.getenv("AZURE_CLIENT_SECRET")
    AZURE_USERNAME: Optional[str] = os.getenv("AZURE_USERNAME")
    AZURE_PASSWORD: Optional[str] = os.getenv("AZURE_PASSWORD")
    GRAPH_SCOPE: str = os.getenv("GRAPH_SCOPE", "Sites.ReadWrite.All Files.ReadWrite.All")
    SP_SITE_URL: Optional[str] = os.getenv("SP_SITE_URL")
    SP_LIBRARY_NAME: str = os.getenv("SP_LIBRARY_NAME", "Documents")
    SP_BASE_FOLDER: str = os.getenv("SP_BASE_FOLDER", "ProcessedDocuments")
    SP_CREATE_YEAR_FOLDER: bool = str(os.getenv("SP_CREATE_YEAR_FOLDER", "true")).lower() == "true"
    SP_CREATE_MONTH_FOLDER: bool = str(os.getenv("SP_CREATE_MONTH_FOLDER", "true")).lower() == "true"
    
    # Manager.io (Local server / Desktop)
    MANAGER_LOCAL_URL: Optional[str] = os.getenv("MANAGER_LOCAL_URL")
    MANAGER_LOCAL_USERNAME: Optional[str] = os.getenv("MANAGER_LOCAL_USERNAME")
    MANAGER_LOCAL_SECRET: Optional[str] = os.getenv("MANAGER_LOCAL_SECRET")
    MANAGER_LOCAL_BUSINESS_ID: Optional[str] = os.getenv("MANAGER_LOCAL_BUSINESS_ID")
    MANAGER_LOCAL_BANK_ACCOUNT_ID: Optional[str] = os.getenv("MANAGER_LOCAL_BANK_ACCOUNT_ID")
    MANAGER_LOCAL_SUSPENSE_ACCOUNT_ID: Optional[str] = os.getenv("MANAGER_LOCAL_SUSPENSE_ACCOUNT_ID")

    # Manager.io (Cloud)
    MANAGER_CLOUD_URL: Optional[str] = os.getenv("MANAGER_CLOUD_URL")
    MANAGER_CLOUD_USERNAME: Optional[str] = os.getenv("MANAGER_CLOUD_USERNAME")
    MANAGER_CLOUD_SECRET: Optional[str] = os.getenv("MANAGER_CLOUD_SECRET")
    MANAGER_CLOUD_BUSINESS_ID: Optional[str] = os.getenv("MANAGER_CLOUD_BUSINESS_ID")
    MANAGER_CLOUD_BANK_ACCOUNT_ID: Optional[str] = os.getenv("MANAGER_CLOUD_BANK_ACCOUNT_ID")
    MANAGER_CLOUD_SUSPENSE_ACCOUNT_ID: Optional[str] = os.getenv("MANAGER_CLOUD_SUSPENSE_ACCOUNT_ID")

    # Xero (OAuth2 authorization-code flow)
    XERO_CLIENT_ID: Optional[str] = os.getenv("XERO_CLIENT_ID")
    XERO_CLIENT_SECRET: Optional[str] = os.getenv("XERO_CLIENT_SECRET")
    XERO_REDIRECT_URI: str = os.getenv("XERO_REDIRECT_URI") or "http://localhost:8000/api/connections/xero/callback"
    XERO_SCOPES: str = os.getenv("XERO_SCOPES") or "offline_access accounting.banktransactions accounting.contacts accounting.settings"
    XERO_DEFAULT_ACCOUNT_CODE: Optional[str] = os.getenv("XERO_DEFAULT_ACCOUNT_CODE")
    XERO_SUSPENSE_CONTACT_NAME: str = os.getenv("XERO_SUSPENSE_CONTACT_NAME") or "Suspense Contact"

    # QuickBooks Online (OAuth2 authorization-code flow)
    QUICKBOOKS_CLIENT_ID: Optional[str] = os.getenv("QUICKBOOKS_CLIENT_ID")
    QUICKBOOKS_CLIENT_SECRET: Optional[str] = os.getenv("QUICKBOOKS_CLIENT_SECRET")
    QUICKBOOKS_REDIRECT_URI: str = os.getenv("QUICKBOOKS_REDIRECT_URI") or "http://localhost:8000/api/connections/quickbooks/callback"
    QUICKBOOKS_ENVIRONMENT: str = os.getenv("QUICKBOOKS_ENVIRONMENT") or "sandbox"
    QUICKBOOKS_SCOPES: str = os.getenv("QUICKBOOKS_SCOPES") or "com.intuit.quickbooks.accounting"
    QUICKBOOKS_DEFAULT_ACCOUNT_ID: Optional[str] = os.getenv("QUICKBOOKS_DEFAULT_ACCOUNT_ID")
    QUICKBOOKS_SUSPENSE_ACCOUNT_NAME: str = os.getenv("QUICKBOOKS_SUSPENSE_ACCOUNT_NAME") or "Suspense"

    # Webhooks: the platforms call us, so these must be reachable at a public URL (tunnel while developing).
    # Xero: developer portal > app > Webhooks > signing key.  Intuit: developer portal > Webhooks > verifier token.
    XERO_WEBHOOK_KEY: Optional[str] = os.getenv("XERO_WEBHOOK_KEY")
    QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN: Optional[str] = os.getenv("QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN")

    # Optional: the public https address of this backend when it is not an ngrok tunnel on this machine (it is
    # auto-detected from a local ngrok otherwise). Used only to show the webhook URLs and to run the test button.
    PUBLIC_BASE_URL: Optional[str] = os.getenv("PUBLIC_BASE_URL")

    # Background sync of the clients a user switched "Auto-sync" on for.
    SYNC_ENABLED: bool = str(os.getenv("SYNC_ENABLED", "true")).lower() == "true"
    SYNC_NIGHTLY_HOUR: int = int(os.getenv("SYNC_NIGHTLY_HOUR", "2"))            # local time; also catches up after downtime
    SYNC_MAX_AGE_HOURS: int = int(os.getenv("SYNC_MAX_AGE_HOURS", "24"))         # re-sync a client if older than this
    SYNC_WEBHOOK_MIN_GAP_SECONDS: int = int(os.getenv("SYNC_WEBHOOK_MIN_GAP_SECONDS", "30"))  # coalesce webhook bursts

    # Where the browser is sent after the OAuth consent screen (Xero / QuickBooks)
    FRONTEND_URL: str = os.getenv("FRONTEND_URL") or "http://localhost:3000"

    class Config:
        case_sensitive = True

settings = Settings()

# Ensure directories exist
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.TEMP_DIR, exist_ok=True)
