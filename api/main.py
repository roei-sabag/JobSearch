"""
api/main.py
-----------
FastAPI application entrypoint.

Run locally with:
    uvicorn api.main:app --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routers import jobs as jobs_router
from db.init_db import init_models

WORKDIR = Path(__file__).resolve().parent.parent
OUTPUT_CVS_DIR = WORKDIR / "output" / "cvs"
STATIC_DIR = WORKDIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure tables exist on startup (idempotent) -- zero manual setup needed.
    await init_models()
    # Ensure output/cvs exists so the static mount below never 404s on a
    # missing directory before the first tailoring run has happened.
    OUTPUT_CVS_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Autonomous Job Search & Resume Tailoring API",
    description="Manual job ingestion port (Module B), intelligent raw-text ingestion, "
                 "and job data foundation.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(jobs_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Serve generated tailored-CV PDFs so the frontend can link/download them
# directly, e.g. GET /files/cvs/tailored_job_2.pdf
app.mount("/files/cvs", StaticFiles(directory=str(OUTPUT_CVS_DIR)), name="tailored_cvs")

# Serve the minimalist one-click web UI (static/index.html + assets).
# Mounted LAST, at "/", so it never shadows the API routes registered above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static_ui")
