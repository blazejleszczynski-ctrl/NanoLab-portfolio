from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services import json_db

router = APIRouter(prefix="/tem", tags=["TEM"])

FILE = "tem_requests.json"

REQUIRED_PREP_FIELDS = {
    "grid_type": "Grid type",
    "solvent": "Solvent",
    "application_method": "Application method",
    "drying_method": "Drying method",
}


# --- Modele danych ---

class ColumnUpdate(BaseModel):
    column: str  # pending / prep / imaging

class PrepData(BaseModel):
    grid_type: str = ""
    solvent: str = ""
    dilution: str = ""
    application_method: str = ""
    volume_ul: float | None = None
    drying_method: str = ""
    atmosphere: str = ""
    staining: str = ""
    notes: str = ""

class ImagingDraft(BaseModel):
    scope: dict = {}
    naming: dict = {}

class NewRequest(BaseModel):
    exp_id: str
    material: str
    scientist: str
    submitted: str
    scientist_note: str = ""

class NotifyData(BaseModel):
    folder_path: str = ""

class ArchiveData(BaseModel):
    box_number: str
    slot: str


# --- Endpointy ---

@router.get("/requests")
def get_requests():
    """Lista wszystkich próbek w kolejce TEM."""
    return json_db.read_all(FILE)


@router.get("/requests/{request_id}")
def get_request(request_id: str):
    """Pojedyncza próbka — używana przez tem-preparation.html i tem-imaging.html."""
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    return record


@router.post("/requests", status_code=201)
def create_request(body: NewRequest):
    """Nowe zlecenie TEM od naukowca."""
    existing = json_db.read_all(FILE)
    new_id = f"TEM-{len(existing) + 1:03d}"
    record = {
        "id": new_id,
        "column": "pending",
        **body.model_dump()
    }
    return json_db.append(FILE, record)


@router.patch("/requests/{request_id}/column")
def update_column(request_id: str, body: ColumnUpdate):
    """Drag & drop w kanbanie — zmiana kolumny."""
    allowed = {"pending", "prep", "imaging"}
    if body.column not in allowed:
        raise HTTPException(status_code=400, detail=f"column must be one of {allowed}")
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    if body.column == "imaging":
        prep = record.get("preparation") or {}
        missing = [label for key, label in REQUIRED_PREP_FIELDS.items() if not prep.get(key)]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required preparation fields: {', '.join(missing)}",
            )
    updated = json_db.update_by_id(FILE, request_id, {"column": body.column})
    return updated


@router.patch("/requests/{request_id}/imaging_draft")
def save_imaging_draft(request_id: str, body: ImagingDraft):
    """Zapis roboczy danych sesji mikroskopowej (tem-imaging.html)."""
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    return json_db.update_by_id(FILE, request_id, {"imaging_draft": body.model_dump()})


@router.patch("/requests/{request_id}/preparation")
def save_preparation(request_id: str, body: PrepData):
    """Zapis danych preparatyki próbki (tem-preparation.html)."""
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    updated = json_db.update_by_id(FILE, request_id, {"preparation": body.model_dump()})
    return updated


@router.patch("/requests/{request_id}/notify")
def notify_scientist(request_id: str, body: NotifyData):
    """Operator wysyła powiadomienie do naukowca po zapisaniu plików na dysk (tem-imaging.html)."""
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    updated = json_db.update_by_id(FILE, request_id, {
        "notified": True,
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "image_folder_path": body.folder_path,
    })
    return updated


@router.patch("/requests/{request_id}/archive")
def archive_sample(request_id: str, body: ArchiveData):
    """Operator odkłada siateczkę do pudełka archiwalnego po naświetleniu — próbka znika z kanbanu."""
    record = json_db.find_by_id(FILE, request_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")
    if not record.get("notified"):
        raise HTTPException(status_code=400, detail="Sample must be notified before it can be archived")
    if not body.box_number.strip() or not body.slot.strip():
        raise HTTPException(status_code=400, detail="box_number and slot are required")
    updated = json_db.update_by_id(FILE, request_id, {
        "column": "archived",
        "archive": {
            "box_number": body.box_number,
            "slot": body.slot,
            "archived_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    return updated
