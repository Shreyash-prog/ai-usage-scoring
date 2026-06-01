"""WSManager — tracks WebSocket connections by (session_id, role) (main spec §12.1).

`role` is 'candidate' or 'dashboard'. Sends are best-effort: a send to a closed
socket is swallowed and the socket dropped, so one dead client never breaks a
broadcast to the others.
"""

import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    def __init__(self) -> None:
        self._rooms: dict[tuple[str, str], set[WebSocket]] = {}
        self._meta: dict[WebSocket, tuple[str, str]] = {}

    async def connect(self, ws: WebSocket, session_id: str, role: str) -> None:
        await ws.accept()
        key = (session_id, role)
        self._rooms.setdefault(key, set()).add(ws)
        self._meta[ws] = key

    def disconnect(self, ws: WebSocket) -> None:
        key = self._meta.pop(ws, None)
        if key is None:
            return
        room = self._rooms.get(key)
        if room:
            room.discard(ws)
            if not room:
                self._rooms.pop(key, None)

    async def send_to_session(self, session_id: str, role: str, msg: dict) -> None:
        room = self._rooms.get((session_id, role))
        if not room:
            return
        dead: list[WebSocket] = []
        for ws in list(room):
            try:
                await ws.send_json(msg)
            except Exception:  # closed/broken socket
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_dashboard(self, session_id: str, msg: dict) -> None:
        await self.send_to_session(session_id, "dashboard", msg)
