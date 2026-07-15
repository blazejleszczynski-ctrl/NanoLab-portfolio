import io
import os
import json
import base64
import struct
import tempfile
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/dm3", tags=["dm3"])


# ─── tag extraction ───────────────────────────────────────────────────────────

# Candidate tag paths, tried in order.
#
# ncempy allTags key format (confirmed from real JEOL JEM-1400 DM3 files):
#   - prefix is "." (dot), NOT "root."
#   - real TEM image is in ImageList.2, not ImageList.1
#     (ImageList.1 is a document/overlay object with no calibration)
#   - Dimension indices are 1-based: Dimension.1 = Y, Dimension.2 = X
#
# Each list tries the most common path first, then fallbacks for older
# software versions or single-image DM3 files.
_TAG_MAP = {
    "voltage_kv": [
        ".ImageList.2.ImageTags.Microscope Info.Voltage",
        ".ImageList.1.ImageTags.Microscope Info.Voltage",
        ".ImageList.2.ImageTags.Microscope Info.High Tension",
        ".ImageList.1.ImageTags.Microscope Info.High Tension",
    ],
    "magnification": [
        ".ImageList.2.ImageTags.Microscope Info.Indicated Magnification",
        ".ImageList.1.ImageTags.Microscope Info.Indicated Magnification",
    ],
    "magnification_actual": [
        ".ImageList.2.ImageTags.Microscope Info.Actual Magnification",
        ".ImageList.1.ImageTags.Microscope Info.Actual Magnification",
    ],
    "magnification_interpolated": [
        ".ImageList.2.ImageTags.Microscope Info.Magnification Interpolated",
        ".ImageList.1.ImageTags.Microscope Info.Magnification Interpolated",
    ],
    "microscope": [
        ".ImageList.2.ImageTags.Microscope Info.Microscope",
        ".ImageList.2.ImageTags.Microscope Info.Name",
        ".ImageList.1.ImageTags.Microscope Info.Microscope",
        ".ImageList.1.ImageTags.Microscope Info.Name",
    ],
    "acquisition_date": [
        ".ImageList.2.ImageTags.DataBar.Acquisition Date",
        ".ImageList.1.ImageTags.DataBar.Acquisition Date",
    ],
    "acquisition_time": [
        ".ImageList.2.ImageTags.DataBar.Acquisition Time",
        ".ImageList.1.ImageTags.DataBar.Acquisition Time",
    ],
    "pixel_size": [
        # Confirmed present in JEOL JEM-1400 DM3 (GMS software): 1-based Dimension index
        ".ImageList.2.ImageData.Calibrations.Dimension.1.Scale",
        ".ImageList.2.ImageData.Calibrations.Dimension.2.Scale",
        ".ImageList.1.ImageData.Calibrations.Dimension.1.Scale",
        # Older DM3 / alternative indexing
        ".ImageList.1.ImageData.Calibrations.Dimension.0.Scale",
        ".ImageList.2.ImageData.Calibrations.Dimension.0.Scale",
    ],
    "pixel_units": [
        ".ImageList.2.ImageData.Calibrations.Dimension.1.Units",
        ".ImageList.2.ImageData.Calibrations.Dimension.2.Units",
        ".ImageList.1.ImageData.Calibrations.Dimension.1.Units",
        ".ImageList.1.ImageData.Calibrations.Dimension.0.Units",
    ],
    "image_width_px": [
        ".ImageList.2.ImageData.Dimensions.0",
        ".ImageList.1.ImageData.Dimensions.0",
    ],
    "image_height_px": [
        ".ImageList.2.ImageData.Dimensions.1",
        ".ImageList.1.ImageData.Dimensions.1",
    ],
    "stage_alpha_deg": [
        ".ImageList.2.ImageTags.Stage Position.Stage Alpha",
        ".ImageList.1.ImageTags.Stage Position.Stage Alpha",
    ],
    "stage_x_um": [
        ".ImageList.2.ImageTags.Stage Position.Stage X",
        ".ImageList.1.ImageTags.Stage Position.Stage X",
    ],
    "stage_y_um": [
        ".ImageList.2.ImageTags.Stage Position.Stage Y",
        ".ImageList.1.ImageTags.Stage Position.Stage Y",
    ],
    "exposure_s": [
        ".ImageList.2.ImageTags.Acquisition.Parameters.High Level.Exposure (s)",
        ".ImageList.2.ImageTags.Acquisition.Parameters.Detector.exp_time",
        ".ImageList.1.ImageTags.Acquisition.Parameters.High Level.Exposure (s)",
        ".ImageList.1.ImageTags.Acquisition.Parameters.Detector.exposure (s)",
    ],
    "spot_size": [
        ".ImageList.2.ImageTags.Microscope Info.Spot Size",
        ".ImageList.1.ImageTags.Microscope Info.Spot Size",
    ],
    "illumination_mode": [
        ".ImageList.2.ImageTags.Microscope Info.Illumination Mode",
        ".ImageList.2.ImageTags.Microscope Info.Imaging Mode",
        ".ImageList.1.ImageTags.Microscope Info.Illumination Mode",
        ".ImageList.1.ImageTags.Microscope Info.Imaging Mode",
    ],
    "camera": [
        ".ImageList.2.ImageTags.Acquisition.Device.Name",
        ".ImageList.2.ImageTags.DataBar.Device Name",
        ".ImageList.1.ImageTags.DataBar.Instrument Name",
        ".ImageList.1.ImageTags.Acquisition.Device.Name",
    ],
    "operator": [
        ".ImageList.2.ImageTags.Microscope Info.Operator",
        ".ImageList.1.ImageTags.Microscope Info.Operator",
    ],
    "specimen": [
        ".ImageList.2.ImageTags.Microscope Info.Specimen",
        ".ImageList.1.ImageTags.Microscope Info.Specimen",
    ],
    "exposure_number": [
        ".ImageList.2.ImageTags.DataBar.Exposure Number",
        ".ImageList.1.ImageTags.DataBar.Exposure Number",
    ],
}


def _to_nm(value: float, units: str) -> float:
    """Convert pixel calibration to nm regardless of stored unit."""
    u = (units or "nm").strip().lower()
    if u in ("nm", "nanometer", "nanometers"):
        return value
    if u in ("a", "å", "angstrom", "angstroms"):
        return value * 0.1
    if u in ("um", "µm", "micrometer", "micrometers"):
        return value * 1000.0
    if u == "m":
        return value * 1e9
    if u in ("pm", "picometer", "picometers"):
        return value * 0.001
    # Unknown unit — return as-is and let operator verify
    return value


def _normalize_tags(raw: dict) -> dict:
    """
    ncempy allTags key format varies by version:
      - older ncempy: "root.ImageList.1.ImageData..." (root. prefix)
      - newer ncempy: ".ImageList.1.ImageData..."    (dot-only prefix)
    Normalise everything to the dot-only form so _TAG_MAP paths always match.
    """
    out = {}
    for k, v in raw.items():
        if k.startswith("root."):
            k = "." + k[5:]   # "root.ImageList..." → ".ImageList..."
        out[k] = v
    return out


def _extract_key_tags(all_tags: dict) -> dict:
    tags = _normalize_tags(all_tags)
    result = {}
    for field, paths in _TAG_MAP.items():
        for path in paths:
            if path not in tags or tags[path] is None:
                continue
            val = tags[path]
            # Skip empty strings (some units tags are empty for uncalibrated dims)
            if isinstance(val, str) and val.strip() == "":
                continue
            # pixel_size = 0 means uncalibrated — skip to next candidate
            if field == "pixel_size" and isinstance(val, (int, float)) and val == 0:
                continue
            # Voltage stored in V in DM3 — convert to kV
            if field == "voltage_kv" and isinstance(val, (int, float)) and val > 1000:
                val = round(val / 1000, 1)
            result[field] = val
            break

    # Normalise pixel_size to nm so the frontend never needs unit conversion
    raw_ps = result.get("pixel_size")
    if raw_ps is not None and hasattr(raw_ps, "item"):
        raw_ps = raw_ps.item()
    if isinstance(raw_ps, (int, float)) and raw_ps > 0:
        raw_units = result.get("pixel_units") or "nm"
        result["pixel_size_nm"] = _to_nm(float(raw_ps), str(raw_units))
    else:
        result["pixel_size_nm"] = 0.0

    # Convert all remaining numpy scalars to native Python types
    for k, v in list(result.items()):
        if hasattr(v, "item"):
            try:
                result[k] = v.item()
            except ValueError:
                del result[k]

    # ── Computed fields ───────────────────────────────────────────────────────
    pixel_size_nm = result.get("pixel_size_nm", 0.0)
    w_px = result.get("image_width_px", 0)
    h_px = result.get("image_height_px", 0)

    result["field_of_view_w_nm"] = round(w_px * pixel_size_nm, 2) if (w_px and pixel_size_nm) else 0.0
    result["field_of_view_h_nm"] = round(h_px * pixel_size_nm, 2) if (h_px and pixel_size_nm) else 0.0
    result["min_detectable_nm"]  = round(3 * pixel_size_nm, 3) if pixel_size_nm > 0 else 0.0

    if pixel_size_nm > 0:
        if pixel_size_nm < 0.1:
            result["imaging_mode"] = "hrtem"
        elif pixel_size_nm <= 2.0:
            result["imaging_mode"] = "standard"
        else:
            result["imaging_mode"] = "overview"
    else:
        result["imaging_mode"] = "unknown"

    return result


def _to_python(v):
    """Convert numpy scalar → native Python; skip arrays; leave primitives as-is."""
    if isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float)):
        return v
    if hasattr(v, "item"):
        # numpy scalar → ok; numpy array with >1 element → ValueError → skip
        try:
            return v.item()
        except ValueError:
            return None
    return None


def _safe_tags(all_tags: dict) -> dict:
    """Return only JSON-serialisable scalar tag values (numpy scalars converted)."""
    out = {}
    for k, v in all_tags.items():
        converted = _to_python(v)
        if converted is not None:
            out[k] = converted
    return out


# ─── POST /api/dm3/process ───────────────────────────────────────────────────

@router.post("/process")
async def process_dm3(file: UploadFile = File(...)):
    """
    Parse a DM3/DM4 file:
    - extract all metadata tags
    - convert image data to 8-bit JPEG (percentile-normalised)
    Returns metadata dict + JPEG as base64 data URL + all raw tags.

    Requires: pip install ncempy numpy Pillow
    """
    try:
        import ncempy.io.dm as dm_module
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(
            500,
            f"Missing dependency: {exc}. Run: pip install ncempy numpy Pillow",
        )

    content = await file.read()
    if len(content) < 16:
        raise HTTPException(400, "File too small to be DM3/DM4")

    version = struct.unpack(">I", content[:4])[0]
    if version not in (3, 4):
        raise HTTPException(400, f"Not a DM3/DM4 file (header={version})")

    suffix = f".dm{version}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        with dm_module.fileDM(tmp_path) as reader:
            reader.parseHeader()
            all_tags = reader.allTags
            dataset = reader.getDataset(0)
            img_array = dataset["data"]

        metadata = _extract_key_tags(all_tags)

        # Normalise to 8-bit; percentile clipping removes hot/dead pixels
        arr = np.asarray(img_array, dtype=np.float32)
        vmin = float(np.percentile(arr, 0.5))
        vmax = float(np.percentile(arr, 99.5))
        if vmax > vmin:
            arr8 = ((arr - vmin) / (vmax - vmin) * 255).clip(0, 255).astype(np.uint8)
        else:
            arr8 = np.zeros_like(arr, dtype=np.uint8)

        pil_img = Image.fromarray(arr8)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")

        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=95)
        jpg_b64 = base64.b64encode(buf.getvalue()).decode()

        return {
            "filename": file.filename,
            "dm_version": f"DM{version}",
            "jpg_base64": f"data:image/jpeg;base64,{jpg_b64}",
            "metadata": metadata,
            "all_tags": _safe_tags(all_tags),
        }

    finally:
        os.unlink(tmp_path)


# ─── POST /api/dm3/annotate ──────────────────────────────────────────────────

class ParticleIn(BaseModel):
    bbox: List[int]                     # [x1, y1, x2, y2] in pixels
    diameter_nm: Optional[float] = None
    confidence: float = 0.0
    d_spacing_nm: Optional[float] = None


class AnnotateRequest(BaseModel):
    jpg_base64: str
    particles: List[ParticleIn]
    pixel_size_nm: float = 0.0          # nm/px — used to draw scale bar


@router.post("/annotate")
async def annotate_image(req: AnnotateRequest):
    """
    Draw bounding boxes on a JPEG.
    Colour-coded by confidence: green ≥0.9 · orange ≥0.6 · red <0.6
    Adds 100 nm scale bar when pixel_size_nm is known.

    Requires: pip install Pillow
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise HTTPException(500, "Pillow not installed. Run: pip install Pillow")

    raw = req.jpg_base64
    if "," in raw:
        raw = raw.split(",", 1)[1]
    img_bytes = base64.b64decode(raw)

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size

    for i, p in enumerate(req.particles):
        if len(p.bbox) != 4:
            continue
        x1, y1, x2, y2 = [int(v) for v in p.bbox]
        x1, x2 = max(0, x1), min(w - 1, x2)
        y1, y2 = max(0, y1), min(h - 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        conf = p.confidence
        color = "#22c55e" if conf >= 0.9 else ("#f97316" if conf >= 0.6 else "#ef4444")

        line_w = max(2, min(8, (x2 - x1) // 20))   # proportional to bbox size
        draw.rectangle([x1, y1, x2, y2], outline=color, width=line_w)

        d_str = f"{p.diameter_nm:.1f}nm" if p.diameter_nm is not None else ""
        label = f"#{i + 1} {d_str}".strip()
        draw.text((x1 + 3, max(y1 - 14, 2)), label, fill=color)

    # Scale bar — 100 nm, only if it fits within 30% of image width
    if req.pixel_size_nm > 0:
        scale_px = int(100 / req.pixel_size_nm)
        if 10 < scale_px < int(w * 0.3):
            bx, by = 20, h - 35
            draw.rectangle([bx, by, bx + scale_px, by + 5], fill="white")
            draw.text((bx, by - 16), "100 nm", fill="white")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"annotated_jpg_base64": f"data:image/jpeg;base64,{b64}"}


# ─── POST /api/dm3/save-session ──────────────────────────────────────────────

class FileToSave(BaseModel):
    new_name: str                           # base filename without extension
    ext: str                                # original extension: "dm3", "dm4"
    dm3_base64: Optional[str] = None        # original binary file
    jpg_base64: Optional[str] = None        # converted JPEG
    annotated_jpg_base64: Optional[str] = None
    analysis: Optional[dict] = None         # LLM JSON result


class ExifInfo(BaseModel):
    scientist: str = ""
    exp_id: str = ""
    sample_name: str = ""
    material: str = ""
    microscope: str = ""
    institution: str = "University Research Lab"
    # archiving fields
    synthesis_date: str = ""        # from NanoLab experiment record
    imaging_date: str = ""          # from DM3 DataBar — reliable
    sample_age_days: Optional[int] = None  # imaging_date - synthesis_date
    imaging_mode: str = ""          # hrtem | standard | overview
    pixel_size_nm: float = 0.0
    field_of_view_nm: str = ""      # "84 × 56 nm" or "1.27 × 0.85 µm"
    magnification_actual: float = 0.0
    exposure_number: int = 0


class SaveSessionRequest(BaseModel):
    folder_path: str            # absolute target path
    files: List[FileToSave]
    session_metadata: dict      # scope + naming + sample
    exif_info: Optional[ExifInfo] = None


@router.post("/save-session")
async def save_session(req: SaveSessionRequest):
    """
    Write files to disk in the TEM folder structure:
      {folder_path}/
        {name}.jpg                ← with EXIF if piexif available
        {name}_annotated.jpg
        {name}_analysis.json
        session_metadata.json
        dm3/
          {name}.dm3
    """
    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "Pillow not installed. Run: pip install Pillow")

    folder = req.folder_path
    dm3_folder = os.path.join(folder, "dm3")

    try:
        os.makedirs(folder, exist_ok=True)
        os.makedirs(dm3_folder, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Cannot create folder: {e}")

    # Session metadata
    meta_path = os.path.join(folder, "session_metadata.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(req.session_metadata, fh, indent=2, ensure_ascii=False)

    saved = [meta_path]

    for item in req.files:
        base = item.new_name
        ext = item.ext.lstrip(".")

        # Original DM3/DM4
        if item.dm3_base64:
            raw = item.dm3_base64
            if "," in raw:
                raw = raw.split(",", 1)[1]
            dm3_path = os.path.join(dm3_folder, f"{base}.{ext}")
            with open(dm3_path, "wb") as fh:
                fh.write(base64.b64decode(raw))
            saved.append(dm3_path)

        # JPEG with optional EXIF
        if item.jpg_base64:
            raw = item.jpg_base64
            if "," in raw:
                raw = raw.split(",", 1)[1]
            jpg_bytes = base64.b64decode(raw)

            if req.exif_info:
                try:
                    import piexif
                    ei = req.exif_info

                    # Compute sample_age_days if both dates present
                    age_days = ei.sample_age_days
                    if age_days is None and ei.synthesis_date and ei.imaging_date:
                        from datetime import date as _date
                        try:
                            d_synth  = _date.fromisoformat(ei.synthesis_date)
                            d_imaged = _date.fromisoformat(ei.imaging_date[:10])
                            age_days = (d_imaged - d_synth).days
                        except ValueError:
                            pass

                    description_parts = [f"Sample: {ei.sample_name}", f"Exp: {ei.exp_id}"]
                    if ei.material:
                        description_parts.append(f"Material: {ei.material}")
                    if ei.synthesis_date:
                        description_parts.append(f"Synthesized: {ei.synthesis_date}")
                    if ei.imaging_date:
                        description_parts.append(f"Imaged: {ei.imaging_date}")
                    if age_days is not None:
                        description_parts.append(f"Age: {age_days} days")
                    if ei.imaging_mode:
                        description_parts.append(f"Mode: {ei.imaging_mode}")
                    if ei.field_of_view_nm:
                        description_parts.append(f"FOV: {ei.field_of_view_nm}")

                    comment_data = ei.dict()
                    if age_days is not None:
                        comment_data["sample_age_days"] = age_days

                    exif_dict = {
                        "0th": {
                            piexif.ImageIFD.Artist:           ei.scientist.encode(),
                            piexif.ImageIFD.Copyright:        ei.institution.encode(),
                            piexif.ImageIFD.Make:             b"JEOL",
                            piexif.ImageIFD.Model:            ei.microscope.encode(),
                            piexif.ImageIFD.ImageDescription: " · ".join(description_parts).encode(),
                        },
                        "Exif": {
                            piexif.ExifIFD.UserComment: (
                                b"ASCII\x00\x00\x00" +
                                json.dumps(comment_data, ensure_ascii=False).encode()
                            ),
                        },
                    }
                    img = Image.open(io.BytesIO(jpg_bytes))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=95, exif=piexif.dump(exif_dict))
                    jpg_bytes = buf.getvalue()
                except Exception:
                    pass  # write without EXIF rather than fail

            jpg_path = os.path.join(folder, f"{base}.jpg")
            with open(jpg_path, "wb") as fh:
                fh.write(jpg_bytes)
            saved.append(jpg_path)

        # Annotated JPEG
        if item.annotated_jpg_base64:
            raw = item.annotated_jpg_base64
            if "," in raw:
                raw = raw.split(",", 1)[1]
            ann_path = os.path.join(folder, f"{base}_annotated.jpg")
            with open(ann_path, "wb") as fh:
                fh.write(base64.b64decode(raw))
            saved.append(ann_path)

        # Analysis JSON
        if item.analysis:
            an_path = os.path.join(folder, f"{base}_analysis.json")
            with open(an_path, "w", encoding="utf-8") as fh:
                json.dump(item.analysis, fh, indent=2, ensure_ascii=False)
            saved.append(an_path)

    return {"saved_files": saved, "folder": folder}
