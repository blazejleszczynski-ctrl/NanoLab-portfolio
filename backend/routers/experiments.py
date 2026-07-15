from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime, timezone
from services import json_db

router = APIRouter(prefix="/experiments", tags=["Experiments"])

FILE = "experiments.json"


# --- Modele danych ---

class Source(BaseModel):
    doi: str = ""
    first_author: str = ""
    year: Optional[int] = None
    title: str = ""
    pdf_filename: str = ""


class Reagent(BaseModel):
    name_eng: str
    chemical_formula: str = ""
    cas: str = ""
    purity: str = ""
    quantity: float | None = None
    unit: str = ""
    ai_extracted: bool = False
    bottle_id: Optional[str] = None


class Step(BaseModel):
    step_id: int
    action_type: str
    title: str
    description: str = ""
    parameters: dict = {}
    expected_duration_min: Optional[float] = None
    expected_observation: str = ""
    critical: bool = False


class ExperimentCreate(BaseModel):
    experiment_id: str
    target_material: str = ""
    experiment_type: str = ""
    synthesis_method: str = ""
    notes: str = ""
    reagents: list[Reagent] = []
    ai_suggested_reagents: list[Reagent] = []   # niezmienny zapis oryginalnej sugestii LLM — do wglądu/przywrócenia
    steps: list[Step] = []
    source: Source = Source()
    status: str = "draft"
    date_started: Optional[str] = None   # ustawiane automatycznie jeśli brak


class ExperimentUpdate(BaseModel):
    target_material: Optional[str] = None
    experiment_type: Optional[str] = None
    synthesis_method: Optional[str] = None
    notes: Optional[str] = None
    reagents: Optional[list[Reagent]] = None
    ai_suggested_reagents: Optional[list[Reagent]] = None
    steps: Optional[list[Step]] = None
    source: Optional[Source] = None
    status: Optional[str] = None


# --- Endpointy ---

@router.get("")
def get_experiments():
    """Lista wszystkich eksperymentów."""
    return json_db.read_all(FILE)


@router.get("/{experiment_id}")
def get_experiment(experiment_id: str):
    """Pojedynczy eksperyment — używany przy edycji."""
    record = json_db.find_by_id(FILE, experiment_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")
    return record


@router.post("", status_code=201)
def create_experiment(body: ExperimentCreate):
    """Nowy eksperyment (save as draft lub submit)."""
    existing = json_db.read_all(FILE)
    if any(r["id"] == body.experiment_id for r in existing):
        raise HTTPException(status_code=409, detail=f"Experiment {body.experiment_id} already exists")
    data = body.model_dump()
    if not data["date_started"]:
        data["date_started"] = date.today().isoformat()
    record = {"id": body.experiment_id, **data}
    return json_db.append(FILE, record)


@router.patch("/{experiment_id}")
def update_experiment(experiment_id: str, body: ExperimentUpdate):
    """Aktualizacja eksperymentu — save draft lub zmiana statusu."""
    record = json_db.find_by_id(FILE, experiment_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return json_db.update_by_id(FILE, experiment_id, updates)


@router.delete("/{experiment_id}")
def delete_experiment(experiment_id: str):
    """Usuwa eksperyment. Zablokowane (status in_synthesis) nie mogą być usuwane."""
    record = json_db.find_by_id(FILE, experiment_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")
    if record.get("status") == "in_synthesis":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a locked experiment (in_synthesis). Change its status first.",
        )
    json_db.delete_by_id(FILE, experiment_id)
    return {"deleted": True, "id": experiment_id}


@router.patch("/{experiment_id}/archive")
def archive_experiment(experiment_id: str):
    """Przenosi eksperyment do archiwum bez zmiany jego status. Zablokowane (in_synthesis) nie mogą być archiwizowane."""
    record = json_db.find_by_id(FILE, experiment_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Experiment {experiment_id} not found")
    if record.get("status") == "in_synthesis":
        raise HTTPException(
            status_code=400,
            detail="Cannot archive a locked experiment (in_synthesis). Change its status first.",
        )
    return json_db.update_by_id(FILE, experiment_id, {
        "archived": True,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    })
