"""
The web backend for Alpha Trading.

Start it from the project folder with:
    uvicorn app.server:app --reload

Then open http://localhost:8000 in your browser.

Endpoints:
    GET /              -> the web page
    GET /api/watchlist -> your current watchlist (from config/watchlist.yaml)
    GET /api/check     -> runs a live check and returns the results
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from src.engine import evaluate_watchlist, load_watchlist

app = FastAPI(title="Alpha Trading")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/watchlist")
def api_watchlist():
    return load_watchlist()


@app.get("/api/check")
def api_check():
    return JSONResponse(evaluate_watchlist())


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
