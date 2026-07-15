"""
Uruchom gdy plik danych jest pusty lub uszkodzony: python seed.py
Bezpieczny do ponownego uruchomienia — pomija pliki które już mają dane.
"""
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def save(filename, data):
    """Zapisz dane tylko jeśli plik nie istnieje lub jest pusty/uszkodzony."""
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing:
                print(f"  {filename} — pomijam ({len(existing)} rekordów już istnieje)")
                return
        except Exception:
            pass  # plik uszkodzony — nadpisz
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  {filename} — zapisano {len(data)} rekordów")


tem_requests = [
    {"id": "TEM-2026-001", "exp_id": "EXP-2026-001", "material": "Fe₃O₄ (co-precipitation)",  "scientist": "dr A. Kowalska", "submitted": "2026-05-15", "column": "imaging", "scientist_note": "Sample in hexane (1 mg/mL), drop cast on Cu 300 grid."},
    {"id": "TEM-2026-002", "exp_id": "EXP-2026-002", "material": "Fe₃O₄ + sodium citrate",    "scientist": "dr A. Kowalska", "submitted": "2026-06-01", "column": "imaging", "scientist_note": "Aqueous suspension, pH ~7. Do not use hexane or non-polar solvents."},
    {"id": "TEM-2026-003", "exp_id": "EXP-2026-004", "material": "Fe₃O₄ (thermal decomp.)",   "scientist": "dr A. Kowalska", "submitted": "2026-06-08", "column": "prep",    "scientist_note": "Sample in hexane (~1 mg/mL). Previous batch showed agglomeration - try diluting 1:5 before drop casting."},
    {"id": "TEM-2026-004", "exp_id": "EXP-2026-005", "material": "Fe₃O₄ · OAm stabilizer",   "scientist": "dr A. Kowalska", "submitted": "2026-06-12", "column": "pending", "scientist_note": "Synthesis just finished. Sample in toluene. Can be diluted if needed."},
    {"id": "TEM-2026-005", "exp_id": "EXP-2026-006", "material": "ZnO (sol-gel)",                      "scientist": "dr A. Kowalska", "submitted": "2026-06-13", "column": "pending", "scientist_note": "Ethanol suspension, 0.5 mg/mL. Handle gently to avoid sedimentation."},
]

print("Generowanie seed data...")
save("tem_requests.json", tem_requests)
save("tem_grids.json", [])
save("experiments.json", [])
print("Gotowe.")
