"""RTL SIGINT SCANNER — backend app factory."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from db import import_channels, init_db
from routers import agent, bandplan, channels, recordings, status
from services import transcription


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    import_channels()
    transcription.start_worker()
    yield
    transcription.stop_worker()


app = FastAPI(title="RTL SIGINT SCANNER", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(status.router)
app.include_router(channels.router)
app.include_router(recordings.router)
app.include_router(agent.router)
app.include_router(bandplan.router)

TEMPLATES = Path("templates")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((TEMPLATES / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})
