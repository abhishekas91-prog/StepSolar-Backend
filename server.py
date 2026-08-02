"""Step Solar — Lead Capture + CRM Backend

- POST /api/leads             public: quotation form submission (auto-assigns code + stages)
- GET  /api/leads             admin token: list leads (recent 100)
- GET  /api/leads/stats       admin token: totals
- GET  /api/health            integration status

CRM (admin token required for all /api/crm/*):
- GET   /api/crm/leads        return every lead (full CRM shape)
- POST  /api/crm/leads        add lead from CRM (auto-code, stages, source=Admin)
- PATCH /api/crm/leads/{id}   partial update (any of: stages, quotation, invoice, contact fields)
- GET   /api/crm/meta         return {nextLeadCode} for display
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from starlette.middleware.cors import CORSMiddleware

from lead_service import (
    SHEET_HEADERS,
    append_lead_to_sheet,
    gmail_enabled,
    send_lead_email,
    sheets_enabled,
)


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stepsolar")

# --------------------------------------------------------------------------- #
# Mongo
# --------------------------------------------------------------------------- #
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
leads_collection = db["leads"]
meta_collection = db["meta"]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _dup_window_min() -> int:
    try:
        return max(0, int(os.environ.get("DUPLICATE_WINDOW_MIN", "10")))
    except ValueError:
        return 10


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


# --------------------------------------------------------------------------- #
# CRM pipeline template
# --------------------------------------------------------------------------- #
STAGE_TEMPLATE: List[Dict[str, str]] = [
    {"key": "reg",         "label": "Consumer Registration",     "actor": "Consumer", "owner": "Sales"},
    {"key": "app",         "label": "Consumer Application",       "actor": "Consumer", "owner": "Sales"},
    {"key": "feas",        "label": "Discom Feasibility",         "actor": "Discom",   "owner": "Site Survey"},
    {"key": "vendor",      "label": "Consumer Vendor Selection",  "actor": "Consumer", "owner": "Sales"},
    {"key": "agreement",   "label": "Vendor Upload Agreement",    "actor": "Vendor",   "owner": "Accounts"},
    {"key": "install",     "label": "Vendor Installation",        "actor": "Vendor",   "owner": "Installation"},
    {"key": "inspection",  "label": "Discom Inspection",          "actor": "Discom",   "owner": "Installation"},
    {"key": "commission",  "label": "Project Commissioning",      "actor": "Discom",   "owner": "Installation"},
    {"key": "subsidyreq",  "label": "Consumer Subsidy Request",   "actor": "Consumer", "owner": "Accounts"},
    {"key": "subsidydisb", "label": "Subsidy Disbursal",          "actor": "REC",      "owner": "Accounts"},
]


def _fresh_stages() -> List[Dict[str, Any]]:
    return [
        {**t, "status": "Pending", "updatedAt": None, "notes": "", "documents": []}
        for t in STAGE_TEMPLATE
    ]


async def _next_lead_code() -> str:
    """Atomically increment the lead counter and return the formatted code."""
    doc = await meta_collection.find_one_and_update(
        {"_id": "counters"},
        {"$inc": {"nextLeadNo": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    # If it was just upserted, $inc set it to 1; that becomes SSE-0001
    n = int(doc.get("nextLeadNo", 1))
    return f"SSE-{n:04d}"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
PIN_RE = re.compile(r"^\d{6}$")

PROPERTY_TYPES = {
    "Residential",
    "Commercial / Office",
    "Industrial / Factory",
    "Agricultural / Pump",
}
ROOF_TYPES = {
    "Rented Roof / No Roof",
    "Small Space (100-200 sq. ft.)",
    "Medium Space (300-500 sq. ft.)",
    "Large Open Roof (500+ sq. ft.)",
}
TIMELINES = {
    "Immediately",
    "Within 1-2 months",
    "Sirf jankari aur quotation chahiye",
}


class LeadCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str = Field(..., min_length=2, max_length=120)
    phone: str
    email: EmailStr
    state: str = Field(..., min_length=2, max_length=80)
    city: str = Field(..., min_length=2, max_length=80)
    pincode: str
    property_type: str
    monthly_bill: int = Field(..., ge=0, le=10_000_000)
    roof_type: str
    timeline: str
    source: Optional[str] = Field(default="website", max_length=60)

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if not PHONE_RE.match(v):
            raise ValueError("phone must be a valid 10-digit Indian mobile number")
        return v

    @field_validator("pincode")
    @classmethod
    def _v_pin(cls, v: str) -> str:
        v = (v or "").strip()
        if not PIN_RE.match(v):
            raise ValueError("pincode must be exactly 6 digits")
        return v

    @field_validator("property_type")
    @classmethod
    def _v_prop(cls, v: str) -> str:
        if v not in PROPERTY_TYPES:
            raise ValueError(f"property_type must be one of {sorted(PROPERTY_TYPES)}")
        return v

    @field_validator("roof_type")
    @classmethod
    def _v_roof(cls, v: str) -> str:
        v = v.replace("\u2013", "-").replace("\u2014", "-")
        if v not in ROOF_TYPES:
            raise ValueError(f"roof_type must be one of {sorted(ROOF_TYPES)}")
        return v

    @field_validator("timeline")
    @classmethod
    def _v_timeline(cls, v: str) -> str:
        v = v.replace("\u2013", "-").replace("\u2014", "-")
        if v not in TIMELINES:
            raise ValueError(f"timeline must be one of {sorted(TIMELINES)}")
        return v


class LeadResponse(BaseModel):
    ok: bool
    id: str
    code: str
    message: str
    sheet_synced: bool
    email_sent: bool


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="Step Solar — Lead Capture + CRM API")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"service": "Step Solar Lead + CRM API", "status": "ok"}


@api.get("/health")
async def health():
    return {
        "status": "ok",
        "integrations": {
            "mongodb": True,
            "google_sheets": sheets_enabled(),
            "gmail": gmail_enabled(),
        },
        "duplicate_window_min": _dup_window_min(),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def _is_duplicate(phone: str, email: str, window_min: int) -> bool:
    if window_min <= 0:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).isoformat()
    existing = await leads_collection.find_one(
        {
            "$and": [
                {"$or": [{"phone": phone}, {"email": email.lower()}]},
                {"created_at": {"$gte": cutoff}},
            ]
        },
        {"_id": 0, "id": 1},
    )
    return existing is not None


def _check_admin(token: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin token not configured on server")
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")


def _admin_from_request(
    token_q: Optional[str], token_header: Optional[str]
) -> None:
    """Accept token via ?token= query or X-Admin-Token header."""
    _check_admin(token_q or token_header)


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    return doc


# --------------------------------------------------------------------------- #
# Public: website form
# --------------------------------------------------------------------------- #
@api.post("/leads", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(payload: LeadCreate, request: Request):
    if await _is_duplicate(payload.phone, payload.email, _dup_window_min()):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "We already received a request from this phone/email a few minutes ago. "
                "Our team will call you shortly."
            ),
        )

    lead_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")[:400]
    code = await _next_lead_code()

    stages = _fresh_stages()
    stages[0]["status"] = "In Progress"
    stages[0]["updatedAt"] = now_iso
    stages[0]["notes"] = "Auto-created from website enquiry form."

    doc = {
        "id": lead_id,
        "code": code,
        "full_name": payload.full_name,
        "phone": payload.phone,
        "email": payload.email.lower(),
        "state": payload.state,
        "city": payload.city,
        "pincode": payload.pincode,
        "property_type": payload.property_type,
        "monthly_bill": payload.monthly_bill,
        "roof_type": payload.roof_type,
        "timeline": payload.timeline,
        "source": payload.source or "website",
        "ip": ip,
        "user_agent": ua,
        "created_at": now_iso,
        "updated_at": now_iso,
        "stages": stages,
        "quotation": None,
        "invoice": None,
        "sheet_synced": False,
        "email_sent": False,
        "sheet_error": None,
        "email_error": None,
    }
    await leads_collection.insert_one(doc)

    # External sync (feature-flagged) — parallel
    row = [
        now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        doc["full_name"], doc["phone"], doc["email"],
        doc["state"], doc["city"], doc["pincode"],
        doc["property_type"], doc["monthly_bill"],
        doc["roof_type"], doc["timeline"],
        doc["source"], doc["ip"],
    ]
    sheet_task = asyncio.to_thread(append_lead_to_sheet, row)
    email_task = asyncio.to_thread(send_lead_email, doc)
    sheet_result, email_result = await asyncio.gather(sheet_task, email_task)

    update_fields = {
        "sheet_synced": bool(sheet_result.get("ok")),
        "email_sent": bool(email_result.get("ok")),
        "sheet_error": None if sheet_result.get("ok") else sheet_result.get("error"),
        "email_error": None if email_result.get("ok") else email_result.get("error"),
    }
    await leads_collection.update_one({"id": lead_id}, {"$set": update_fields})

    return LeadResponse(
        ok=True,
        id=lead_id,
        code=code,
        message="Thank you! Our team will contact you shortly to schedule a free site survey.",
        sheet_synced=update_fields["sheet_synced"],
        email_sent=update_fields["email_sent"],
    )


# --------------------------------------------------------------------------- #
# Legacy admin endpoints
# --------------------------------------------------------------------------- #
@api.get("/leads")
async def list_leads(
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(default=100, ge=1, le=500),
):
    _admin_from_request(token, x_admin_token)
    cursor = leads_collection.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    return items


@api.get("/leads/stats")
async def leads_stats(
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _admin_from_request(token, x_admin_token)
    total = await leads_collection.count_documents({})
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    today = await leads_collection.count_documents({"created_at": {"$gte": today_start}})
    return {"total": total, "today": today, "sheet_headers": SHEET_HEADERS}


# --------------------------------------------------------------------------- #
# CRM endpoints
# --------------------------------------------------------------------------- #
crm = APIRouter(prefix="/crm")

# Field allow-list for PATCH updates
_PATCHABLE = {
    "full_name", "phone", "email", "state", "city", "pincode",
    "property_type", "monthly_bill", "roof_type", "timeline",
    "source", "stages", "quotation", "invoice", "notes",
}


@crm.get("/meta")
async def crm_meta(
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _admin_from_request(token, x_admin_token)
    m = await meta_collection.find_one({"_id": "counters"})
    last = int((m or {}).get("nextLeadNo", 0))
    next_no = last + 1
    return {
        "lastIssuedLeadNo": last,
        "lastIssuedLeadCode": f"SSE-{last:04d}" if last > 0 else None,
        "nextLeadNo": next_no,
        "nextLeadCode": f"SSE-{next_no:04d}",
    }


@crm.get("/leads")
async def crm_list_leads(
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(default=500, ge=1, le=5000),
):
    _admin_from_request(token, x_admin_token)
    cursor = leads_collection.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


class CRMLeadCreate(LeadCreate):
    """CRM lead intake (admin) — inherits validators from LeadCreate, default source=Admin."""
    source: Optional[str] = Field(default="Admin", max_length=60)


@crm.post("/leads")
async def crm_create_lead(
    payload: CRMLeadCreate,
    request: Request,
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _admin_from_request(token, x_admin_token)

    lead_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    code = await _next_lead_code()

    stages = _fresh_stages()
    stages[0]["status"] = "In Progress"
    stages[0]["updatedAt"] = now_iso

    doc = {
        "id": lead_id,
        "code": code,
        "full_name": payload.full_name,
        "phone": payload.phone,
        "email": payload.email.lower(),
        "state": payload.state,
        "city": payload.city,
        "pincode": payload.pincode,
        "property_type": payload.property_type,
        "monthly_bill": payload.monthly_bill,
        "roof_type": payload.roof_type,
        "timeline": payload.timeline,
        "source": payload.source or "Admin",
        "ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:400],
        "created_at": now_iso,
        "updated_at": now_iso,
        "stages": stages,
        "quotation": None,
        "invoice": None,
        "sheet_synced": False,
        "email_sent": False,
        "sheet_error": None,
        "email_error": None,
    }
    await leads_collection.insert_one(doc)
    return _clean(doc)


@crm.patch("/leads/{lead_id}")
async def crm_patch_lead(
    lead_id: str,
    body: Dict[str, Any],
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _admin_from_request(token, x_admin_token)

    update = {k: v for k, v in (body or {}).items() if k in _PATCHABLE}
    if not update:
        raise HTTPException(status_code=400, detail="No patchable fields provided")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = await leads_collection.find_one_and_update(
        {"id": lead_id},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if not result:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result


@crm.delete("/leads/{lead_id}")
async def crm_delete_lead(
    lead_id: str,
    token: Optional[str] = Query(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _admin_from_request(token, x_admin_token)
    r = await leads_collection.delete_one({"id": lead_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True}


api.include_router(crm)
app.include_router(api)


# --------------------------------------------------------------------------- #
# Validation error handler
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    msg = first.get("msg", "Invalid input")
    return JSONResponse(
        status_code=422,
        content={"ok": False, "detail": f"{field}: {msg}" if field else msg},
    )


# --------------------------------------------------------------------------- #
# Startup — indexes + one-time migration for legacy leads
# --------------------------------------------------------------------------- #
async def _migrate_legacy_leads():
    """Add code/stages/quotation/invoice to leads that predate the CRM schema."""
    cursor = leads_collection.find(
        {"$or": [{"code": {"$exists": False}}, {"stages": {"$exists": False}}]},
        {"_id": 0, "id": 1, "created_at": 1},
    )
    async for lead in cursor:
        code = await _next_lead_code()
        stages = _fresh_stages()
        stages[0]["status"] = "In Progress"
        stages[0]["updatedAt"] = lead.get("created_at")
        await leads_collection.update_one(
            {"id": lead["id"]},
            {"$set": {
                "code": code,
                "stages": stages,
                "quotation": None,
                "invoice": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )


@app.on_event("startup")
async def _startup():
    await leads_collection.create_index("id", unique=True)
    await leads_collection.create_index("code", unique=True, sparse=True)
    await leads_collection.create_index([("created_at", -1)])
    await leads_collection.create_index([("phone", 1), ("created_at", -1)])
    await leads_collection.create_index([("email", 1), ("created_at", -1)])
    await _migrate_legacy_leads()
    logger.info(
        "Startup — sheets_enabled=%s gmail_enabled=%s dup_window_min=%s",
        sheets_enabled(),
        gmail_enabled(),
        _dup_window_min(),
    )


@app.on_event("shutdown")
async def _shutdown():
    client.close()
