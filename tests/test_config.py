from __future__ import annotations

import pytest
from pydantic import ValidationError

from simlab.config import DistributionConfig, SimulationConfig


def test_invalid_triangular_distribution() -> None:
    with pytest.raises(ValidationError):
        DistributionConfig(kind="triangular", low=3, mode=2, high=5)


def test_duplicate_station_names() -> None:
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(
            {
                "until": 10,
                "arrival_interarrival": {"kind": "deterministic", "value": 1},
                "stations": [
                    {
                        "name": "same",
                        "service_time": {"kind": "deterministic", "value": 1},
                    },
                    {
                        "name": "same",
                        "service_time": {"kind": "deterministic", "value": 1},
                    },
                ],
            }
        )


def test_zero_interarrival_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(
            {
                "until": 10,
                "arrival_interarrival": {"kind": "deterministic", "value": 0},
                "stations": [
                    {
                        "name": "server",
                        "service_time": {"kind": "deterministic", "value": 1},
                    }
                ],
            }
        )
