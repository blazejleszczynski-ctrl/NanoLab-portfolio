import base64
import io
import json
import math
import os
import pathlib
import re
import datetime
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/llm", tags=["tem-analysis"])

# ─── Paths ────────────────────────────────────────────────────────────────────

_BASE_DIR    = pathlib.Path(__file__).parent.parent
_PROMPTS_DIR = _BASE_DIR / "LLM" / "prompts" / "vision"

# ─── JSON schemas for response_format (OpenAI strict mode) ───────────────────
#
# Using response_format: json_schema with strict: true instead of tool use.
# Works reliably with GPT-4o and GPT-4o mini via OpenRouter.
# The model MUST return a JSON object matching the schema — no prose, no fences.
#
# Strict mode constraints:
# - All object properties must be listed in "required"
# - All objects must have "additionalProperties": false
# - minItems/maxItems on arrays are NOT supported in strict mode (removed)

_QUALIFY_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "image_quality":                 {"type": "string", "enum": ["good", "fair", "poor", "unusable"]},
        "can_analyze":                   {"type": "boolean"},
        "quality_note":                  {"type": "string"},
        "estimated_n_particles_visible": {"type": "integer"},
        "aggregation_level":             {"type": "string", "enum": ["none", "mild", "severe"]},
        "recommendation":                {"type": "string"},
    },
    "required": [
        "image_quality", "can_analyze", "quality_note",
        "estimated_n_particles_visible", "aggregation_level", "recommendation",
    ],
    "additionalProperties": False,
}

_REPORT_PARTICLES_SCHEMA = {
    "type": "object",
    "properties": {
        "comment": {"type": "string"},
        "particles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bbox":         {"type": "array", "items": {"type": "number"}},
                    "diameter_nm":  {"type": "number"},
                    "confidence":   {"type": "number"},
                    "has_fringes":  {"type": "boolean"},
                    "d_spacing_nm": {"type": "number"},
                },
                "required": ["bbox", "diameter_nm", "confidence", "has_fringes", "d_spacing_nm"],
                "additionalProperties": False,
            },
        },
        "warning": {"type": "string"},
    },
    "required": ["comment", "particles", "warning"],
    "additionalProperties": False,
}

# ─── Prompt file loading ──────────────────────────────────────────────────────

def _parse_prompt_file(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}

    if text.startswith("---"):
        end = text.index("---", 3)
        for line in text[3:end].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        text = text[end + 3:].strip()

    sections: dict = {}
    current: Optional[str] = None
    buf: list = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)

    if current is not None:
        sections[current] = "\n".join(buf).strip()

    return {**meta, "sections": sections, "filename": path.name}


def _load_prompt(filename: str) -> dict:
    if not filename.endswith(".md") or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, f"Invalid prompt filename: {filename!r}")
    path = _PROMPTS_DIR / filename
    if not path.exists():
        raise HTTPException(404, f"Vision prompt not found: {filename}")
    return _parse_prompt_file(path)


def _list_prompts() -> list:
    if not _PROMPTS_DIR.exists():
        return []
    result = []
    for p in sorted(_PROMPTS_DIR.glob("*.md")):
        try:
            parsed = _parse_prompt_file(p)
            result.append({
                "filename":    p.name,
                "name":        parsed.get("name", p.stem),
                "description": parsed.get("description", ""),
                "version":     parsed.get("version", ""),
            })
        except Exception:
            pass
    return result


# ─── Request schemas ──────────────────────────────────────────────────────────

class AnalysisContext(BaseModel):
    sample_name: str = ""
    scientist:   str = ""
    material:    str = ""
    expected_shape:   str   = "spherical"
    expected_min_nm:  float = 1.0
    expected_max_nm:  float = 100.0
    pixel_size_nm:    float = 0.0
    image_width_px:   int   = 0
    image_height_px:  int   = 0
    microscope:       str   = ""
    voltage_kv:       float = 0.0
    magnification:    float = 0.0
    magnification_actual: float = 0.0
    experiment_notes: str   = ""
    imaging_mode:     str   = "standard"   # hrtem | standard | overview
    field_of_view_w_nm: float = 0.0
    field_of_view_h_nm: float = 0.0
    min_detectable_nm:  float = 0.0


class AnalyzeTemRequest(BaseModel):
    jpg_base64:  str
    context:     AnalysisContext
    model:       str = "openai/gpt-4o"
    prompt_file: str = "tem_analysis_v1.md"


# ─── OpenRouter vision call with json_schema response_format ─────────────────

def _strip_b64_prefix(b64: str) -> str:
    return b64.split(",", 1)[1] if "," in b64 else b64


def _call_with_schema(
    model: str,
    system: str,
    user_text: str,
    jpg_b64_raw: str,
    schema_name: str,
    schema: dict,
) -> dict:
    """
    Call OpenRouter vision API with response_format: json_schema (strict).
    Works with GPT-4o and GPT-4o mini — model must return valid JSON matching schema.
    """
    api_key = os.getenv("OPENROUTER_API_KEY_VISION") or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "OPENROUTER_API_KEY_VISION (or OPENROUTER_API_KEY) not set in backend/.env")

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpg_b64_raw}", "detail": "high"}},
                {"type": "text", "text": user_text},
            ],
        },
    ]

    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name":   schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://nanolab.amu.edu.pl",
                "X-Title": "NanoLab TEM image",
            },
            json=payload,
            timeout=120,
        )
    except requests.Timeout:
        raise HTTPException(504, "OpenRouter request timed out (120 s)")
    except requests.RequestException as e:
        raise HTTPException(502, f"OpenRouter connection error: {e}")

    if not resp.ok:
        raise HTTPException(502, f"OpenRouter HTTP {resp.status_code}: {resp.text[:400]}")

    try:
        message = resp.json()["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise HTTPException(502, f"Unexpected OpenRouter response structure: {e}")

    content = (message.get("content") or "").strip()
    if not content:
        raise HTTPException(502, "LLM returned empty content")

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"LLM returned non-JSON content: {content[:300]!r}")


# ─── Validation + statistics ──────────────────────────────────────────────────

def _validate_particles(particles: list, img_w: int, img_h: int, pixel_size_nm: float) -> tuple:
    """
    Filter out physically impossible or out-of-bounds particles.
    Returns (valid_list, n_removed).

    When pixel_size_nm == 0 (calibration unknown):
    - diameter in nm is unknown → accepted as 0 (displayed as "—" in UI)
    - bbox/diameter consistency check is skipped
    - physical diameter range check is skipped
    """
    calibrated = pixel_size_nm > 0
    valid = []

    for p in particles:
        # ── bbox ─────────────────────────────────────────────────────────────
        bbox = p.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
        except (TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1 or x1 < 0 or y1 < 0:
            continue
        if img_w > 0 and x2 > img_w:
            continue
        if img_h > 0 and y2 > img_h:
            continue

        # ── diameter ──────────────────────────────────────────────────────────
        d = p.get("diameter_nm") or 0
        if not isinstance(d, (int, float)):
            continue
        if calibrated:
            # Physical range 0.5–500 nm
            if not (0.5 <= d <= 500):
                continue
            # Diameter must match bbox size within ±50%
            bbox_size_px = max(x2 - x1, y2 - y1)
            expected_nm  = bbox_size_px * pixel_size_nm
            if d > 0 and abs(d - expected_nm) / max(d, 0.001) > 0.5:
                continue
        else:
            # No calibration: accept d = 0 (unknown), reject negatives
            if d < 0:
                continue

        # ── confidence ────────────────────────────────────────────────────────
        c = p.get("confidence")
        if c is None or not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
            continue

        # ── d-spacing: 0 → None ───────────────────────────────────────────────
        d_spacing = p.get("d_spacing_nm") or None
        if isinstance(d_spacing, (int, float)) and d_spacing <= 0:
            d_spacing = None

        valid.append({
            "bbox":         [x1, y1, x2, y2],
            "diameter_nm":  round(float(d), 2) if calibrated and d > 0 else None,
            "confidence":   round(float(c), 3),
            "has_fringes":  bool(p.get("has_fringes", False)),
            "d_spacing_nm": round(float(d_spacing), 4) if d_spacing else None,
        })

    return valid, len(particles) - len(valid)


def _compute_statistics(particles: list) -> dict:
    diams = [p["diameter_nm"] for p in particles if p.get("diameter_nm", 0) > 0]
    n = len(diams)
    if n == 0:
        return {"n": 0, "mean_nm": 0.0, "std_nm": 0.0, "min_nm": 0.0, "max_nm": 0.0}
    mean = sum(diams) / n
    var  = sum((d - mean) ** 2 for d in diams) / n if n > 1 else 0.0
    return {
        "n":       n,
        "mean_nm": round(mean, 2),
        "std_nm":  round(math.sqrt(var), 2),
        "min_nm":  round(min(diams), 2),
        "max_nm":  round(max(diams), 2),
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/vision-prompts")
def list_vision_prompts():
    """List available vision prompt files from LLM/prompts/vision/."""
    return {"prompts": _list_prompts()}


@router.post("/analyze-tem")
async def analyze_tem(req: AnalyzeTemRequest):
    """
    Two-phase TEM image analysis via LLM vision (OpenRouter).

    Uses tool use (function calling) to guarantee structured output — the model
    is forced to call qualify_image (Phase 1) and report_particles (Phase 2).
    This works for GPT-4o and Claude via OpenRouter.

    Prompts loaded from LLM/prompts/vision/{prompt_file}.
    API key: OPENROUTER_API_KEY_VISION (fallback: OPENROUTER_API_KEY).
    """
    ctx       = req.context
    model     = req.model
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    b64_raw   = _strip_b64_prefix(req.jpg_base64)

    # ── Pre-resize image for LLM ──────────────────────────────────────────────
    # GPT-4o does not expose the internal resolution it uses when processing
    # large images, so bboxes come back in an unknown coordinate space.
    # We pre-resize to exactly 1024 px wide, adjust pixel_size_nm proportionally,
    # and scale bboxes back to original coordinates after the LLM responds.
    _LLM_MAX_W   = 1024
    _img_scale   = 1.0          # original → LLM; <1 means image was shrunk
    b64_for_llm  = b64_raw
    pixel_size_nm_llm = ctx.pixel_size_nm

    try:
        from PIL import Image as _PILImage
        _pil      = _PILImage.open(io.BytesIO(base64.b64decode(b64_raw)))
        _orig_w, _orig_h = _pil.size
        if _orig_w > _LLM_MAX_W:
            _img_scale = _LLM_MAX_W / _orig_w
            _pil_r = _pil.resize(
                (int(_orig_w * _img_scale), int(_orig_h * _img_scale)),
                _PILImage.LANCZOS,
            )
            _buf = io.BytesIO()
            _pil_r.save(_buf, format="JPEG", quality=92)
            b64_for_llm = base64.b64encode(_buf.getvalue()).decode()
            if ctx.pixel_size_nm > 0:
                pixel_size_nm_llm = ctx.pixel_size_nm / _img_scale
    except Exception:
        pass  # fall back to original image / original pixel_size_nm

    prompt   = _load_prompt(req.prompt_file)
    sections = prompt.get("sections", {})

    size_line  = (
        f"Image size: {ctx.image_width_px} × {ctx.image_height_px} px. "
        if ctx.image_width_px and ctx.image_height_px else ""
    )
    notes_line = f"Additional context: {ctx.experiment_notes}" if ctx.experiment_notes else ""

    calibration_note = (
        ""
        if ctx.pixel_size_nm > 0
        else "IMPORTANT: pixel calibration is unavailable (pixel_size_nm = 0). "
             "Still detect all particles and draw bounding boxes. "
             "Set diameter_nm = 0 for each particle — do not skip detection."
    )

    pixel_size_display = (
        f"{ctx.pixel_size_nm} nm/px"
        if ctx.pixel_size_nm > 0
        else "not calibrated (absent from DM3 metadata)"
    )

    # pixel_size_nm sent to LLM = scaled value matching the pre-resized image
    calibration_note_llm = (
        ""
        if pixel_size_nm_llm > 0
        else calibration_note
    )

    def _fov_display(w_nm: float, h_nm: float) -> str:
        if w_nm <= 0 or h_nm <= 0:
            return "unknown"
        if w_nm >= 1000:
            return f"{w_nm/1000:.2f} × {h_nm/1000:.2f} µm"
        return f"{w_nm:.0f} × {h_nm:.0f} nm"

    fmt = {
        "material":             ctx.material    or "unknown",
        "expected_shape":       ctx.expected_shape,
        "expected_min_nm":      ctx.expected_min_nm,
        "expected_max_nm":      ctx.expected_max_nm,
        "microscope":           ctx.microscope  or "unknown",
        "voltage_kv":           ctx.voltage_kv,
        "magnification":        int(ctx.magnification),
        "magnification_actual": int(ctx.magnification_actual) if ctx.magnification_actual else "unknown",
        "pixel_size_nm":        pixel_size_nm_llm,   # calibrated to LLM image size
        "pixel_size_display":   pixel_size_display,  # human-readable, original value
        "imaging_mode":         ctx.imaging_mode,
        "field_of_view":        _fov_display(ctx.field_of_view_w_nm, ctx.field_of_view_h_nm),
        "min_detectable_nm":    ctx.min_detectable_nm,
        "scientist":            ctx.scientist   or "unknown",
        "sample_name":          ctx.sample_name or "unknown",
        "size_line":            size_line,
        "notes_line":           notes_line,
        "calibration_note":     calibration_note_llm,
    }

    p1_system = sections.get("phase1_system", "").format(**fmt)
    p1_user   = sections.get("phase1_user",   "").format(**fmt)
    p2_system = sections.get("phase2_system", "").format(**fmt)
    p2_user   = sections.get("phase2_user",   "").format(**fmt)

    # ── Phase 1: image qualification ──────────────────────────────────────────
    phase1 = _call_with_schema(model, p1_system, p1_user, b64_for_llm, "qualify_image", _QUALIFY_IMAGE_SCHEMA)

    if not phase1.get("can_analyze", False):
        return {
            "can_analyze": False,
            "phase1":      phase1,
            "particles":   [],
            "statistics":  {"n": 0, "mean_nm": 0.0, "std_nm": 0.0, "min_nm": 0.0, "max_nm": 0.0},
            "phase2":      None,
            "ai_provenance": {
                "model":          model,
                "prompt_file":    req.prompt_file,
                "prompt_version": prompt.get("version", ""),
                "timestamp":      timestamp,
            },
        }

    # ── Phase 2: quantitative particle detection ───────────────────────────────
    phase2 = _call_with_schema(model, p2_system, p2_user, b64_for_llm, "report_particles", _REPORT_PARTICLES_SCHEMA)

    raw_particles = phase2.get("particles") or []

    # Scale bboxes back from LLM image space → original image coordinates
    if _img_scale < 1.0 and raw_particles:
        inv = 1.0 / _img_scale
        for p in raw_particles:
            bbox = p.get("bbox")
            if bbox and len(bbox) == 4:
                p["bbox"] = [round(c * inv) for c in bbox]

    # Recompute diameter_nm from bbox + pixel_size_nm (backend is authoritative).
    # LLMs estimate size visually, not arithmetically — their diameter values are
    # unreliable. We use the (now correctly-scaled) bbox for the ground truth.
    if ctx.pixel_size_nm > 0:
        for p in raw_particles:
            bbox = p.get("bbox", [])
            if len(bbox) == 4:
                px_size = max(abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1]))
                p["diameter_nm"] = round(px_size * ctx.pixel_size_nm, 2)

    # Detect "grid artifact": LLM placed identically-sized boxes in a regular
    # pattern rather than detecting actual particle boundaries. Signature:
    # all bbox sizes within ±5 px of each other (std < 5) with ≥ 5 particles.
    grid_artifact = False
    if len(raw_particles) >= 5:
        sizes = [
            max(abs(p["bbox"][2] - p["bbox"][0]), abs(p["bbox"][3] - p["bbox"][1]))
            for p in raw_particles
            if p.get("bbox") and len(p["bbox"]) == 4
        ]
        if sizes:
            mean_s = sum(sizes) / len(sizes)
            std_s  = math.sqrt(sum((s - mean_s) ** 2 for s in sizes) / len(sizes))
            if std_s < 5:
                grid_artifact = True

    valid_particles, n_removed = _validate_particles(
        raw_particles, ctx.image_width_px, ctx.image_height_px, ctx.pixel_size_nm,
    )

    warning = (phase2.get("warning") or "").strip() or None
    if grid_artifact:
        note    = "Grid artifact detected: LLM placed identical-sized boxes without detecting real particle boundaries. Results are unreliable — try a different model or image."
        warning = f"{warning} {note}".strip() if warning else note
    if n_removed > 0:
        note    = f"[{n_removed} particle(s) removed by backend validation]"
        warning = f"{warning} {note}".strip() if warning else note

    stats = _compute_statistics(valid_particles)

    return {
        "can_analyze": True,
        "phase1":      phase1,
        "particles":   valid_particles,
        "statistics":  stats,
        "phase2": {
            "comment":   phase2.get("comment", ""),
            "warning":   warning,
        },
        "ai_provenance": {
            "model":             model,
            "prompt_file":       req.prompt_file,
            "prompt_version":    prompt.get("version", ""),
            "timestamp":         timestamp,
            "phase1_result":     phase1,
            "n_raw_particles":   len(raw_particles),
            "n_valid_particles": len(valid_particles),
        },
    }
