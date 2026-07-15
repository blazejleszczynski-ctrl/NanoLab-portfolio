from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from services import json_db

router = APIRouter(prefix="/bottles", tags=["Bottles"])

BOTTLES_FILE  = "bottles.json"
REAGENTS_FILE = "reagents.json"


class BottleCreate(BaseModel):
    name: str
    formula: str = ""
    cas: str = ""
    purity: str = ""
    manufacturer: str = ""
    hazard: str = "low"
    quantity: float
    unit: str = ""
    expiry: str = ""
    location: str = ""


@router.get("")
def get_bottles():
    """Lista butelek z magazynu — złączona z katalogiem odczynników."""
    reagents = {r["reagent_id"]: r for r in json_db.read_all(REAGENTS_FILE)}
    bottles  = json_db.read_all(BOTTLES_FILE)
    result = []
    for b in bottles:
        if not b.get("active", True):
            continue
        r = reagents.get(b["reagent_id"], {})
        result.append({
            "bottle_id":    b["bottle_id"],
            "name":         r.get("name_eng", ""),
            "formula":      r.get("chemical_formula", ""),
            "cas":          r.get("cas", ""),
            "hazard":       r.get("hazard_level", "low"),
            "purity":       b.get("purity"),
            "manufacturer": b.get("manufacturer", ""),
            "current":      b.get("current_quantity", 0),
            "initial":      b.get("initial_quantity", 0),
            "unit":         b.get("unit", ""),
            "expiry":       b.get("expiry_date", ""),
            "location":     b.get("location", ""),
        })
    return result


@router.post("", status_code=201)
def create_bottle(body: BottleCreate):
    """Dodaje nową butelkę do magazynu. Szuka istniejącego odczynnika lub tworzy nowy."""
    reagents = json_db.read_all(REAGENTS_FILE)
    bottles  = json_db.read_all(BOTTLES_FILE)

    formula_lower = body.formula.lower().strip()
    name_lower    = body.name.lower().strip()

    reagent = None
    for r in reagents:
        r_formula = (r.get("chemical_formula") or "").lower()
        r_name    = (r.get("name_eng") or "").lower()
        if (formula_lower and r_formula == formula_lower) or r_name == name_lower:
            reagent = r
            break

    if not reagent:
        new_rid = max((r["reagent_id"] for r in reagents), default=0) + 1
        reagent = {
            "reagent_id":       new_rid,
            "cas":              body.cas or "",
            "name_pl":          "",
            "name_eng":         body.name,
            "chemical_formula": body.formula,
            "hazard_level":     body.hazard,
        }
        json_db.append(REAGENTS_FILE, reagent)

    new_bid = max((b["bottle_id"] for b in bottles), default=0) + 1
    bottle = {
        "bottle_id":            new_bid,
        "reagent_id":           reagent["reagent_id"],
        "purity":               body.purity or None,
        "manufacturer":         body.manufacturer,
        "manufacturer_serial":  "",
        "source_experiment_id": None,
        "initial_quantity":     body.quantity,
        "current_quantity":     body.quantity,
        "unit":                 body.unit,
        "expiry_date":          body.expiry or "",
        "location":             body.location,
        "active":               True,
    }
    json_db.append(BOTTLES_FILE, bottle)

    return {
        "bottle_id":    new_bid,
        "name":         reagent["name_eng"],
        "formula":      reagent.get("chemical_formula", ""),
        "cas":          reagent.get("cas", ""),
        "hazard":       reagent.get("hazard_level", "low"),
        "purity":       body.purity or None,
        "manufacturer": body.manufacturer,
        "current":      body.quantity,
        "initial":      body.quantity,
        "unit":         body.unit,
        "expiry":       body.expiry or "",
        "location":     body.location,
    }
