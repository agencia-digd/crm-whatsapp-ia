"""Helper compartilhado: abre a planilha Acme via service account."""
import os
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "SPREADSHEET_ID"
_CRED_ENV = os.environ.get("ACME_SHEETS_CRED")
if not _CRED_ENV:
    raise SystemExit("ACME_SHEETS_CRED não setado no ambiente (.env)")
CRED = Path(_CRED_ENV)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def open_sheet():
    if not CRED.exists():
        raise SystemExit(f"credencial não encontrada: {CRED}")
    creds = Credentials.from_service_account_file(str(CRED), scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(SHEET_ID)
