"""FastAPI app: routes, lifespan wiring, candidate WebSocket (main spec §1, §13).

Day 3 scope: candidate flow end-to-end (task -> chat stream -> run -> output).
EventBus/LiveScorer (Day 4), dashboard WS (Day 5), and post-hoc scoring (Day 6)
are not wired yet.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.llm.chat_client import OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient
from app.models.events import EventType
from app.sandbox.runner import Sandbox
from app.storage import sessions as session_store
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.tasks import TaskStore
from app.ws.candidate import CandidateDeps, CandidateSession
from app.ws.manager import WSManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "static"
_PROMPTS_DIR = Path(__file__).parent / "llm" / "prompts"


class SessionCreate(BaseModel):
    candidate_name: str
    task_sequence: list[str] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db = Database(settings.db_path)
    await db.connect()

    tasks = TaskStore(settings.tasks_dir)
    tasks.load()

    chat = OpenAIChatClient(
        settings.openai_api_key, settings.openai_chat_model, settings.openai_chat_timeout_s
    )
    judge = AnthropicJudgeClient(
        settings.anthropic_api_key,
        settings.anthropic_judge_model,
        settings.anthropic_judge_timeout_s,
        settings.anthropic_judge_max_retries,
    )
    system_prompt = (_PROMPTS_DIR / "system_chat.txt").read_text(encoding="utf-8")

    # Startup health (PROVIDER_SPEC §P.3.5). Server starts regardless.
    health = {"openai": await chat.health(), "anthropic": await judge.health()}
    logger.info("Startup health: %s", health)

    app.state.db = db
    app.state.event_logger = EventLogger(db)
    app.state.tasks = tasks
    app.state.chat = chat
    app.state.judge = judge
    app.state.ws_manager = WSManager()
    app.state.deps = CandidateDeps(
        db=db,
        logger=app.state.event_logger,
        sandbox=Sandbox(),
        chat=chat,
        tasks=tasks,
        ws_manager=app.state.ws_manager,
        system_prompt=system_prompt,
    )
    app.state.health = health
    try:
        yield
    finally:
        await db.close()


app = FastAPI(title="AI Usage Scoring", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/candidate")


@app.get("/candidate")
async def candidate_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "candidate.html")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> str:
    path = _STATIC_DIR / "dashboard.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "<h1>Dashboard</h1><p>Coming on Day 5.</p>"


@app.post("/api/session")
async def create_session(body: SessionCreate) -> dict:
    if not app.state.health.get("openai"):
        raise HTTPException(status_code=503, detail="Chat AI unavailable")

    tasks: TaskStore = app.state.tasks
    sequence = body.task_sequence or tasks.ids()
    if not sequence:
        raise HTTPException(status_code=400, detail="No tasks available")
    missing = [t for t in sequence if tasks.get(t) is None]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown task(s): {missing}")

    session_id = await session_store.create_session(app.state.db, body.candidate_name, sequence)
    await app.state.event_logger.write(
        session_id,
        EventType.SESSION_STARTED,
        {"candidate_name": body.candidate_name, "task_sequence": sequence},
    )
    return {
        "session_id": session_id,
        "candidate_name": body.candidate_name,
        "task_sequence": sequence,
        "current_task_idx": 0,
    }


@app.get("/api/session/{session_id}")
async def get_session(session_id: str) -> dict:
    session = await session_store.get_session(app.state.db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.model_dump()


@app.get("/api/session/{session_id}/events")
async def get_events(session_id: str, from_seq: int = 0) -> list[dict]:
    events = await app.state.event_logger.get_session_events(session_id, from_seq=from_seq)
    return [e.model_dump() for e in events]


@app.get("/api/session/{session_id}/scores")
async def get_scores(session_id: str, phase: str = "final") -> list[dict]:
    async with app.state.db.read() as conn:
        cur = await conn.execute(
            "SELECT session_id, task_id, dimension, phase, score, confidence, evidence, "
            "updated_at FROM scores WHERE session_id = ? AND phase = ?",
            (session_id, phase),
        )
        rows = await cur.fetchall()
    cols = [
        "session_id",
        "task_id",
        "dimension",
        "phase",
        "score",
        "confidence",
        "evidence",
        "updated_at",
    ]
    out = []
    for r in rows:
        record = dict(zip(cols, r, strict=True))
        record["evidence"] = json.loads(record["evidence"])
        out.append(record)
    return out


@app.get("/api/tasks")
async def list_tasks() -> list[dict]:
    return [t.model_dump() for t in app.state.tasks.all()]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = app.state.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump()


@app.get("/api/health")
async def health() -> dict:
    db_ok = True
    try:
        async with app.state.db.read() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        db_ok = False
    return {**app.state.health, "db": db_ok}


@app.websocket("/ws/session/{session_id}")
async def candidate_ws(ws: WebSocket, session_id: str) -> None:
    session = await session_store.get_session(app.state.db, session_id)
    if session is None:
        await ws.close(code=4404)
        return
    manager: WSManager = app.state.ws_manager
    await manager.connect(ws, session_id, "candidate")
    try:
        await CandidateSession(ws, session_id, app.state.deps).run()
    finally:
        manager.disconnect(ws)
