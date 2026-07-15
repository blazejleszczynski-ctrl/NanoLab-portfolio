from fastapi import APIRouter, HTTPException
from services import json_db

router = APIRouter(prefix="/reagents", tags=["Reagents"])

REAGENTS_FILE = "reagents.json"
BOTTLES_FILE  = "bottles.json"

LOW_STOCK_THRESHOLD = 0.20   # poniżej 20% initial_quantity → "low"


# --- Endpointy ---

@router.get("")
def get_reagents():
    """Lista wszystkich odczynników z katalogu."""
    return json_db.read_all(REAGENTS_FILE)


@router.get("/availability")
def get_availability():
    """
    Mapa dostępności odczynników dla frontendu.
    Zwraca: { "nazwa_ang": "ok" | "low" | "none", ... }
    """
    reagents = json_db.read_all(REAGENTS_FILE)
    bottles  = json_db.read_all(BOTTLES_FILE)

    # Dla każdego odczynnika zsumuj aktywne butelki
    stock: dict[int, dict] = {}
    for b in bottles:
        if not b.get("active", True):
            continue
        rid = b["reagent_id"]
        if rid not in stock:
            stock[rid] = {"current": 0.0, "initial": 0.0}
        stock[rid]["current"] += b.get("current_quantity", 0.0)
        stock[rid]["initial"] += b.get("initial_quantity", 0.0)

    result = {}
    for r in reagents:
        rid  = r["reagent_id"]
        name = r.get("name_eng", "")
        s    = stock.get(rid)
        if not s or s["initial"] == 0:
            result[name] = "none"
        elif s["current"] / s["initial"] <= LOW_STOCK_THRESHOLD:
            result[name] = "low"
        else:
            result[name] = "ok"

    return result
