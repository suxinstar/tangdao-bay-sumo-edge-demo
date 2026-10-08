"""Small executable interfaces shared by the demo and future real integrations.

Coordinates and lane IDs are in the current SUMO network. Confidence from the
synthetic provider is deliberately absent; SUMO truth is not model inference.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Protocol, Sequence


@dataclass(frozen=True)
class AudioFrame:
    run_id: str
    vehicle_id: str
    rsu_id: str
    sim_time: float
    lane_id: str
    speed_mps: float
    # A real capture adapter supplies a local file or a service-owned object ID.
    # The simulator does not invent a microphone recording from vehicle truth.
    audio_ref: str | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    source: str = 'sumo_vehicle_truth'

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PerceptionResult:
    vehicle_id: str
    rsu_id: str
    lane_id: str | None
    label: str
    confidence: float | None
    source: str
    model_version: str
    synthetic: bool
    direction_degrees: float | None = None
    position_m: tuple[float, float] | None = None
    uncertainty_m: float | None = None

    def to_dict(self):
        return asdict(self)


class PerceptionProvider(Protocol):
    def infer(self, frame: AudioFrame) -> PerceptionResult: ...


class ComputeProvider(Protocol):
    def estimate_seconds(self, node: dict, work_factor: float) -> float: ...


class TransportProvider(Protocol):
    def estimate_seconds(self, distance_m: float, *, remote: bool) -> tuple[float, float]: ...


class SchedulerProvider(Protocol):
    def select(self, candidates: Sequence[dict], *, origin: str, policy: str) -> str: ...


class SignalPolicy(Protocol):
    def decide(self, *, result_returned: bool, matching_green: bool, has_yellow: bool,
               already_extended: bool, spent: float, remaining: float,
               maximum_green: float, extension: float) -> tuple[float | None, str]: ...


class SignalActuator(Protocol):
    def extend(self, trafficlight, tls_id: str, duration: float) -> None: ...


class SyntheticPerception:
    def infer(self, frame):
        return PerceptionResult(frame.vehicle_id, frame.rsu_id, frame.lane_id,
                                'vehicle', None, 'sumo_ideal_lane_observation',
                                'demo-synthetic-v1', True)


class SyntheticCompute:
    def estimate_seconds(self, node, work_factor):
        seconds = float(node['serviceTimeS']) * float(work_factor)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('compute estimate must be finite and positive')
        return seconds


class SyntheticTransport:
    def estimate_seconds(self, distance_m, *, remote):
        return ((0.28 + distance_m / 1600.0, 0.16 + distance_m / 2400.0)
                if remote else (0.04, 0.04))


class LeastFinishScheduler:
    def select(self, candidates, *, origin, policy):
        if policy not in ('local', 'least_finish'):
            raise ValueError('unknown scheduler policy')
        permitted = [candidate for candidate in candidates
                     if policy != 'local' or candidate['target'] == origin]
        if not permitted:
            raise ValueError('no permitted compute candidate')
        return min(permitted, key=lambda item: (item['finish'], item['target'] != origin,
                                                item['target']))['target']


class BoundedGreenPolicy:
    def decide(self, *, result_returned, matching_green, has_yellow, already_extended,
               spent, remaining, maximum_green=55.0, extension=4.0):
        if not result_returned:
            return None, '结果尚未返回'
        if has_yellow or not matching_green:
            return None, '检测车道当前非可延长绿灯'
        if already_extended:
            return None, '本绿相位已延长一次'
        extra = min(extension, maximum_green - spent - remaining)
        if extra <= 0.05:
            return None, '已达本演示最大绿灯时长'
        return remaining + extra, '已完成任务触发有界绿灯延长'


class TraCISignalActuator:
    """The only active actuator targets the local SUMO process."""
    def extend(self, trafficlight, tls_id, duration):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid green duration')
        trafficlight.setPhaseDuration(tls_id, duration)


class DisabledPhysicalSignalActuator:
    """Explicit fail-closed space for a future authenticated device integration."""
    def extend(self, trafficlight, tls_id, duration):
        raise RuntimeError('physical signal controller is not configured; no device command was sent')


def validate_perception_result(result, frame):
    if not isinstance(result, PerceptionResult):
        raise ValueError('adapter must return PerceptionResult')
    if result.vehicle_id != frame.vehicle_id or result.rsu_id != frame.rsu_id:
        raise ValueError('model result does not match submitted vehicle and RSU')
    if not isinstance(result.synthetic, bool):
        raise ValueError('synthetic must be a boolean')
    for value in (result.label, result.source, result.model_version):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError('label, source and model_version require 1–200 characters')
    if result.lane_id is not None and (not isinstance(result.lane_id, str) or len(result.lane_id) > 256):
        raise ValueError('invalid model lane ID')
    if result.confidence is not None and (isinstance(result.confidence, bool)
            or not isinstance(result.confidence, (int, float))
            or not math.isfinite(result.confidence) or not 0 <= result.confidence <= 1):
        raise ValueError('confidence must be null or a finite number in [0, 1]')
    if not result.synthetic and frame.audio_ref is None:
        raise ValueError('a real acoustic result requires an actual audio_ref input')
    for name, value in (('direction_degrees', result.direction_degrees), ('uncertainty_m', result.uncertainty_m)):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(name + ' must be null or a finite number')
    if result.direction_degrees is not None and not 0 <= result.direction_degrees < 360:
        raise ValueError('direction_degrees must be in [0, 360)')
    if result.uncertainty_m is not None and result.uncertainty_m < 0:
        raise ValueError('uncertainty_m must be non-negative')
    if result.position_m is not None and (not isinstance(result.position_m, (tuple, list))
            or len(result.position_m) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in result.position_m)):
        raise ValueError('position_m must be null or two finite SUMO coordinates')
    return result
