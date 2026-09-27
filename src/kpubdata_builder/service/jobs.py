"""비동기 build job 레지스트리와 bounded worker 실행기 (#482, #481 취소).

취소(#481) 설계 요약:

- **강제 종료 없음**: worker thread를 죽이지 않는다. ``RunCancellation``이
  run 하나에 바인딩된 협력적 취소 상태를 들고, pipeline이 안전한 stage 경계에서
  이를 확인한다(``pipeline.cancellation.CancellationProbe`` 계약).
- **단일 arbiter**: "이 job이 succeeded/failed로 끝나는가 cancelled로 끝나는가"는
  ``RunCancellation``의 committed/requested 래치 하나로만 결정된다
  (``AsyncBuildJobRegistry.finish``). 그래서 같은 run에 succeeded와 cancelled가
  동시에 기록되는 상태가 만들어질 수 없다.
- **lock 순서**: registry lock -> run cancellation lock. 반대 방향으로 잡는
  경로는 없으며(``RunCancellation``은 registry를 전혀 모른다), 두 lock 모두
  임계구역 안에서 runner/pipeline/디스크 I/O 같은 외부 호출을 하지 않는다.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from ..spec import JsonValue

_logger = logging.getLogger(__name__)

BuildJobStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]


if TYPE_CHECKING:
    from .app import ServiceResponse as BuildJobResponse
else:

    class BuildJobResponse(Protocol):
        """BuilderService 응답 중 job 실행기가 필요한 구조."""

        status_code: int
        body: dict[str, JsonValue]


class BuildJobRunner(Protocol):
    """job worker가 호출하는 실제 build 실행 진입점.

    ``cancellation``은 run 하나에 바인딩된 협력적 취소 상태(#481)다 — runner는
    이를 pipeline까지 그대로 내려보내기만 하며, registry/HTTP/Principal 같은
    service 개념은 pipeline domain으로 넘기지 않는다.
    """

    def __call__(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: RunCancellation,
    ) -> BuildJobResponse: ...


SubmitStatus = Literal["accepted", "existing", "queue_full"]

# ``AsyncBuildJobRegistry.request_cancel``의 결과. 각 값은 정확히 하나의 HTTP
# 응답으로 매핑된다(``BuilderService.cancel_build``) — race와 무관하게 결정적이다.
#   - "cancelled":  queued job을 실행 전에 종결 취소했다(runner 호출 0회).
#   - "cancelling": running job에 취소를 요청했다. 실제 종결은 다음 안전 경계.
#   - "already":    이미 취소가 요청된(cancelling) 또는 이미 cancelled인 job.
#   - "terminal":   succeeded/failed로 이미 끝났거나, pipeline이 마지막 안전
#                   경계를 지나 정상 종료로 확정(commit)해 더는 취소할 수 없는 job.
#   - "unknown":    registry가 모르는 run_id.
CancelOutcome = Literal["cancelled", "cancelling", "already", "terminal", "unknown"]

# 다시 running/cancelling으로 돌아갈 수 없는 종단 상태.
_TERMINAL_STATUSES: frozenset[BuildJobStatus] = frozenset({"succeeded", "failed", "cancelled"})

# registry 가 메모리에 들고 있을 terminal job 수. 넘으면 먼저 끝난 것부터 버린다.
# 완료된 run 의 정본은 manifest 와 event store 라, 이 캐시는 "방금 끝난 run 을
# 폴링해서 결과를 받아가는" 창만 덮으면 된다.
_DEFAULT_MAX_TERMINAL_JOBS = 256


class RunCancellation:
    """run 하나에 바인딩된 협력적 취소 상태 (#481).

    ``pipeline.cancellation.CancellationProbe``를 구조적으로 만족한다 —
    pipeline은 이 클래스도, service 계층도 알 필요가 없고 ``cancel_requested()``와
    ``commit()``만 호출한다. 인스턴스는 run마다 새로 만들어지므로 한 run의 취소가
    다른 run에 전파되지 않는다.

    두 개의 단조(monotonic) 래치만 갖는다.

    ``_requested``
        취소가 요청됐다. 한 번 True가 되면 다시 False가 되지 않는다.
    ``_committed``
        pipeline이 마지막 안전 경계를 지나 정상 종료(성공/실패 manifest 기록)로
        확정했다. 이후 ``request()``는 항상 False를 반환한다.

    두 래치는 같은 lock 아래에서만 바뀌므로 "commit과 request가 동시에 성공"하는
    상태가 존재할 수 없다 — 그래서 ``cancelling -> succeeded``나 "성공 manifest를
    쓴 run이 cancelled로 뒤집히는" 전이가 구조적으로 불가능하다.
    """

    __slots__ = ("_committed", "_lock", "_requested")

    def __init__(self) -> None:
        self._lock = Lock()
        self._requested = False
        self._committed = False

    def request(self) -> bool:
        """취소를 요청한다. 아직 정상 종료로 확정되지 않았으면 True.

        이미 요청된 상태에서 다시 호출해도 True를 반환한다(idempotent) —
        "취소는 여전히 유효하다"는 같은 답을 준다.
        """
        with self._lock:
            if self._committed:
                return False
            self._requested = True
            return True

    def cancel_requested(self) -> bool:
        """``CancellationProbe``: 취소가 요청됐는지. 상태를 바꾸지 않는다."""
        with self._lock:
            return self._requested

    def commit(self) -> bool:
        """``CancellationProbe``: 정상 종료로 확정할 수 있으면 True(그리고 래치)."""
        with self._lock:
            if self._requested:
                return False
            self._committed = True
            return True

    def close(self) -> bool:
        """job이 종결될 때 창을 닫고 "취소로 끝났는가"를 반환한다.

        ``AsyncBuildJobRegistry.finish``가 terminal 상태를 확정하기 직전에 정확히
        한 번 호출한다. commit을 함께 래치하므로, 종결 판정 이후에 도착한 취소
        요청이 이미 확정된 terminal 상태를 바꾸는 일이 없다.
        """
        with self._lock:
            self._committed = True
            return self._requested


@dataclass(frozen=True, slots=True)
class BuildJobSnapshot:
    """build job 상태 스냅샷.

    ``owner_id``는 active/terminal async run의 ownership 판정용 internal
    필드다(#496 follow-up, #505 canonical stable identity) — ``created_by``
    (Principal.label, display/legacy fallback)와 달리 신규 ownership 판정에
    우선 쓰이는 값이고, ``BuilderService._run_build_job``이 persisted
    manifest/BuildIndex(#505 SSOT) 기록용으로도 그대로 재사용한다. 다만
    ``to_body()``가 wire로 절대 내보내지 않고, ``kind="file"`` source
    resolver(#498)에는 여전히 전달되지 않는다 — async file-backed source
    owner propagation 한계는 그대로 유지된다.
    """

    run_id: str
    status: BuildJobStatus
    created_at: str
    updated_at: str
    created_by: str | None = None
    owner_id: str | None = None
    response: dict[str, JsonValue] | None = None
    error: str | None = None

    def to_body(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.created_by is not None:
            body["created_by"] = self.created_by
        if self.response is not None:
            body["response"] = self.response
        if self.error is not None:
            body["error"] = self.error
        return body


@dataclass(frozen=True, slots=True)
class BuildJobSubmitResult:
    status: SubmitStatus
    snapshot: BuildJobSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AsyncBuildJobCounts:
    """``snapshot_counts()``가 단일 lock scope에서 집계한 active job 수 (#516)."""

    queued: int
    running: int


class AsyncBuildJobRegistry:
    """프로세스 메모리에만 유지되는 active/terminal job 레지스트리.

    terminal job 은 **종결된 순서대로 최대 ``max_terminal_jobs`` 개**만 남는다.
    예전에는 하나도 지우지 않아서, 오래 떠 있는 프로세스의 ``_jobs`` 가 그동안
    실행한 모든 build 를 들고 있었다 — snapshot 에는 성공 응답 본문(``response``)
    까지 들어 있어서 job 하나가 작지도 않다. 재시작 말고는 줄어들 방법이 없었다.

    지워지는 것은 이 메모리 캐시뿐이다. run 의 정본 기록은 두 군데에 남는다 —
    완료된 run 의 ``manifest.json`` 과 append-only event store 다. 그래서
    ``GET /builds/{run_id}/events`` 와 manifest 기반 조회는 eviction 뒤에도
    그대로 답하고, ownership 판정도 manifest 를 먼저 보므로(``_guards``)
    산출물이 있는 run 의 소유권이 약해지지 않는다. ``GET /builds/{run_id}`` 만
    아주 오래된 run 에 대해 404 가 된다.
    """

    def __init__(self, *, max_terminal_jobs: int = _DEFAULT_MAX_TERMINAL_JOBS) -> None:
        self._lock = Lock()
        self._jobs: dict[str, BuildJobSnapshot] = {}
        # run_id -> 협력적 취소 상태 (#481). snapshot과 같은 생명주기를 갖고,
        # mutable 상태 자체는 외부에 노출하지 않는다(``cancellation()``이
        # 반환하는 객체도 좁은 request/probe API만 제공한다).
        self._cancellations: dict[str, RunCancellation] = {}
        # 종결된 순서대로의 run_id. 생성 순서(``_jobs`` 의 삽입 순서)가 아니라
        # 종결 순서로 버려야 "먼저 끝난 것부터" 가 된다 — 오래 걸린 run 이
        # 먼저 제출됐다는 이유로 방금 끝나자마자 지워지면 안 된다.
        self._terminal_order: deque[str] = deque()
        self._max_terminal_jobs = max(0, max_terminal_jobs)

    def create(
        self, *, run_id: str, created_by: str | None, owner_id: str | None = None
    ) -> BuildJobSnapshot:
        now = _utc_now_text()
        snapshot = BuildJobSnapshot(
            run_id=run_id,
            status="queued",
            created_at=now,
            updated_at=now,
            created_by=created_by,
            owner_id=owner_id,
        )
        with self._lock:
            self._jobs[run_id] = snapshot
            self._cancellations[run_id] = RunCancellation()
        return snapshot

    def try_create(
        self,
        *,
        run_id: str,
        created_by: str | None,
        owner_id: str | None = None,
        max_queued: int,
    ) -> tuple[str, BuildJobSnapshot | None]:
        """존재 확인·큐 용량 확인·생성을 **한 lock scope** 안에서 한다 (#482 후속).

        ``get`` → ``queued_count`` → ``create`` 를 따로 부르면 그 사이에 다른
        스레드가 끼어든다. 같은 run_id 로 동시에 POST 하면 둘 다 존재 확인을
        통과해 ``on_accept`` 가 두 번 불리고 event 가 두 번 남으며, 큐 용량도
        ``max_queue_size`` 를 넘길 수 있다.

        반환값: ``("existing"|"queue_full"|"created", snapshot|None)``.
        """
        now = _utc_now_text()
        with self._lock:
            existing = self._jobs.get(run_id)
            if existing is not None:
                return "existing", existing
            queued = sum(1 for job in self._jobs.values() if job.status == "queued")
            if queued >= max_queued:
                return "queue_full", None
            snapshot = BuildJobSnapshot(
                run_id=run_id,
                status="queued",
                created_at=now,
                updated_at=now,
                created_by=created_by,
                owner_id=owner_id,
            )
            self._jobs[run_id] = snapshot
            self._cancellations[run_id] = RunCancellation()
            return "created", snapshot

    def cancellation(self, run_id: str) -> RunCancellation | None:
        """이 run의 협력적 취소 상태를 반환한다 (#481). 없으면 None."""
        with self._lock:
            return self._cancellations.get(run_id)

    def begin_run(self, run_id: str) -> bool:
        """worker가 runner를 실행하기 직전에 호출한다. 시작해도 되면 True (#481).

        ``queued -> running`` 전이를 단일 lock scope의 원자적 판정으로 만든다 —
        cancel 요청과 worker 시작이 동시에 일어나도 두 경로 중 정확히 하나만
        성립한다.

        - cancel이 먼저 잡으면 job은 ``queued -> cancelled``로 끝나고, 이 함수는
          False를 반환해 **runner를 한 번도 호출하지 않는다**(pipeline artifact 0개).
        - worker가 먼저 잡으면 job은 ``queued -> running``이 되고, 뒤이은 cancel은
          ``running -> cancelling``으로 이어져 다음 안전 경계에서 종결된다.

        ``cancelled -> running`` 같은 역전이는 이 판정 때문에 존재할 수 없다.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None or current.status != "queued":
                return False
            self._jobs[run_id] = _transition(current, status="running")
            return True

    def request_cancel(self, run_id: str) -> tuple[CancelOutcome, BuildJobSnapshot | None]:
        """취소를 요청한다. 상태 전이와 결과 판정을 하나의 원자적 연산으로 수행한다.

        registry lock 안에서 run cancellation lock을 잡는다(문서화된 유일한 lock
        순서). 두 임계구역 모두 외부 호출/I/O를 하지 않는다.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None:
                return "unknown", None
            if current.status in _TERMINAL_STATUSES:
                # cancelled를 다시 취소하는 요청은 "이미 취소됨"으로 응답해
                # 반복 호출이 결정적으로 같은 결과를 주게 한다.
                outcome: CancelOutcome = "already" if current.status == "cancelled" else "terminal"
                return outcome, current
            cancellation = self._cancellations.get(run_id)
            if cancellation is not None and not cancellation.request():
                # pipeline이 이미 마지막 안전 경계를 지나 정상 종료로 확정했다 —
                # 곧 succeeded/failed가 된다. 여기서 cancelling으로 바꾸면
                # "cancelling -> succeeded"라는 금지된 전이가 만들어진다.
                return "terminal", current
            if current.status == "queued":
                cancelled = _transition(current, status="cancelled")
                self._jobs[run_id] = cancelled
                self._retire_locked(run_id)
                return "cancelled", cancelled
            if current.status == "cancelling":
                return "already", current
            cancelling = _transition(current, status="cancelling")
            self._jobs[run_id] = cancelling
            return "cancelling", cancelling

    def finish(
        self,
        run_id: str,
        *,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
        failed: bool,
    ) -> BuildJobSnapshot | None:
        """runner 종료 후 terminal 상태를 확정한다 (#481).

        terminal 상태를 정하는 **유일한** 지점이다. ``RunCancellation.close()``가
        취소 창을 닫으면서 "취소로 끝났는가"를 알려주므로, 같은 run이 succeeded와
        cancelled를 동시에 기록할 수 없다. 취소로 끝난 job에는 build 응답 본문도
        error 문자열도 싣지 않는다 — 취소는 실패가 아니고, 부분 실행의 build 출력을
        성공 응답처럼 노출하지도 않는다(부분 산출물의 정본은 partial manifest다).

        이미 terminal인 job은 그대로 둔다(멱등) — 확정된 종단 상태를 덮어쓰지 않는다.

        **취소 우선 판정의 범위(명시적 정책)**: ``close()``가 True면 runner 결과와
        무관하게 ``cancelled``다. 취소가 요청됐는데 pipeline이 취소를 관찰할 안전
        경계에 도달하기 *전에* 실행이 끝난 경우(예: ``BuilderService.build()``가
        파이프라인 진입 전 spec 검증에서 400으로 되돌아온 경우)도 여기 해당한다.
        그 run은 manifest도 artifact도 남기지 않으므로 ``cancelled``로 보고하는
        것이 사용자의 요청과 실제 결과(아무 것도 만들어지지 않음) 양쪽에 모두
        부합하고, ``cancelling -> failed``라는 금지된 전이를 만들지도 않는다.
        반대로 pipeline이 실제로 실행된 실패는 절대 취소로 삼켜지지 않는다 —
        manifest를 쓰기 직전에 ``commit()``이 취소 창을 닫으므로 그 이후의 취소
        요청은 거절되고(``request_cancel``이 "terminal") ``close()``도 False다.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None:
                return None
            if current.status in _TERMINAL_STATUSES:
                return current
            cancellation = self._cancellations.get(run_id)
            cancelled = cancellation.close() if cancellation is not None else False
            if cancelled:
                updated = _transition(current, status="cancelled")
            elif failed:
                updated = _transition(current, status="failed", response=response, error=error)
            else:
                updated = _transition(current, status="succeeded", response=response)
            self._jobs[run_id] = updated
            self._retire_locked(run_id)
            return updated

    def mark_failed(
        self, run_id: str, *, response: dict[str, JsonValue] | None = None, error: str | None = None
    ) -> BuildJobSnapshot:
        """job을 실패로 확정한다 (제출 자체가 실패한 경로 전용, #496).

        실제 실행 결과의 종결에는 ``finish()``를 쓴다 — 이 메서드는 worker에
        큐잉조차 되지 못한 job(``AsyncBuildExecutor.submit``의 enqueue 실패)을
        phantom queued로 남기지 않기 위한 경로다. 그 시점에는 아직 취소 요청이
        도달할 수 없는 구조(제출 응답이 반환되기 전)라 취소와 경합하지 않는다.
        """
        return self._replace(run_id, status="failed", response=response, error=error)

    def list_all(self) -> list[BuildJobSnapshot]:
        """Every job the registry still holds, in-flight ones included (#679).

        The administrator view needs these. ``BuildIndex`` is only written once a
        build has produced a manifest, so a queued or running job does not appear
        there at all -- and a stuck run is exactly what an operator looks for.
        """
        with self._lock:
            return list(self._jobs.values())

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        with self._lock:
            return self._jobs.get(run_id)

    def discard(self, run_id: str) -> None:
        """생성 직후의 job 을 없던 것으로 되돌린다.

        ``on_accept`` 가 실패하면 event store 에 ``run_submitted`` 가 남지 않으므로
        registry 에만 있는 job 은 아무도 관찰할 수 없다 — 유령 queued 항목이 되어
        큐 용량만 차지한다.
        """
        with self._lock:
            self._jobs.pop(run_id, None)
            self._cancellations.pop(run_id, None)

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status == "queued")

    def snapshot_counts(self) -> AsyncBuildJobCounts:
        """queued/running job 수를 단일 lock scope에서 일관되게 집계한다 (#516).

        ``queued_count()`` 같은 개별 메서드를 서로 다른 시점에 호출해 조합하면
        그 사이의 상태 전이(queued -> running)로 모순된 snapshot이 만들어질 수
        있다 — monitoring은 반드시 이 메서드처럼 하나의 lock 안에서 계산한
        값을 써야 한다. terminal(succeeded/failed/cancelled) job은 registry가
        메모리에 계속 보존하지만 이 snapshot에는 포함하지 않는다 — Monitoring이
        보여줘야 하는 건 현재 workload이지 terminal history가 아니다. mutable
        ``_jobs`` dict 자체는 절대 외부에 노출하지 않는다.

        ``cancelling``(#481)은 running으로 센다 — 취소를 요청받았을 뿐 아직
        worker slot을 실제로 점유하고 있는 job이라, 현재 workload에서 빼면
        Monitoring의 worker 사용률이 실제보다 낮게 보인다. 취소가 종결되면
        terminal(cancelled)이 되어 자연히 집계에서 빠진다.
        """
        with self._lock:
            queued = 0
            running = 0
            for job in self._jobs.values():
                if job.status == "queued":
                    queued += 1
                elif job.status in ("running", "cancelling"):
                    running += 1
        return AsyncBuildJobCounts(queued=queued, running=running)

    def _retire_locked(self, run_id: str) -> None:
        """job 이 방금 terminal 이 됐다고 기록하고, 보존 한도를 넘으면 버린다.

        반드시 ``self._lock`` 을 잡은 채로 부른다. 한 run_id 가 두 번 들어오는
        일은 없다 — terminal 로 바꾸는 모든 경로가 이미 terminal 인 job 을
        먼저 걸러내기 때문이다.
        """
        self._terminal_order.append(run_id)
        while len(self._terminal_order) > self._max_terminal_jobs:
            evicted = self._terminal_order.popleft()
            job = self._jobs.get(evicted)
            # discard() 로 이미 사라졌거나(있을 수 없지만) 어떤 이유로 terminal 이
            # 아니게 된 항목은 건드리지 않는다 — 살아 있는 job 을 지우는 쪽이
            # 한도를 조금 넘기는 쪽보다 훨씬 나쁘다.
            if job is not None and job.status in _TERMINAL_STATUSES:
                del self._jobs[evicted]
                self._cancellations.pop(evicted, None)

    def _replace(
        self,
        run_id: str,
        *,
        status: BuildJobStatus,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> BuildJobSnapshot:
        with self._lock:
            current = self._jobs[run_id]
            # 확정된 종단 상태는 덮어쓰지 않는다 (#481) — cancelled가 나중에
            # failed로 바뀌는 역전이를 막는다.
            if current.status in _TERMINAL_STATUSES:
                return current
            updated = _transition(current, status=status, response=response, error=error)
            self._jobs[run_id] = updated
            if status in _TERMINAL_STATUSES:
                self._retire_locked(run_id)
            return updated


@dataclass(frozen=True, slots=True)
class AsyncBuildStats:
    """Monitoring API(#516)가 소비하는 async build 실행기 raw 집계.

    ``queued``/``running``/``capacity`` raw 값만 담는다 — ``total``/``active``/
    ``utilization`` 같은 파생값과 availability 판정은 ``service/monitoring.py``의
    책임이다.
    """

    queued: int
    running: int
    capacity: int


class AsyncBuildExecutor:
    """고정 크기 worker pool로 build job을 실행한다."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_queue_size: int = 10,
        max_terminal_jobs: int = _DEFAULT_MAX_TERMINAL_JOBS,
        on_cancelled: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-build"
        )
        # ThreadPoolExecutor._max_workers 같은 private field를 외부(monitoring)가
        # 직접 읽지 않도록 capacity를 생성 시점에 명시적으로 보존한다 (#516).
        self._max_workers = max_workers
        self._max_queue_size = max_queue_size
        # running job이 안전 경계에서 실제로 cancelled로 종결됐을 때 정확히 한 번
        # 호출된다 (#481). 호출자(BuilderService)는 이 hook으로 종결
        # event(run_cancelled)를 남긴다 — worker thread에서 호출되므로 hook은
        # 예외를 전파하지 않아야 한다(호출자 책임).
        self._on_cancelled = on_cancelled
        self.registry = AsyncBuildJobRegistry(max_terminal_jobs=max_terminal_jobs)

    def stats(self) -> AsyncBuildStats:
        """monitoring용 read-only aggregate snapshot (#516).

        registry의 mutable internal job dict는 노출하지 않고, 단일 lock scope
        에서 얻은 queued/running 집계(``registry.snapshot_counts()``)와 worker
        pool capacity만 반환한다. 이 호출 자체는 job 상태를 변경하지 않는다.
        """
        counts = self.registry.snapshot_counts()
        return AsyncBuildStats(
            queued=counts.queued, running=counts.running, capacity=self._max_workers
        )

    def submit(
        self,
        *,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
        owner_id: str | None = None,
        on_accept: Callable[[], None] | None = None,
        on_enqueue_failure: Callable[[], None] | None = None,
    ) -> BuildJobSubmitResult:
        """job을 큐잉한다. ``existing``/``queue_full``이면 새 submission이 아니므로
        ``on_accept``를 호출하지 않는다.

        ``owner_id``는 ``registry.create()``까지만 전달되어 snapshot에
        보존된다(#496 follow-up, active run ownership 판정용) — ``runner``
        호출(아래 ``self._executor.submit(self._run, spec_yaml, run_id,
        created_by, runner)``)에는 전달하지 않는다. run_build/source resolver
        쪽 owner propagation은 #498에서 이미 별도 한계로 남겨졌고, 이번
        변경에서 그 범위를 넓히지 않는다.

        ``on_accept``는 job이 실제로 worker pool에 큐잉되기(``self._executor.submit``)
        *전*에 호출된다(#496). 호출자(``BuilderService.submit_build``)는 이 hook으로
        "run_submitted" event를 append한다 — 그 append가 여기서 실패해 예외를
        전파하면, job은 registry에 만들어지지도 worker에 큐잉되지도 않은 채
        그대로 끝난다. 이 순서 덕분에 "event는 유실됐는데 job은 이미 실행
        중"이라는 모순이 구조적으로 생기지 않는다 — job의 "accepted" 여부 자체가
        이 event 기록 성공에 달려 있다. ``on_accept``가 없는 호출자(기존
        monitoring 테스트 등)는 이전과 동일하게 아무 gating 없이 그대로 큐잉된다.

        반대 방향(#496 self-review): ``on_accept``가 성공해 event는 기록됐는데
        ``self._executor.submit()``(실제 worker pool 큐잉) 자체가 실패하면,
        ``registry.create()``가 이미 만든 "queued" 항목이 아무도 실행하지 않을
        phantom으로 영원히 남는다 — event(``run_submitted``)는 append-only라
        지울 수 없으므로(#496 원칙), 이 항목을 "queued"로 방치하지 않고 실제
        job 실행 실패에 이미 쓰이는 것과 동일한 ``registry.mark_failed()``로
        정리한 뒤 예외를 그대로 전파한다 — 새 상태값을 만들지 않고 기존
        terminal mechanism을 재사용한다.

        ``on_enqueue_failure``는 ``registry.mark_failed()`` 직후, 예외를
        재전파하기 *전*에 호출된다(#496 lifecycle 계약: timeline 자체도 이
        실패를 표현해야 한다) — 호출자는 이 hook으로 같은 run_id에 기존
        ``run_failed`` event를 append한다. ``on_accept`` 실패 경로(event 자체가
        전혀 기록되지 못한 경우)와는 구별된다 — 그 경로는 ``registry.create()``
        까지 가지도 못하므로 여기 도달하지 않고, ``run_submitted``도
        ``run_failed``도 남기지 않는다.
        """
        # 존재·용량·생성을 한 번에 판정한다. 셋을 따로 부르던 시절에는 같은
        # run_id 로 동시에 POST 하면 둘 다 통과해 on_accept 가 두 번 불리고
        # queue 상한도 넘길 수 있었다.
        outcome, snapshot = self.registry.try_create(
            run_id=run_id,
            created_by=created_by,
            owner_id=owner_id,
            max_queued=self._max_queue_size,
        )
        if outcome == "existing":
            return BuildJobSubmitResult(status="existing", snapshot=snapshot)
        if outcome == "queue_full":
            return BuildJobSubmitResult(status="queue_full")
        assert snapshot is not None  # noqa: S101 - "created" 는 항상 snapshot 을 준다
        if on_accept is not None:
            # 생성 뒤에 부른다. 이 hook 이 던지면 아래에서 job 을 정리한다 —
            # 예전에는 생성 전에 불러서, 두 요청이 모두 여기에 도달할 수 있었다.
            try:
                on_accept()
            except Exception:
                self.registry.discard(run_id)
                raise
        try:
            self._executor.submit(self._run, spec_yaml, run_id, created_by, runner)
        except Exception:
            self.registry.mark_failed(run_id, error="failed to queue build job")
            if on_enqueue_failure is not None:
                on_enqueue_failure()
            raise
        return BuildJobSubmitResult(status="accepted", snapshot=snapshot)

    def list_all(self) -> list[BuildJobSnapshot]:
        return self.registry.list_all()

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        return self.registry.get(run_id)

    def request_cancel(self, run_id: str) -> tuple[CancelOutcome, BuildJobSnapshot | None]:
        """이 run의 취소를 요청한다 (#481). registry의 원자적 전이를 그대로 위임한다."""
        return self.registry.request_cancel(run_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
    ) -> None:
        """worker thread 진입점. runner 호출은 어떤 lock도 쥐지 않은 채 수행한다.

        ``begin_run``이 False면 이 job은 실행 전에 이미 취소된 것이므로 runner를
        **호출하지 않고** 그대로 끝난다 (#481) — 취소된 job이 뒤늦게 pipeline을
        시작해 artifact를 만드는 일이 없다. 종결 event(``run_cancelled``)는 그
        전이를 만든 취소 요청 쪽에서 이미 남겼다.
        """
        if not self.registry.begin_run(run_id):
            return
        try:
            cancellation = self.registry.cancellation(run_id)
            if cancellation is None:  # pragma: no cover - create()가 항상 함께 만든다
                cancellation = RunCancellation()
            response = runner(spec_yaml, run_id, created_by, cancellation)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 job은 종결되어야 한다
            # RuntimeError만 잡던 시절에는 그 밖의 예외가 worker thread를 그대로
            # 빠져나가, job이 영원히 running으로 남았다. polling하는 클라이언트는
            # 끝나지 않는 build를 기다리고, queue 슬롯도 돌아오지 않는다. 무엇이
            # 터졌든 terminal 상태를 확정하는 것이 이 지점의 책임이다.
            # 예외 문자열은 로그에만 남긴다. 이 error 는 ``GET /builds/{run_id}``
            # 응답에 그대로 실리는데, 여기 걸리는 건 "예상하지 못한" 예외라
            # 내용이 무엇일지 보장할 수 없다 — 경로든 SQL 이든 자격증명이든.
            # 타입 이름까지는 남긴다. 어느 계층에서 터졌는지는 알려주면서
            # 임의의 내부 문자열을 내보내지는 않는 선이다.
            _logger.exception("build job %s failed with an unhandled exception", run_id)
            self._finish(run_id, failed=True, error=f"internal error: {type(exc).__name__}")
            return
        if response.status_code < 400:
            self._finish(run_id, failed=False, response=response.body)
            return
        # 여기서 status_code만으로 실패/취소를 구분하지 않는다 (#481). 취소로 끝난
        # run도 build()가 4xx(409) 요약을 돌려주지만, terminal 상태는 오직
        # ``registry.finish()``의 취소 래치가 정한다 — 그래서 아래 response/error는
        # 취소된 job에서는 그대로 버려진다(``finish()`` 참고). 판정 주체를 한 곳에만
        # 두어 "실행 결과"와 "취소 여부"가 서로 다른 답을 낼 수 없게 한다.
        error = response.body.get("error")
        self._finish(
            run_id,
            failed=True,
            response=response.body,
            error=error if isinstance(error, str) else "build failed",
        )

    def _finish(
        self,
        run_id: str,
        *,
        failed: bool,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> None:
        """terminal 상태를 확정하고, cancelled로 끝났으면 종결 hook을 호출한다."""
        snapshot = self.registry.finish(run_id, response=response, error=error, failed=failed)
        if (
            snapshot is not None
            and snapshot.status == "cancelled"
            and self._on_cancelled is not None
        ):
            self._on_cancelled(run_id)


def _transition(
    current: BuildJobSnapshot,
    *,
    status: BuildJobStatus,
    response: dict[str, JsonValue] | None = None,
    error: str | None = None,
) -> BuildJobSnapshot:
    """상태 전이된 새 snapshot을 만든다. 호출자가 registry lock을 쥐고 있어야 한다."""
    return BuildJobSnapshot(
        run_id=current.run_id,
        status=status,
        created_at=current.created_at,
        updated_at=_utc_now_text(),
        created_by=current.created_by,
        owner_id=current.owner_id,
        response=response,
        error=error,
    )


def _utc_now_text() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def generate_run_id() -> str:
    return f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"


__all__ = [
    "AsyncBuildExecutor",
    "AsyncBuildJobCounts",
    "AsyncBuildJobRegistry",
    "AsyncBuildStats",
    "BuildJobSubmitResult",
    "BuildJobResponse",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "CancelOutcome",
    "RunCancellation",
    "SubmitStatus",
    "generate_run_id",
]
