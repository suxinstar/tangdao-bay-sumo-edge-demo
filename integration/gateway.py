"""Bounded background inference, with wall-clock deadlines and stale-run isolation.

Python threads cannot forcibly kill a stuck native model. A hung adapter consumes
the one daemon worker; it cannot create unlimited workers or stall SUMO. Use a
separate service/process for killable inference and set its own request timeout.
"""
from __future__ import annotations

from collections import deque
import queue
import threading
import time
from uuid import uuid4
from .providers import validate_perception_result


class ModelGateway:
    def __init__(self, provider, *, timeout_s=1.0, max_pending=8):
        if not 0.01 <= timeout_s <= 60 or not 1 <= max_pending <= 64:
            raise ValueError('timeout must be 0.01–60 seconds and capacity 1–64')
        self.provider, self.timeout_s, self.max_pending = provider, float(timeout_s), int(max_pending)
        self._queue = queue.Queue(maxsize=max_pending)
        self._pending, self._ready = {}, deque(maxlen=64)
        self._lock, self._stop = threading.Lock(), threading.Event()
        self.counters = dict(submitted=0, completed=0, rejected=0, timedOut=0, failed=0)
        self._worker = threading.Thread(target=self._run, name='acoustic-model', daemon=True)
        self._worker.start()

    def submit(self, frame):
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            if self._stop.is_set() or self.provider is None:
                self.counters['rejected'] += 1
                return {'accepted': False, 'reason': 'model_not_configured_or_closed'}
            if len(self._pending) >= self.max_pending or self._queue.full():
                self.counters['rejected'] += 1
                return {'accepted': False, 'reason': 'model_queue_full'}
            job_id = uuid4().hex
            job = {'id': job_id, 'frame': frame, 'deadline': now + self.timeout_s}
            self._pending[job_id] = job
            self._queue.put_nowait(job)
            self.counters['submitted'] += 1
            return {'accepted': True, 'jobId': job_id}

    def _finish(self, job, status, result=None):
        if self._pending.pop(job['id'], None) is None:
            return
        self._ready.append({'jobId': job['id'], 'runId': job['frame'].run_id,
                            'vehicleId': job['frame'].vehicle_id, 'rsuId': job['frame'].rsu_id,
                            'status': status, 'result': result.to_dict() if result else None,
                            'mode': 'shadow', 'usedForSignalControl': False})
        self.counters[{'ok': 'completed', 'timeout': 'timedOut', 'error': 'failed'}[status]] += 1

    def _expire(self, now):
        for job in list(self._pending.values()):
            if now >= job['deadline']:
                self._finish(job, 'timeout')

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                self._expire(time.monotonic())
                current = job['id'] in self._pending
            if not current:
                self._queue.task_done()
                continue
            try:
                result = validate_perception_result(self.provider.infer(job['frame']), job['frame'])
                status = 'ok'
            except Exception:
                # Model errors can contain endpoint credentials or local data paths.
                # Expose a typed failure, never arbitrary exception text over HTTP.
                result, status = None, 'error'
            with self._lock:
                self._expire(time.monotonic())
                self._finish(job, status, result)
            self._queue.task_done()

    def drain(self, run_id):
        with self._lock:
            self._expire(time.monotonic())
            items = [item for item in self._ready if item['runId'] == run_id]
            self._ready.clear()  # Old-run model outputs never attach to a reset run.
            return items

    def status(self):
        with self._lock:
            self._expire(time.monotonic())
            return {'mode': 'shadow', 'configured': self.provider is not None,
                    'pending': len(self._pending), 'capacity': self.max_pending,
                    'timeoutS': self.timeout_s, 'counters': dict(self.counters),
                    'usedForSignalControl': False}

    def close(self):
        self._stop.set()
        # Do not join an untrusted adapter on the SUMO thread or HTTP shutdown.
        with self._lock:
            self._pending.clear()
            self._ready.clear()
