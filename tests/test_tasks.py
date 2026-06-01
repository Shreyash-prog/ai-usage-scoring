"""TaskStore tests (main spec §14, §16): load valid tasks, skip malformed."""

from app.storage.tasks import TaskStore


def test_loads_real_task_dir() -> None:
    store = TaskStore("./tasks")
    store.load()
    assert "001" in store.ids()
    task = store.get("001")
    assert task is not None
    assert task.title
    assert task.starter_code


def test_skips_malformed_yaml(tmp_path) -> None:
    (tmp_path / "good.yaml").write_text(
        'id: "G"\ntitle: "Good"\ndescription_md: "x"\n', encoding="utf-8"
    )
    (tmp_path / "bad.yaml").write_text("id: 'B'\ntitle:\n  - not\n  - a task\n", encoding="utf-8")
    store = TaskStore(str(tmp_path))
    store.load()
    assert store.ids() == ["G"]  # malformed file skipped, not fatal
