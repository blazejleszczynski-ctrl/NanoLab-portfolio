import os
import re
import json
import time
import requests
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from dotenv import load_dotenv
from config import PDF_MAX_PAGES, LLM_MAX_TEXT_CHARS, LLM_MAX_INSTRUCTION_CHARS, LLM_RATE_LIMIT_SECONDS

load_dotenv()

router = APIRouter(prefix="/llm", tags=["LLM"])

LLM_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "LLM")
PROMPTS_DIR  = os.path.join(LLM_DIR, "prompts")
EXTRACTED_DIR = os.path.join(LLM_DIR, "extracted")

MODELS = {
    "gpt-4o":       "openai/gpt-4o",
    "gpt-4o-mini":  "openai/gpt-4o-mini",
}

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
    model: str = "gpt-4o"
    prompt_file: str
    user_instruction: str = ""


# --- Endpointy ---

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
    Wywołuje LLM przez OpenRouter.
    Zwraca wynik + pełne metadane wywołania do śledzenia.
    """
    # Rate limiting
    if LLM_RATE_LIMIT_SECONDS > 0:
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        wait = LLM_RATE_LIMIT_SECONDS - (now - _last_llm_call[ip])
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"Rate limit: wait {int(wait) + 1}s before next LLM call.")
        _last_llm_call[ip] = now

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY not set in .env")

    model_id = MODELS.get(body.model)
    if not model_id:
        raise HTTPException(status_code=400, detail=f"Unknown model: {body.model}. Available: {list(MODELS.keys())}")

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

    if schema:
        schema_name = body.prompt_file.replace(".md", "").replace("-", "_")
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
    else:
        response_format = {"type": "json_object"}

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "response_format": response_format,
    }

    t_start = time.time()
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    elapsed_ms = int((time.time() - t_start) * 1000)

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"OpenRouter error {response.status_code}: {response.text}")

    data = response.json()
    raw_content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        parsed = None

    return {
        "result": parsed,
        "raw_response": raw_content,
        "meta": {
            "model":        body.model,
            "model_id":     model_id,
            "prompt_file":  body.prompt_file,
            "elapsed_ms":   elapsed_ms,
            "prompt_tokens":     usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens":      usage.get("total_tokens"),
        }
    }
