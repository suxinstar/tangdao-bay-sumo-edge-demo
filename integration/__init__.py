"""Replaceable model contracts; no learned weights or device credentials bundled."""
from .providers import (
    AudioFrame, PerceptionResult, SyntheticPerception, SyntheticCompute, SyntheticTransport,
    LeastFinishScheduler, BoundedGreenPolicy, TraCISignalActuator,
    DisabledPhysicalSignalActuator, validate_perception_result,
)
from .gateway import ModelGateway

__all__ = [
    'AudioFrame', 'PerceptionResult', 'SyntheticPerception', 'SyntheticCompute', 'SyntheticTransport',
    'LeastFinishScheduler', 'BoundedGreenPolicy', 'TraCISignalActuator',
    'DisabledPhysicalSignalActuator', 'validate_perception_result', 'ModelGateway',
]
