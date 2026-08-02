"""
External integrations: Google Sheets (append) and Gmail SMTP (notify).
Both integrations are FEATURE-FLAGGED: they no-op cleanly if the required
env vars are missing, so leads still get saved to MongoDB in every case.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Google Sheets (Service Account + gspread)
# --------------------------------------------------------------------------- #

SHEET_HEADERS = [
    "Timestamp",
    "Full Name",
    "Phone",
    "Email",
    "State",
    "City",
    "Pincode",
    "Property Type",
    "Monthly Bill (INR)",
    "Roof Type",
    "Timeline",
    "Source",
    "IP",
]


def _load_service_account_info() -> Optional[Dict[str, Any]]:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    # Support both raw JSON string and a file path
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: %s", e)
            return None
    if os.path.exists(raw):
        try:
            with open(raw, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to read service account file %s: %s", raw, e)
            return None
    logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is set but is neither JSON nor a valid path.")
    return None


def sheets_enabled() -> bool:
    return bool(
        os.environ.get("GOOGLE_SHEET_ID", "").strip()
        and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )


def _open_worksheet():
    """Open (or create) the target worksheet and ensure headers exist."""
    import gspread
    from google.oauth2.service_account import Credentials

    info = _load_service_account_info()
    if info is None:
        raise RuntimeError("Service account JSON not available")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)

    sheet_id = os.environ["GOOGLE_SHEET_ID"].strip()
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", "Leads").strip() or "Leads"

    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
        return ws

    # Ensure header row exists (only if the sheet is empty)
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
    return ws


def append_lead_to_sheet(row: List[Any]) -> Dict[str, Any]:
    """Append a single lead row. Returns {'ok': bool, 'error': str|None}."""
    if not sheets_enabled():
        return {"ok": False, "error": "sheets_disabled", "skipped": True}
    try:
        ws = _open_worksheet()
        ws.append_row(row, value_input_option="USER_ENTERED")
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        logger.exception("Google Sheets append failed")
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# Gmail SMTP notification
# --------------------------------------------------------------------------- #

def gmail_enabled() -> bool:
    return bool(
        os.environ.get("GMAIL_USER", "").strip()
        and os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        and os.environ.get("GMAIL_TO", "").strip()
    )


def _build_html(lead: Dict[str, Any]) -> str:
    def esc(v: Any) -> str:
        s = "" if v is None else str(v)
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    rows = [
        ("Full Name", lead.get("full_name")),
        ("Phone", lead.get("phone")),
        ("Email", lead.get("email")),
        ("State", lead.get("state")),
        ("City", lead.get("city")),
        ("Pincode", lead.get("pincode")),
        ("Property Type", lead.get("property_type")),
        ("Monthly Bill", f"₹ {lead.get('monthly_bill')}" if lead.get("monthly_bill") is not None else ""),
        ("Roof Type", lead.get("roof_type")),
        ("Timeline", lead.get("timeline")),
        ("Source", lead.get("source") or "website"),
        ("IP", lead.get("ip") or ""),
        ("Submitted At", lead.get("created_at") or ""),
    ]

    body_rows = "".join(
        f'<tr><td style="padding:8px 14px;border-bottom:1px solid #eee;color:#4B5A54;'
        f'font-family:Inter,Arial,sans-serif;font-size:13px;width:180px;">{esc(k)}</td>'
        f'<td style="padding:8px 14px;border-bottom:1px solid #eee;color:#182420;'
        f'font-family:Inter,Arial,sans-serif;font-size:14px;font-weight:500;">{esc(v)}</td></tr>'
        for k, v in rows
    )

    phone = esc(lead.get("phone") or "")
    email = esc(lead.get("email") or "")
    name = esc(lead.get("full_name") or "")

    return f"""
<!doctype html>
<html><body style="margin:0;padding:0;background:#F4F8F4;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#F4F8F4;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;background:#FFFFFF;border-radius:16px;
                    box-shadow:0 20px 50px -20px rgba(18,80,123,0.25);overflow:hidden;">
        <tr><td style="background:linear-gradient(90deg,#1B6FA8,#3FA535);
                        padding:22px 28px;color:#fff;
                        font-family:'Space Grotesk',Arial,sans-serif;">
          <div style="font-size:12px;letter-spacing:0.14em;text-transform:uppercase;
                      opacity:0.9;">New Lead — Step Solar</div>
          <div style="font-size:22px;font-weight:700;margin-top:6px;">{name or 'New Quotation Request'}</div>
        </td></tr>

        <tr><td style="padding:20px 28px 6px 28px;
                        font-family:Inter,Arial,sans-serif;color:#4B5A54;font-size:14px;">
          A new "Get Free Quotation" form has been submitted on your website.
        </td></tr>

        <tr><td style="padding:8px 28px 20px 28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #eee;border-radius:12px;overflow:hidden;">
            {body_rows}
          </table>
        </td></tr>

        <tr><td style="padding:0 28px 24px 28px;
                        font-family:Inter,Arial,sans-serif;font-size:13px;">
          <a href="tel:{phone}" style="display:inline-block;background:#F5A623;color:#2A1A00;
             padding:11px 20px;border-radius:999px;font-weight:600;text-decoration:none;
             margin-right:10px;">Call {phone}</a>
          <a href="mailto:{email}" style="display:inline-block;background:#12507B;color:#fff;
             padding:11px 20px;border-radius:999px;font-weight:600;text-decoration:none;">
             Email {email}</a>
        </td></tr>

        <tr><td style="padding:16px 28px;background:#12241A;color:rgba(231,237,233,0.75);
                        font-family:Inter,Arial,sans-serif;font-size:12px;">
          Step Solar Energy Pvt Ltd · Automated lead notification
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
""".strip()


def send_lead_email(lead: Dict[str, Any]) -> Dict[str, Any]:
    if not gmail_enabled():
        return {"ok": False, "error": "gmail_disabled", "skipped": True}
    try:
        user = os.environ["GMAIL_USER"].strip()
        password = os.environ["GMAIL_APP_PASSWORD"].strip()
        recipients = [
            r.strip() for r in os.environ["GMAIL_TO"].split(",") if r.strip()
        ]
        from_name = os.environ.get("GMAIL_FROM_NAME", "Step Solar Leads").strip()

        msg = EmailMessage()
        subject_name = lead.get("full_name") or lead.get("phone") or "New enquiry"
        msg["Subject"] = f"[Step Solar Lead] {subject_name} — {lead.get('city','')}"
        msg["From"] = f"{from_name} <{user}>"
        msg["To"] = ", ".join(recipients)
        if lead.get("email"):
            msg["Reply-To"] = lead["email"]

        msg.set_content(
            "New lead received.\n\n"
            f"Name: {lead.get('full_name')}\n"
            f"Phone: {lead.get('phone')}\n"
            f"Email: {lead.get('email')}\n"
            f"City: {lead.get('city')}, {lead.get('state')} - {lead.get('pincode')}\n"
            f"Property: {lead.get('property_type')}\n"
            f"Monthly Bill: {lead.get('monthly_bill')}\n"
            f"Roof: {lead.get('roof_type')}\n"
            f"Timeline: {lead.get('timeline')}\n"
        )
        msg.add_alternative(_build_html(lead), subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)
        return {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001
        logger.exception("Gmail send failed")
        return {"ok": False, "error": str(e)}
