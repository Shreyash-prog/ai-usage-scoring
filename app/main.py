"""FastAPI app: routes, lifespan wiring, candidate WebSocket (main spec §1, §13).

Day 3 scope: candidate flow end-to-end (task -> chat stream -> run -> output).
EventBus/LiveScorer (Day 4), dashboard WS (Day 5), and post-hoc scoring (Day 6)
are not wired yet.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.bus import EventBus
from app.config import settings
from app.llm.chat_client import OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient
from app.models.events import EventType, PersistedEvent
from app.sandbox.runner import Sandbox
from app.scoring.live import LiveScorer
from app.scoring.posthoc import PostHocScorer
from app.storage import sessions as session_store
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.scores import ScoreStore
from app.storage.tasks import TaskStore
from app.ws.candidate import CandidateDeps, CandidateSession
from app.ws.dashboard import DashboardDeps, DashboardSession
from app.ws.manager import WSManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "static"
_PROMPTS_DIR = Path(__file__).parent / "llm" / "prompts"


class SessionCreate(BaseModel):
    candidate_name: str
    task_sequence: list[str] | None = None


def _dashboard_forwarder(ws_manager: WSManager, session_id: str):
    """A bus subscriber that mirrors each persisted event to dashboard watchers (§12.3)."""

    async def forward(event: PersistedEvent) -> None:
        await ws_manager.broadcast_dashboard(
            session_id, {"type": "event", "event": event.model_dump(mode="json")}
        )

    return forward


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

    ws_manager = WSManager()
    event_logger = EventLogger(db)
    bus = EventBus()
    app.state.db = db
    app.state.event_logger = event_logger
    app.state.tasks = tasks
    app.state.chat = chat
    app.state.judge = judge
    app.state.ws_manager = ws_manager
    app.state.bus = bus
    scores = ScoreStore(db)
    app.state.scores = scores
    app.state.live_scorer = LiveScorer(scores, ws_manager, tasks)
    app.state.dashboard_deps = DashboardDeps(
        logger=event_logger, scores=scores, ws_manager=ws_manager
    )
    posthoc = PostHocScorer(db, event_logger, judge, scores, tasks, ws_manager, bus)
    app.state.posthoc = posthoc
    app.state.deps = CandidateDeps(
        db=db,
        logger=event_logger,
        sandbox=Sandbox(),
        chat=chat,
        tasks=tasks,
        ws_manager=ws_manager,
        bus=bus,
        posthoc=posthoc,
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


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


@app.post("/api/session")
async def create_session(body: SessionCreate) -> dict:
    if not app.state.health.get("openai"):
        raise HTTPException(status_code=503, detail="Chat AI unavailable")

    tasks: TaskStore = app.state.tasks
    sequence = body.task_sequence or settings.default_task_sequence
    if not sequence:
        raise HTTPException(status_code=400, detail="No tasks available")
    missing = [t for t in sequence if tasks.get(t) is None]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown task(s): {missing}")

    session_id = await session_store.create_session(app.state.db, body.candidate_name, sequence)

    # Wire scoring + dashboard fan-out for this session, then publish session.started.
    bus: EventBus = app.state.bus
    bus.subscribe(session_id, app.state.live_scorer.handle_event)
    bus.subscribe(session_id, _dashboard_forwarder(app.state.ws_manager, session_id))
    started = await app.state.event_logger.write(
        session_id,
        EventType.SESSION_STARTED,
        {"candidate_name": body.candidate_name, "task_sequence": sequence},
    )
    await bus.publish(started)
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
    return await app.state.scores.list_scores(session_id, phase)


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    rows = await session_store.list_sessions(app.state.db)
    return [r.model_dump() for r in rows]


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


@app.websocket("/ws/dashboard/{session_id}")
async def dashboard_ws(ws: WebSocket, session_id: str) -> None:
    session = await session_store.get_session(app.state.db, session_id)
    if session is None:
        await ws.close(code=4404)
        return
    manager: WSManager = app.state.ws_manager
    await manager.connect(ws, session_id, "dashboard")
    try:
        await DashboardSession(ws, session_id, app.state.dashboard_deps).run()
    finally:
        manager.disconnect(ws)
