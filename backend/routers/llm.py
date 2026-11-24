# Import PDF: tekst z PDF (pdfplumber), lista promptów, wywołanie LLM tekstowego przez bramkę (profile *-or z bramka/modele.json).
# Limit tokenów wyjścia: MAX_OUTPUT_TOKENS_ARTICLE w .env; log wywołań: logi/llm_call.ndjson.
import os
import re
import json
import time
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from dotenv import load_dotenv
from config import PDF_MAX_PAGES, LLM_MAX_TEXT_CHARS, LLM_MAX_INSTRUCTION_CHARS, LLM_RATE_LIMIT_SECONDS
from bramka import bramka, log_ndjson
from bramka.wywolanie_http import MODELE_JSON, modele_zadania, wywolaj_http

load_dotenv()

router = APIRouter(prefix="/llm", tags=["LLM"])

BACKEND_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LLM_DIR      = os.path.join(BACKEND_DIR, "LLM")
PROMPTS_DIR  = os.path.join(LLM_DIR, "prompts")
EXTRACTED_DIR = os.path.join(LLM_DIR, "extracted")

MODELS = {m["id"]: m["profil"] for m in modele_zadania("pdf")}   # model z przeglądarki (np. openai/gpt-4o-mini) → profil bramki

_LOG = log_ndjson.Log(os.path.join(BACKEND_DIR, "logi"), "llm_call")   # jeden log na czas działania serwera
_LOG.start(profile=sorted(MODELS.values()))

_last_llm_call: dict[str, float] = defaultdict(float)


# --- Pomocnicze ---

def _parse_prompt_file(path: str) -> tuple[str, str, dict | None]:
    """Zwraca (system_prompt, user_prompt, json_schema|None) z pliku .md."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    system = re.search(r"## system\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    user   = re.search(r"## user\n(.*?)(?=\n## |\Z)",   content, re.DOTALL)
    schema_block = re.search(r"## schema\n```json\n(.*?)```", content, re.DOTALL)
    schema = None
    if schema_block:
        try:
            schema = json.loads(schema_block.group(1).strip())
        except json.JSONDecodeError:
            pass
    return (
        system.group(1).strip() if system else "",
        user.group(1).strip()   if user   else "{text}",
        schema,
    )


# --- Modele żądań ---

class LLMCallRequest(BaseModel):
    text: str
    model: str = "openai/gpt-4o"
    prompt_file: str
    user_instruction: str = ""


# --- Endpointy ---

@router.get("/models/{zadanie}")
def list_models(zadanie: str):
    """Modele dla listy w formularzu (pdf – import PDF, tem – analiza obrazu TEM) z bramka/modele.json."""
    modele = [{"id": m["id"], "label": m["label"]} for m in modele_zadania(zadanie)]
    if not modele:
        raise HTTPException(status_code=404, detail=f"Brak modeli dla zadania: {zadanie}")
    return modele


@router.get("/prompts")
def list_prompts():
    """Lista dostępnych plików promptów z katalogu LLM/prompts/."""
    if not os.path.exists(PROMPTS_DIR):
        return []
    files = [f for f in os.listdir(PROMPTS_DIR) if f.endswith(".md")]
    result = []
    for f in sorted(files):
        path = os.path.join(PROMPTS_DIR, f)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
        desc_match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
        result.append({
            "file": f,
            "name": name_match.group(1).strip() if name_match else f,
            "description": desc_match.group(1).strip() if desc_match else "",
        })
    return result


@router.post("/extract-pdf")
async def extract_pdf(file: UploadFile = File(...)):
    """
    Odbiera plik PDF, wyciąga tekst przez pdfplumber,
    zapisuje do LLM/extracted/ i zwraca tekst.
    """
    try:
        import pdfplumber
    except ImportError:
        raise HTTPException(status_code=500, detail="pdfplumber not installed. Run: pip install pdfplumber")

    contents = await file.read()
    import io
    text_pages = []
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        total_pages = len(pdf.pages)
        for page in pdf.pages[:PDF_MAX_PAGES]:
            t = page.extract_text()
            if t:
                text_pages.append(t)

    if not text_pages:
        raise HTTPException(status_code=422, detail="No text could be extracted from this PDF.")

    truncated = total_pages > PDF_MAX_PAGES

    extracted_text = "\n\n--- PAGE BREAK ---\n\n".join(text_pages)

    safe_name = re.sub(r"[^\w\-.]", "_", file.filename or "upload")
    out_path = os.path.join(EXTRACTED_DIR, safe_name + ".txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(extracted_text)

    return {
        "filename": file.filename,
        "saved_as": safe_name + ".txt",
        "pages": len(text_pages),
        "total_pages": total_pages,
        "truncated": truncated,
        "chars": len(extracted_text),
        "text": extracted_text,
    }


@router.post("/call")
def llm_call(body: LLMCallRequest, request: Request):
    """
    Wywołuje LLM przez bramkę (OpenRouter).
    Zwraca wynik + pełne metadane wywołania do śledzenia (tokeny, czas, koszt).
    """
    # Rate limiting
    if LLM_RATE_LIMIT_SECONDS > 0:
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        wait = LLM_RATE_LIMIT_SECONDS - (now - _last_llm_call[ip])
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"Rate limit: wait {int(wait) + 1}s before next LLM call.")
        _last_llm_call[ip] = now

    profil_nazwa = MODELS.get(body.model)
    if not profil_nazwa:
        raise HTTPException(status_code=400, detail=f"Unknown model: {body.model}. Available: {list(MODELS.keys())}")
    profil = bramka.wczytaj_profil(MODELE_JSON, profil_nazwa, {"etap": "nanolab-pdf"})

    if not body.prompt_file.endswith(".md") or "/" in body.prompt_file or "\\" in body.prompt_file or ".." in body.prompt_file:
        raise HTTPException(status_code=400, detail="Invalid prompt_file name.")
    prompt_path = os.path.join(PROMPTS_DIR, body.prompt_file)
    if not os.path.exists(prompt_path):
        raise HTTPException(status_code=404, detail=f"Prompt file not found: {body.prompt_file}")

    if len(body.text) > LLM_MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"text exceeds {LLM_MAX_TEXT_CHARS:,} characters. Split your input.")
    if len(body.user_instruction) > LLM_MAX_INSTRUCTION_CHARS:
        raise HTTPException(status_code=400, detail=f"user_instruction exceeds {LLM_MAX_INSTRUCTION_CHARS} characters.")

    system_prompt, user_template, schema = _parse_prompt_file(prompt_path)
    user_prompt = user_template.replace("{text}", body.text)
    if body.user_instruction.strip():
        user_prompt = f"Additional instruction from the user: {body.user_instruction.strip()}\n\n{user_prompt}"

    # Schemat z pliku promptu → ścisły JSON; prompt bez schematu → zwykły tekst (result = None, gdy to nie JSON)
    wynik = wywolaj_http(profil, system_prompt, user_prompt, [], schema, _LOG, limit_env="MAX_OUTPUT_TOKENS_ARTICLE")
    raw_content = wynik["tekst"]

    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        parsed = None

    tokeny = (wynik["tokeny_wejscie"], wynik["tokeny_wyjscie"])
    return {
        "result": parsed,
        "raw_response": raw_content,
        "meta": {
            "model":        body.model,
            "model_id":     profil["model"],
            "prompt_file":  body.prompt_file,
            "elapsed_ms":   wynik["czas_ms"],
            "prompt_tokens":     tokeny[0],
            "completion_tokens": tokeny[1],
            "total_tokens":      sum(tokeny) if None not in tokeny else None,
            "koszt_usd":         round(wynik["koszt_usd"], 6),
        }
    }
