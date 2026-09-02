"""Google Sheets side: one spreadsheet per sales rep, appended in turn."""

import os
import json
import base64
import logging

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Column order written to every rep's sheet. "Call Status" and "Remarks" are left
# blank on purpose - that is where the rep works.
COLUMNS = [
    "Assigned At",
    "Assigned To",
    "Checkout ID",
    "Checkout",
    "Abandoned At",
    "Customer Name",
    "Email",
    "Phone",
    "City",
    "State",
    "Country",
    "Products",
    "Items",
    "Cart Value",
    "Currency",
    "Recovery Link",
    "Call Status",
    "Remarks",
]

CHECKOUT_ID_COL = COLUMNS.index("Checkout ID") + 1  # 1-based, for gspread


class SheetsError(RuntimeError):
    pass


def _load_credentials():
    """Service-account creds from GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON, base64, or a path)."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if path and os.path.exists(path):
            return Credentials.from_service_account_file(path, scopes=SCOPES)
        raise SheetsError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Paste the whole service-account "
            "JSON file into that environment variable / GitHub secret."
        )

    if os.path.exists(raw):
        return Credentials.from_service_account_file(raw, scopes=SCOPES)

    if not raw.lstrip().startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:
            raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON, a path, nor base64") from exc

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {}".format(exc)) from exc

    return Credentials.from_service_account_info(info, scopes=SCOPES)


def service_account_email():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    try:
        if os.path.exists(raw):
            with open(raw, "r", encoding="utf-8") as fh:
                return json.load(fh).get("client_email", "")
        if not raw.lstrip().startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        return json.loads(raw).get("client_email", "")
    except Exception:
        return ""


class SheetsClient:
    def __init__(self):
        self.gc = gspread.authorize(_load_credentials())
        self._cache = {}

    def worksheet(self, sheet_id, worksheet_name):
        """Open (creating if needed) the rep's tab, guaranteeing a header row."""
        key = (sheet_id, worksheet_name)
        if key in self._cache:
            return self._cache[key]

        try:
            spreadsheet = self.gc.open_by_key(sheet_id)
        except gspread.exceptions.APIError as exc:
            raise SheetsError(
                "Cannot open spreadsheet {}. Share it (Editor) with the service account {} "
                "and check the ID in config.json. Google said: {}".format(
                    sheet_id, service_account_email() or "<service account>", exc)
            ) from exc

        try:
            ws = spreadsheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            log.info("Creating worksheet '%s' in %s", worksheet_name, sheet_id)
            ws = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(COLUMNS))

        header = ws.row_values(1)
        if [h.strip() for h in header][:len(COLUMNS)] != COLUMNS:
            if any(cell.strip() for cell in header):
                log.warning("Header in '%s' differs from the expected columns; leaving it alone. "
                            "Rows are appended in this order: %s", worksheet_name, COLUMNS)
            else:
                ws.update(values=[COLUMNS], range_name="A1")
                ws.format("A1:{}1".format(gspread.utils.rowcol_to_a1(1, len(COLUMNS))[:-1]),
                          {"textFormat": {"bold": True}})
                ws.freeze(rows=1)

        self._cache[key] = ws
        return ws

    @staticmethod
    def existing_checkout_ids(ws):
        """Checkout IDs already present in the sheet - a safety net against duplicates
        if state.json is ever lost or reset."""
        try:
            values = ws.col_values(CHECKOUT_ID_COL)[1:]  # skip header
        except gspread.exceptions.APIError as exc:
            log.warning("Could not read existing IDs from %s: %s", ws.title, exc)
            return set()
        return {v.strip() for v in values if v and v.strip()}

    @staticmethod
    def append(ws, rows):
        if not rows:
            return
        ws.append_rows(rows, value_input_option="USER_ENTERED",
                       insert_data_option="INSERT_ROWS", table_range="A1")
