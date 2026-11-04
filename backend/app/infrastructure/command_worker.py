"""One lifespan-owned command consumer. Never replay uncertain running inference."""
import asyncio
from one_request_telemetry import background_trace, capture_background_trace

from ..application.commands import StaleCommand, active_command_id


class CommandWorker:
    def __init__(self, store, planner, *, tracing=None):
        self.store, self.planner = store, planner
        self._wake = asyncio.Event()
        self._task = None
        self._running = None
        self._job_id = None
        self._closed = False
        self._last_owner = None
        self._tracing = capture_background_trace(tracing)
        self._traces = {}

    def start(self):
        if self._closed:
            raise RuntimeError("worker_closed")
        if self._task is None:
            self.store.recover_commands()
            self._task = asyncio.create_task(self._run(), name="trip-command-worker")
        self.notify()
        return self

    def notify(self, job_id=None):
        if job_id is not None and job_id not in self._traces and len(self._traces) < 33:
            job = self.store.get_job(job_id)
            if job is not None and job.status == 'queued':
                captured = capture_background_trace()
                if captured is not None:
                    self._traces[job_id] = captured
        self._wake.set()

    def cancel(self, job_id):
        self._traces.pop(job_id, None)
        # Admission/application persists cancellation first. A request cannot
        # cancel another command merely by guessing this process-local task ID.
        job = self.store.get_job(job_id)
        if (job is None or job.status in ("canceled", "interrupted")) and self._job_id == job_id and self._running is not None:
            self._running.cancel()
        self.notify()

    async def _execute(self, job):
        token = active_command_id.set(job.id)
        try:
            with background_trace(self._traces.pop(job.id, self._tracing)):
                if job.kind == "prices_refresh":
                    await self.planner.refresh_prices(job.id)
                else:
                    await self.planner.run(job.id)
        finally:
            active_command_id.reset(token)

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            job = self.store.claim_next_command(self._last_owner)
            if job is None:
                await self._wake.wait()
                continue
            self._last_owner = self.store.session_owner(job.session_id)
            self._job_id = job.id
            self._running = asyncio.create_task(self._execute(job), name="trip-command-execution")
            try:
                await self._running
            except asyncio.CancelledError:
                if self._closed:
                    break
            except StaleCommand:
                pass
            except Exception:
                # No provider bodies, exception strings or prompt contents.
                self.store.fail_command(job.id)
            finally:
                self._running = None
                self._job_id = None
                # A planner returning without a terminal commit is not success.
                self.store.fail_command(job.id)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._traces.clear()
        if self._job_id is not None:
            job = self.store.get_job(self._job_id)
            if job is not None and job.status == "running":
                job.status, job.error = "interrupted", "Обработка прервана остановкой приложения. Повторите запрос с новым ключом."
                self.store.save_job(job)
        if self._running is not None:
            self._running.cancel()
        self._wake.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
