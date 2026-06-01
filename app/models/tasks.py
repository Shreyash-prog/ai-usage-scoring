"""Task model (main spec §14). Loaded from YAML files in `tasks/` at startup."""

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    CODE = "code"
    DEBUG = "debug"
    DESIGN = "design"


class Task(BaseModel):
    id: str
    title: str
    type: TaskType = TaskType.CODE
    description_md: str
    starter_code: str = ""
    test_code: str | None = None
    time_limit_minutes: int = 15
    baseline_prompts: int = 3  # for the iteration heuristic (§9.2.3)
    expected_signals: dict = Field(default_factory=dict)
