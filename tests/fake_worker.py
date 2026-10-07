"""Deterministic subprocess fixture; never used by production calculations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def main():
    if sys.argv[1] == "child":
        pid_file = Path(sys.argv[2])
        if len(sys.argv) > 3 and sys.argv[3] == "ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pid_file.write_text(str(os.getpid()))
        while True:
            time.sleep(0.05)

    mode, request_file, report_file = sys.argv[1:]
    if mode != "reference":
        raise ValueError("This fixture only implements the reference worker protocol")
    request = json.loads(Path(request_file).read_text())
    behavior = json.loads(Path(request["tgz"]).read_text())
    directory = Path(report_file).parent
    (directory / "worker.pid").write_text(str(os.getpid()))
    write_json(directory / "received-options.json", request["options"])
    write_json(directory / "progress.json", {"status": "running", "completed": 0, "total": 1})
    child = None
    if behavior.get("spawn_child"):
        command = [sys.executable, "-m", "Energyhub.tests.fake_worker", "child", str(directory / "child.pid")]
        if behavior.get("child_ignores_term"):
            command.append("ignore-term")
        child = subprocess.Popen(command)
    gate = behavior.get("gate")
    while gate and not Path(gate).exists():
        time.sleep(0.02)
    if behavior.get("exit_without_report"):
        raise SystemExit(3)
    if behavior.get("failure"):
        write_json(report_file, {"ok": False, "error": behavior["failure"]})
        raise SystemExit(1)
    output = Path(request["output"])
    output.write_text("1 H2 -1.100000000000\n", encoding="utf-8")
    report = {
        "complete": not behavior.get("incomplete"),
        "output": str(output),
        "molecularEnergies": [{"name": "H2", "energy_hartree": -1.1, "converged": True}],
    }
    write_json(report_file, {"ok": True, "result": report})
    if child is not None:
        child.terminate()
        child.wait(timeout=2)


if __name__ == "__main__":
    main()
