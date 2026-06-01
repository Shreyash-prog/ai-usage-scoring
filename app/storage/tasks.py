"""TaskStore — loads task YAMLs at startup (main spec §14).

Malformed task files are skipped with a log warning rather than crashing the
server (§16); a session that references a missing/invalid task fails at creation.
"""

import logging
from pathlib import Path

import yaml

from app.models.tasks import Task

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self, tasks_dir: str) -> None:
        self._dir = Path(tasks_dir)
        self._tasks: dict[str, Task] = {}

    def load(self) -> None:
        """Parse every `*.yaml` in the tasks dir. Skip (and log) malformed files."""
        self._tasks.clear()
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                task = Task(**data)
            except Exception as exc:  # malformed YAML or schema violation
                logger.warning("Skipping malformed task file %s: %s", path.name, exc)
                continue
            self._tasks[task.id] = task
        logger.info("Loaded %d task(s): %s", len(self._tasks), ", ".join(self._tasks))

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        return list(self._tasks.values())

    def ids(self) -> list[str]:
        return list(self._tasks.keys())
