"""Queue, resource, cancellation and HTTP contracts using real subprocesses."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from Energyhub.job_manager import EnergyJobManager, TERMINAL_STATES

TEST_ROOT = Path(__file__).resolve().parent
FAKE_CAPABILITIES = {
    "pyscf_version": "fixture",
    "basis": ["3zeta", "4zeta", "CBS"],
    "methods": [
        {"name": name, "available": name != "CCSDT(Q)", "closed_shell": name != "CCSDT(Q)", "open_shell": name != "CCSDT(Q)",
         "reason": "Perturbative quadruples are unavailable" if name == "CCSDT(Q)" else None}
        for name in ("CCSD", "CCSD(T)", "CCSDT", "CCSDT(Q)")
    ],
}


def process_alive(pid):
    """Treat a reaped-or-zombie test worker as no longer executing."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".jobs-test-", dir=TEST_ROOT)
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {"ENERGYHUB_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.manager = None
        self.owned_pids = []
        self.make_manager()

    def tearDown(self):
        if self.manager is not None:
            self.manager.close()
        # Cleanup is limited to descendants whose PID file the fixture wrote.
        for pid in self.owned_pids:
            if process_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.env.stop()
        self.temporary.cleanup()

    def make_manager(self, slots=2, memory=128):
        if self.manager is not None:
            self.manager.close()
        self.manager = EnergyJobManager(data_dir=self.root / "jobs", pool_size=slots, memory_pool_mb=memory, worker_module="Energyhub.tests.fake_worker")
        self.manager._capabilities = FAKE_CAPABILITIES
        return self.manager

    def wait(self, predicate, message="condition", timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {message}")

    def terminal(self, job):
        return self.wait(lambda: self.manager.get(job.task_id) if self.manager.get(job.task_id).state in TERMINAL_STATES else None, f"terminal job {job.task_id}")

    def submit(self, behavior=None, **options):
        settings = {"method": "CCSD", "basis": "3zeta", "pool_size": 1, "task_threads": 1, "memory_mb": 64}
        settings.update(options)
        return self.manager.submit(json.dumps(behavior or {}).encode(), b"", settings)

    def started(self, job):
        return self.wait(lambda: (job.directory / "worker.pid").exists(), "worker startup")


class JobManagerTests(ManagerCase):
    def test_success_preserves_options_report_and_empty_reference(self):
        job = self.submit(method="CCSD(T)", basis="CBS", task_threads=1)
        completed = self.terminal(job)
        self.assertEqual(completed.state, "completed", completed.error)
        self.assertEqual(completed.output_path.read_text(), "1 H2 -1.100000000000\n")
        self.assertEqual((job.directory / "input.ref").stat().st_size, 0)
        options = json.loads((job.directory / "received-options.json").read_text())
        self.assertEqual(options["method"], "CCSD(T)")
        self.assertEqual(options["basis"], "CBS")
        self.assertEqual(options["memory_pool_mb"], 64)
        self.assertTrue(completed.report["complete"])
        self.assertIsNotNone(completed.finished_at)
        self.assertEqual(self.manager.config()["memory_used_mb"], 0)
        self.assertEqual(self.manager.config()["slots_used"], 0)

    def test_global_slots_prevent_nested_pool_oversubscription(self):
        gate = self.root / "release"
        first = self.submit({"gate": str(gate)}, pool_size=2, memory_mb=32)
        self.started(first)
        second = self.submit()
        self.wait(lambda: second.state == "queued")
        self.assertIsNone(second.started_at)
        self.assertEqual(self.manager.config()["slots_used"], 2)
        self.assertFalse((second.directory / "worker.pid").exists())
        gate.touch()
        self.assertEqual(self.terminal(first).state, "completed")
        self.assertEqual(self.terminal(second).state, "completed")

    def test_global_memory_blocks_even_when_slot_is_available(self):
        self.make_manager(slots=3, memory=128)
        gate = self.root / "release"
        first = self.submit({"gate": str(gate)})
        second = self.submit({"gate": str(gate)})
        self.started(first)
        self.started(second)
        third = self.submit()
        self.wait(lambda: third.state == "queued")
        self.assertEqual(self.manager.config()["slots_used"], 2)
        self.assertEqual(self.manager.config()["memory_used_mb"], 128)
        self.assertIsNone(third.started_at)
        gate.touch()
        for job in (first, second, third):
            self.assertEqual(self.terminal(job).state, "completed")
        self.assertEqual(self.manager.config()["memory_used_mb"], 0)

    def test_queued_cancel_never_launches_worker(self):
        self.make_manager(slots=1, memory=128)
        gate = self.root / "release"
        first = self.submit({"gate": str(gate)})
        self.started(first)
        queued = self.submit()
        self.manager.cancel(queued.task_id)
        self.assertEqual(queued.state, "cancelled")
        self.assertIsNotNone(queued.finished_at)
        self.assertIsNone(queued.started_at)
        gate.touch()
        self.terminal(first)
        self.assertFalse((queued.directory / "request.json").exists())
        self.assertEqual(self.manager.config()["slots_used"], 0)

    def test_running_cancel_stops_worker_and_releases_reservations(self):
        job = self.submit({"gate": str(self.root / "never")})
        self.started(job)
        pid = int((job.directory / "worker.pid").read_text())
        self.owned_pids.append(pid)
        self.manager.cancel(job.task_id)
        self.assertEqual(self.terminal(job).state, "cancelled")
        self.wait(lambda: not process_alive(pid), "terminated worker")
        self.assertFalse(job.output_path.exists())
        self.assertEqual(self.manager.config()["slots_used"], 0)
        self.assertEqual(self.manager.config()["memory_used_mb"], 0)
        self.assertEqual(self.terminal(self.submit()).state, "completed")

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "POSIX process groups required")
    def test_cancel_kills_descendant_even_if_it_ignores_sigterm(self):
        job = self.submit({"gate": str(self.root / "never"), "spawn_child": True, "child_ignores_term": True})
        child_file = job.directory / "child.pid"
        self.wait(lambda: child_file.exists(), "descendant startup")
        pid = int(child_file.read_text())
        self.owned_pids.append(pid)
        self.manager.cancel(job.task_id)
        self.assertEqual(self.terminal(job).state, "cancelled")
        self.wait(lambda: not process_alive(pid), "terminated descendant", timeout=5)

    def test_failed_and_incomplete_results_are_never_downloadable(self):
        for behavior in ({"failure": "fixture convergence error"}, {"incomplete": True}, {"exit_without_report": True}):
            with self.subTest(behavior=behavior):
                job = self.submit(behavior)
                failed = self.terminal(job)
                self.assertEqual(failed.state, "failed")
                self.assertTrue(failed.error)
                self.assertFalse(failed.output_path.exists())
                self.assertNotIn("download_url", failed.as_dict())
                self.assertEqual(self.manager.config()["memory_used_mb"], 0)

    def test_invalid_resources_and_unavailable_q_rejected_before_creating_job(self):
        cases = [
            {"pool_size": 0}, {"pool_size": "1.5"}, {"pool_size": True},
            {"task_threads": 0}, {"task_threads": -1}, {"task_threads": "nan"},
            {"memory_mb": 0}, {"memory_mb": "inf"}, {"pool_size": 3},
            {"memory_pool_mb": 129}, {"pool_size": 2, "memory_mb": 64, "memory_pool_mb": 64},
            {"method": "CCSDT(Q)"},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.submit(**options)
        self.assertEqual(self.manager._jobs, {})
        self.assertEqual(list((self.root / "jobs").glob("*/task.json")), [])

    def test_upload_failure_does_not_leave_job_directory(self):
        with self.assertRaises(ValueError):
            self.manager.submit(b"", b"", {"memory_mb": 64})
        self.assertEqual([path for path in (self.root / "jobs").iterdir() if path.is_dir()], [])

    def test_close_cancels_running_and_queued_jobs_before_returning(self):
        self.make_manager(slots=1, memory=128)
        first = self.submit({"gate": str(self.root / "never")})
        self.started(first)
        pid = int((first.directory / "worker.pid").read_text())
        self.owned_pids.append(pid)
        queued = self.submit()
        self.manager.close()
        self.assertFalse(process_alive(pid))
        self.assertEqual(first.state, "cancelled")
        self.assertEqual(queued.state, "cancelled")
        self.assertFalse((queued.directory / "worker.pid").exists())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.submit()

    def test_restart_marks_stale_running_and_queued_failed(self):
        successful = self.terminal(self.submit())
        self.manager.close()
        self.manager = None
        for state in ("queued", "running", "cancelling"):
            identifier = uuid.uuid4().hex
            directory = self.root / "jobs" / identifier
            directory.mkdir()
            data = {"task_id": identifier, "state": state, "method": "CCSD", "basis": "3zeta", "pool_size": 1,
                    "task_threads": 1, "memory_mb": 64, "memory_pool_mb": 64}
            (directory / "task.json").write_text(json.dumps(data))
        self.make_manager()
        self.assertEqual(self.manager.get(successful.task_id).state, "completed")
        for job in self.manager._jobs.values():
            if job.task_id == successful.task_id:
                continue
            self.assertEqual(job.state, "failed")
            self.assertIn("Service stopped", job.error)
            self.assertIsNotNone(job.finished_at)

    @unittest.skipUnless(os.name == "posix", "POSIX file locks required")
    def test_second_scheduler_cannot_overbook_same_data_directory(self):
        with self.assertRaisesRegex(RuntimeError, "already has an Energyhub scheduler"):
            EnergyJobManager(data_dir=self.root / "jobs", worker_module="Energyhub.tests.fake_worker")


@unittest.skipUnless(importlib.util.find_spec("flask"), "optional Flask extra is unavailable")
class HttpApiTests(ManagerCase):
    def setUp(self):
        super().setUp()
        from Energyhub.api import create_app
        self.app = create_app(self.manager)
        self.app.testing = True
        self.client = self.app.test_client()

    def post(self, behavior=None, **options):
        data = {"tgz": (io.BytesIO(json.dumps(behavior or {}).encode()), "geometry.tgz"),
                "ref": (io.BytesIO(b""), "blank.ref"), "memory_mb": "64"}
        data.update({key: str(value) for key, value in options.items()})
        return self.client.post("/api/energyhub/jobs", data=data)

    def test_upload_status_and_result_download(self):
        response = self.post(method="CCSD(T)", basis="CBS")
        self.assertEqual(response.status_code, 202, response.get_json())
        task_id = response.get_json()["task_id"]
        job = self.manager.get(task_id)
        self.assertEqual(self.terminal(job).state, "completed", job.error)
        state = self.client.get(f"/api/energyhub/jobs/{task_id}")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.get_json()["state"], "completed")
        result = self.client.get(f"/api/energyhub/jobs/{task_id}/result")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data, b"1 H2 -1.100000000000\n")
        self.assertIn("attachment", result.headers["Content-Disposition"])
        result.close()

    def test_cancel_endpoint_and_unfinished_download(self):
        response = self.post({"gate": str(self.root / "never")})
        self.assertEqual(response.status_code, 202)
        task_id = response.get_json()["task_id"]
        job = self.manager.get(task_id)
        self.started(job)
        self.assertEqual(self.client.get(f"/api/energyhub/jobs/{task_id}/result").status_code, 409)
        cancel = self.client.post(f"/api/energyhub/jobs/{task_id}/cancel")
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(self.terminal(job).state, "cancelled")
        again = self.client.delete(f"/api/energyhub/jobs/{task_id}")
        self.assertEqual(again.get_json()["state"], "cancelled")
        self.assertEqual(self.client.get(f"/api/energyhub/jobs/{task_id}/result").status_code, 409)

    def test_invalid_upload_options_and_missing_task(self):
        self.assertEqual(self.client.post("/api/energyhub/jobs", data={}).status_code, 400)
        for options in ({"method": "CCSDT(Q)"}, {"task_threads": "1.5"}, {"memory_mb": "NaN"}, {"pool_size": "0"}):
            with self.subTest(options=options):
                response = self.post(**options)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        missing = "0" * 32
        self.assertEqual(self.client.get(f"/api/energyhub/jobs/{missing}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/energyhub/jobs/{missing}/cancel").status_code, 404)
        self.assertEqual(self.client.get(f"/api/energyhub/jobs/{missing}/result").status_code, 404)

    def test_resource_config_changes_only_when_idle(self):
        initial = self.client.get("/api/energyhub/config")
        self.assertEqual(initial.get_json()["pool_size"], 2)
        updated = self.client.put("/api/energyhub/config", json={"pool_size": 3, "memory_pool_mb": 256})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["pool_size"], 3)
        invalid = self.client.put("/api/energyhub/config", json={"pool_size": 0})
        self.assertEqual(invalid.status_code, 400)
        response = self.post({"gate": str(self.root / "never")})
        job = self.manager.get(response.get_json()["task_id"])
        self.started(job)
        busy = self.client.put("/api/energyhub/config", json={"pool_size": 1})
        self.assertEqual(busy.status_code, 409)
        self.manager.cancel(job.task_id)
        self.terminal(job)
        self.assertEqual(self.client.put("/api/energyhub/config", json={"pool_size": 1}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
