from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI(title="NanoLab API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Endpointy API ---
from routers import tem, experiments, reagents, bottles, llm, dm3, tem_analysis
app.include_router(tem.router, prefix="/api")
app.include_router(experiments.router, prefix="/api")
app.include_router(reagents.router, prefix="/api")
app.include_router(bottles.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(dm3.router)           # prefix="/api/dm3" defined in router
app.include_router(tem_analysis.router)  # prefix="/api/llm" defined in router

# --- Health check ---
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}

# --- Frontend (musi być na końcu) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(BASE_DIR, "..", "frontend")

app.mount("/app/scientist", StaticFiles(directory=os.path.join(FRONTEND, "scientist-app"), html=True), name="scientist")
app.mount("/app/operator", StaticFiles(directory=os.path.join(FRONTEND, "operator-app"), html=True), name="operator")
