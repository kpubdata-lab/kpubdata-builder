#!/usr/bin/env python3
"""Stop a real Builder while it is building, and start it again (#1118).

The unit tests call ``begin_shutdown`` and ``drain_builds`` on a service object. Nothing
sent a signal to a process, so nothing showed that ``docker stop`` reaches ``serve``,
that the container leaves by itself before Docker's stop timeout, or what a restart on
the same ``/data`` says about the jobs that were there. This starts the production
compose file with the Builder image and does it three times:

1. **Drain.** One build is running and two are queued behind it (one build slot).
   ``docker compose stop`` sends SIGTERM. A submission the server was already reading
   is answered 503 ``shutting_down`` and leaves nothing; a new connection is not
   served. The two queued jobs end ``failed`` with the ``interrupted`` reason and never
   start; the running build finishes after the signal, within the default grace period;
   the container exits by itself with code 0. After a start on the same volume all of
   that is still what the API says, and nothing runs again.
2. **Grace expired.** The same with a grace period of one second: the running build is
   asked to stop, ends ``cancelled`` with an event that says the server was shutting
   down, and the container still exits by itself with code 0.
3. **Killed.** SIGKILL instead: no drain happens. At the next start the build that was
   running is ``failed`` with ``credentials_required``, and is not run again.

The builds read an uploaded CSV, so no provider key and no network outside the host is
needed; the file is large enough that a build takes several seconds.

The deployment is multi-user (``ENFORCE_OWNERSHIP``), because that is the mode in which
a restart marks interrupted runs. CI has no OIDC issuer, so requests carry the service
key. The compose override also sets the grace period, which the production compose
file leaves at its default.

Not checked here: that a job's provider keys are gone from memory (nothing outside the
process can see them), a build that fetches from a provider, and two builds running at
once.

Usage:
    python3 scripts/shutdown_drain_smoke.py --image kpubdata-builder:ci
    python3 scripts/shutdown_drain_smoke.py --local   # the installed `serve`, no Docker
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"
PROJECT = "shutdown-drain-smoke"
SERVICE_KEY = "smoke-" + "k" * 40
TERMINAL = ("succeeded", "failed", "cancelled")

# Compose fills SMOKE_SHUTDOWN_GRACE_SECONDS in from the environment this script sets;
# empty leaves the server's default.
_OVERRIDE = """\
services:
  builder:
    environment:
      ENFORCE_OWNERSHIP: "true"
      KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS: ${SMOKE_SHUTDOWN_GRACE_SECONDS:-}
"""

# What the smoke sets for the server, in either mode. One build slot, so that the
# second and third submissions wait; the per-owner limit must let all three in.
_SERVER_ENV = {
    "KPUBDATA_BUILDER_API_KEY": SERVICE_KEY,
    "KPUBDATA_BUILDER_MAX_BUILDS": "1",
    "KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER": "3",
}

Json = dict[str, object]


def stop_timeout_seconds() -> float:
    """The compose file's ``stop_grace_period``: how long Docker waits before SIGKILL."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    found = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*$", text, flags=re.MULTILINE)
    if found is None:
        raise RuntimeError("docker-compose.prod.app.yml has no stop_grace_period in seconds")
    return float(found.group(1))


class Compose:
    """The Builder service of the production compose file, in a container."""

    def __init__(self, image: str, directory: Path) -> None:
        self._override = directory / "override.yml"
        self._override.write_text(_OVERRIDE, encoding="utf-8")
        os.environ.update(
            _SERVER_ENV,
            BUILDER_IMAGE=image,
            # The host picks the port; `port` below asks which.
            BUILDER_BIND="127.0.0.1:0",
        )
        self._stopping: subprocess.Popen[str] | None = None

    def _compose(self, *args: str) -> list[str]:
        files = ["-f", str(COMPOSE_FILE), "-f", str(self._override)]
        return ["docker", "compose", "-p", PROJECT, *files, *args]

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self._compose(*args), check=False, capture_output=True, text=True)

    def start(self, grace: str) -> str:
        """Start the container (again), on the same volume. Returns the base URL."""
        os.environ["SMOKE_SHUTDOWN_GRACE_SECONDS"] = grace
        started = self._run("up", "-d", "--no-build", "builder")
        if started.returncode != 0:
            raise RuntimeError(f"compose up failed: {(started.stdout + started.stderr)[-600:]}")
        published = self._run("port", "builder", "8000").stdout.strip()
        return f"http://127.0.0.1:{published.rsplit(':', 1)[-1]}"

    def begin_stop(self) -> None:
        # `stop` sends SIGTERM and SIGKILL after the service's stop_grace_period.
        self._stopping = subprocess.Popen(
            self._compose("stop", "builder"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def wait_stopped(self, timeout: float) -> int | None:
        """The container's exit code, or None when it is still running at the timeout."""
        if self._stopping is not None:
            try:
                self._stopping.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None
            self._stopping = None
        return self._exit_code()

    def _exit_code(self) -> int | None:
        container = self._run("ps", "-a", "-q", "builder").stdout.strip()
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", container],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.split()
        if len(state) != 2 or state[0] == "true":
            return None
        return int(state[1])

    def kill(self) -> None:
        self._run("kill", "-s", "SIGKILL", "builder")

    def logs(self) -> str:
        logs = self._run("logs", "--no-color", "--tail", "80", "builder")
        return logs.stdout + logs.stderr

    def close(self) -> None:
        self._run("down", "--volumes", "--remove-orphans")


class Local:
    """The installed ``kpubdata-builder serve`` as a process: the same signals, no Docker."""

    def __init__(self, directory: Path) -> None:
        self._data = directory / "data"
        self._data.mkdir()
        self._log = directory / "serve.log"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, grace: str) -> str:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = {
            **os.environ,
            **_SERVER_ENV,
            "ENFORCE_OWNERSHIP": "true",
            "KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS": grace,
            "KPUBDATA_BUILDER_WAREHOUSE": str(self._data / "warehouse"),
        }
        executable = shutil.which("kpubdata-builder", path=str(Path(sys.executable).parent))
        if executable is None:
            raise RuntimeError("kpubdata-builder is not installed beside this interpreter")
        command = [executable, "serve", "--port", str(port), "--output-dir", str(self._data)]
        with self._log.open("ab") as log:
            self._process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        return f"http://127.0.0.1:{port}"

    def begin_stop(self) -> None:
        assert self._process is not None
        self._process.send_signal(signal.SIGTERM)

    def wait_stopped(self, timeout: float) -> int | None:
        assert self._process is not None
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        assert self._process is not None
        self._process.kill()
        self._process.wait()

    def logs(self) -> str:
        return self._log.read_text("utf-8", "replace")[-6000:]

    def close(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait()


class Api:
    """Requests to one started server, with the service key."""

    def __init__(self, base: str) -> None:
        self.base = base

    def call(
        self, method: str, path: str, body: bytes | None = None, content_type: str = ""
    ) -> tuple[int, Json]:
        """One request. A connection that is refused or dropped raises ``OSError``."""
        headers = {"X-API-Key": SERVICE_KEY}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=body, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _json(error.read())

    def answers(self) -> bool:
        try:
            return self.call("GET", "/healthz")[0] == 200
        except OSError:
            return False

    def wait_until(self, answering: bool, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.answers() is answering:
                return True
            time.sleep(0.1)
        return False

    def submit(self, spec: str, run_id: str) -> tuple[int, Json]:
        return self.call("POST", "/builds", _submission(spec, run_id), "application/json")

    def job(self, run_id: str) -> Json:
        status, body = self.call("GET", f"/builds/{run_id}")
        return {**body, "http": status}

    def events(self, run_id: str) -> list[Json]:
        body = self.call("GET", f"/builds/{run_id}/events?limit=200")[1]
        events = body.get("events")
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]

    def wait_for_status(self, run_id: str, wanted: tuple[str, ...], seconds: float) -> str:
        deadline = time.monotonic() + seconds
        status = ""
        while time.monotonic() < deadline:
            status = str(self.job(run_id).get("status"))
            if status in wanted:
                break
            time.sleep(0.05)
        return status

    def held_submission(self, spec: str, run_id: str) -> HeldRequest:
        return HeldRequest(self.base, _submission(spec, run_id))


class HeldRequest:
    """A ``POST /builds`` whose last byte is sent later.

    The server has accepted the connection and is reading the body when the signal
    arrives, so the request is one "already being handled": it reaches the job registry
    after the shutdown began, whenever the listener closes.
    """

    def __init__(self, base: str, body: bytes) -> None:
        host, port = base.removeprefix("http://").rsplit(":", 1)
        self._connection = http.client.HTTPConnection(host, int(port), timeout=30)
        self._last = body[-1:]
        self._connection.putrequest("POST", "/builds")
        self._connection.putheader("X-API-Key", SERVICE_KEY)
        self._connection.putheader("Content-Type", "application/json")
        self._connection.putheader("Content-Length", str(len(body)))
        self._connection.endheaders()
        self._connection.send(body[:-1])

    def finish(self) -> tuple[int, Json]:
        try:
            self._connection.send(self._last)
            response = self._connection.getresponse()
            return response.status, _json(response.read())
        finally:
            self._connection.close()


def _json(raw: bytes) -> Json:
    try:
        loaded = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:200].decode("utf-8", "replace")}
    return loaded if isinstance(loaded, dict) else {"raw": loaded}


def _submission(spec: str, run_id: str) -> bytes:
    return json.dumps({"spec": spec, "run_id": run_id}).encode("utf-8")


def _csv(rows: int) -> bytes:
    lines = (f"{n},{n * 7 % 1000},name-{n % 977},{n / 3:.4f}\n" for n in range(rows))
    return ("id,bucket,name,ratio\n" + "".join(lines)).encode("utf-8")


def _spec(upload_id: str) -> str:
    return (
        "dataset_id: smoke.shutdown_drain\n"
        "title: Shutdown drain smoke\n"
        "description: An uploaded file large enough that a build takes seconds.\n"
        "sources:\n"
        "  - kind: file\n"
        f"    upload_id: {upload_id}\n"
        "    format: csv\n"
        "    alias: rows\n"
        "exports:\n"
        "  - kind: jsonl\n"
        "    output_path: data.jsonl\n"
    )


class Smoke:
    def __init__(self, server: Compose | Local, rows: int) -> None:
        self.server = server
        self.rows = rows
        self.problems: list[str] = []
        self.stop_timeout = stop_timeout_seconds()
        self.spec = ""

    def check(self, ok: bool, label: str, detail: object = "") -> bool:
        print(f"{'ok  ' if ok else 'FAIL'} {label}{f': {detail}' if detail != '' else ''}")
        if not ok:
            self.problems.append(f"{label}: {detail}")
        return ok

    def start(self, grace: str) -> Api | None:
        api = Api(self.server.start(grace))
        if not api.wait_until(True, 90.0):
            self.check(False, "the server answers /healthz after a start")
            return None
        return api

    def upload(self, api: Api) -> bool:
        status, body = api.call("POST", "/uploads?format=csv", _csv(self.rows), "text/csv")
        if not self.check(status == 200, "the source file is uploaded", status):
            return False
        self.spec = _spec(str(body.get("upload_id")))
        return True

    def one_running_two_queued(self, api: Api, prefix: str) -> tuple[str, list[str]] | None:
        """Submit three builds; the first holds the only slot. None when that did not hold."""
        running, queued = f"{prefix}-running", [f"{prefix}-queued-1", f"{prefix}-queued-2"]
        accepted = api.submit(self.spec, running)[0]
        started = api.wait_for_status(running, ("running", *TERMINAL), 30.0)
        if not self.check(
            (accepted, started) == (202, "running"), f"{running} is running", (accepted, started)
        ):
            return None
        codes = [api.submit(self.spec, run_id)[0] for run_id in queued]
        states = [api.job(run_id).get("status") for run_id in queued]
        if not self.check(
            codes == [202, 202] and states == ["queued", "queued"],
            "two more builds are queued behind it",
            (codes, states),
        ):
            return None
        return running, queued

    def stop(self, api: Api, label: str) -> bool:
        """SIGTERM; the server must leave by itself, with code 0, inside the stop timeout."""
        began = time.monotonic()
        code = self.server.wait_stopped(self.stop_timeout + 30.0)
        took = time.monotonic() - began
        ok = self.check(
            code == 0 and took < self.stop_timeout,
            f"{label}: the server exits by itself with code 0",
            f"code {code} after {took:.1f}s (stop timeout {self.stop_timeout:.0f}s)",
        )
        return self.check(not api.answers(), f"{label}: nothing answers afterwards") and ok

    def check_queued_ended(self, api: Api, queued: list[str], when: str) -> None:
        for run_id in queued:
            job = api.job(run_id)
            names = [event.get("event") for event in api.events(run_id)]
            error = str(job.get("error"))
            self.check(
                job.get("status") == "failed"
                and error.startswith("interrupted:")
                and "new run_id" in error
                and names == ["run_submitted", "run_failed"],
                f"{when}: {run_id} is failed (interrupted) and never started",
                (job.get("status"), error[:60], names),
            )

    def check_not_resumed(self, api: Api, run_ids: list[str]) -> None:
        """Nothing picks an ended run up again: its events stay as they were."""
        before = {run_id: api.events(run_id) for run_id in run_ids}
        time.sleep(3.0)
        for run_id in run_ids:
            after = api.events(run_id)
            self.check(
                after == before[run_id] and bool(after),
                f"{run_id} is not run again after the restart",
                [event.get("event") for event in after][-3:],
            )

    def drain(self) -> None:
        print("== 1. SIGTERM with the default grace period: the running build finishes")
        api = self.start("")
        if api is None or not self.upload(api):
            return
        jobs = self.one_running_two_queued(api, "drain")
        if jobs is None:
            return
        running, queued = jobs
        late = api.held_submission(self.spec, "drain-late")
        # Connections are accepted in order: this answer means the held one was taken.
        self.check(api.answers(), "the server still answers before the signal")

        self.server.begin_stop()
        self.check(api.wait_until(False, 20.0), "the listener closes after SIGTERM")
        status, body = late.finish()
        self.check(
            (status, body.get("code")) == (503, "shutting_down"),
            "a submission already being read is refused",
            (status, body.get("code")),
        )
        try:
            fresh: object = api.submit(self.spec, "drain-fresh")[0]
        except OSError as error:
            fresh = type(error).__name__
        self.check(not isinstance(fresh, int), "a new connection is not served", fresh)
        if not self.stop(api, "drain"):
            return

        api = self.start("")
        if api is None:
            return
        self.check_queued_ended(api, queued, "after the restart")
        job = api.job(running)
        self.check(job.get("status") == "succeeded", f"{running} finished", job.get("status"))
        finished = [e for e in api.events(running) if e.get("event") == "run_finished"]
        ended = [e for e in api.events(queued[0]) if e.get("event") == "run_failed"]
        if self.check(bool(finished and ended), "both endings are in the event log"):
            # The queued jobs are ended when the shutdown begins, so a later ending of
            # the running build is one the server waited for.
            order = (str(ended[0].get("timestamp")), str(finished[0].get("timestamp")))
            self.check(order[0] < order[1], f"{running} finished after the shutdown began", order)
        for run_id in ("drain-late", "drain-fresh"):
            found = api.job(run_id).get("http")
            self.check(found == 404, f"the refused {run_id} left no job", found)
        self.check_not_resumed(api, [running, *queued])
        self.server.begin_stop()
        self.stop(api, "idle")

    def grace_expired(self) -> Api | None:
        """Returns the server started again afterwards, for the next step."""
        print("== 2. SIGTERM with a 1 s grace period: the running build is asked to stop")
        api = self.start("1")
        if api is None:
            return None
        jobs = self.one_running_two_queued(api, "overdue")
        if jobs is None:
            return None
        running, queued = jobs
        self.server.begin_stop()
        if not self.stop(api, "grace expired"):
            return None

        api = self.start("1")
        if api is None:
            return None
        self.check_queued_ended(api, queued, "after the restart")
        job = api.job(running)
        events = api.events(running)
        last = events[-1] if events else {}
        message = str(last.get("message"))
        self.check(
            job.get("status") == "cancelled"
            and last.get("event") == "run_cancelled"
            and "shutting down" in message
            and "new run_id" in message,
            f"{running} is cancelled and its event says the server was shutting down",
            (job.get("status"), last.get("event"), message[:80]),
        )
        self.check_not_resumed(api, [running, *queued])
        return api

    def killed(self, api: Api) -> None:
        print("== 3. SIGKILL: no drain; the next start marks the run")
        run_id = "killed-running"
        accepted = api.submit(self.spec, run_id)[0]
        started = api.wait_for_status(run_id, ("running", *TERMINAL), 30.0)
        if not self.check(
            (accepted, started) == (202, "running"), f"{run_id} is running", (accepted, started)
        ):
            return
        self.server.kill()
        self.check(api.wait_until(False, 20.0), "the killed server stops answering")

        api_again = self.start("1")
        if api_again is None:
            return
        job = api_again.job(run_id)
        names = [event.get("event") for event in api_again.events(run_id)]
        self.check(
            job.get("status") == "failed"
            and job.get("code") == "credentials_required"
            and "new run_id" in str(job.get("error"))
            and names[-1:] == ["run_failed"]
            and "run_finished" not in names,
            f"{run_id} is failed as credentials_required after the restart",
            (job.get("status"), job.get("code"), names[-2:]),
        )
        self.check_not_resumed(api_again, [run_id])

    def run(self) -> None:
        self.drain()
        if self.problems:
            return
        # Step 2 leaves the server up again, on the same volume.
        api = self.grace_expired()
        if api is not None and not self.problems:
            self.killed(api)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", help="Builder image to run")
    mode.add_argument("--local", action="store_true", help="run the installed serve here")
    parser.add_argument(
        "--rows", type=int, default=300_000, help="rows of the uploaded file (build length)"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="shutdown-drain-smoke-") as directory:
        server: Compose | Local
        server = Local(Path(directory)) if args.local else Compose(args.image, Path(directory))
        smoke = Smoke(server, args.rows)
        try:
            try:
                smoke.run()
            except (OSError, RuntimeError) as error:
                smoke.problems.append(f"the smoke could not go on: {error!r}")
            if smoke.problems:
                print(server.logs())
        finally:
            server.close()

    for problem in smoke.problems:
        print(f"::error::{problem}")
    return 1 if smoke.problems else 0


if __name__ == "__main__":
    sys.exit(main())
