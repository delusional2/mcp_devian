import pytest

from mcp_devian import jobs, mcp_server
from mcp_devian.config import Config, save_config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOB_FILE", tmp_path / "job.json")
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))


class FakeProc:
    pid = 4242


def test_start_writes_running_then_pid(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    job = jobs.start("toyhouse", now=1000)
    assert job["state"] == "running" and job["pid"] == 4242
    # Пока идёт — второй раз не запускаем.
    assert jobs.start("toyhouse", now=1001)["pid"] == 4242


def test_child_finishing_first_is_not_overwritten(monkeypatch):
    def fast_child(cmd, **kw):
        job_id = cmd[cmd.index("--job") + 1]
        jobs.finish(job_id, ["deviantart: ок"])  # ребёнок успел раньше родителя
        return FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fast_child)
    job = jobs.start("all", now=1000)
    assert job["state"] == "done" and job["lines"] == ["deviantart: ок"]


def test_dead_process_marked_failed(monkeypatch):
    monkeypatch.setattr(jobs, "_alive", lambda pid: False)
    jobs._write({"state": "running", "pid": 1, "started": 0})
    assert jobs.current(now=100)["state"] == "failed"


def test_stale_job_marked_failed(monkeypatch):
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    jobs._write({"state": "running", "pid": 1, "started": 0})
    assert jobs.current(now=jobs.STALE_AFTER_S + 1)["state"] == "failed"


def test_check_now_requires_setup():
    assert "get_settings" in mcp_server.check_now()


def test_check_now_reports_running_then_done(monkeypatch):
    cfg = Config()
    cfg.configured = True
    save_config(cfg)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "wait", lambda timeout_s=45, job_id=None: jobs.current())
    assert "ещё идёт" in mcp_server.check_now("toyhouse")
    jobs.finish(jobs.current()["id"], ["toyhouse: просмотрено 25, новых 3, подходит из новых 1"])
    assert "просмотрено 25" in mcp_server.check_status()


def test_finish_ignores_other_job():
    jobs._write({"id": "new", "state": "running", "pid": 1, "started": 0})
    assert not jobs.finish("old", ["старый результат"])
    assert jobs._read()["state"] == "running"


def test_parallel_starts_spawn_one_child(monkeypatch):
    import threading

    spawned = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or FakeProc())
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        jobs.start("all")

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(spawned) == 1 and jobs.current()["pid"] == 4242


def test_check_now_for_other_running_source(monkeypatch):
    cfg = Config()
    cfg.configured = True
    save_config(cfg)
    monkeypatch.setattr(jobs, "_alive", lambda pid: True)
    jobs._write({"id": "x", "state": "running", "source": "toyhouse", "pid": 1, "started": __import__("time").time()})
    out = mcp_server.check_now("deviantart")
    assert "уже идёт проверка toyhouse" in out and "deviantart ещё не проверялся" in out
