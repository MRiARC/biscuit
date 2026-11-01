"""Memory measurements remain partial and are never equated with 8 GiB compliance."""
from __future__ import annotations

from biscuit.evaluate.fixtures import load_simulated_predictions
from biscuit.evaluate.profile import HostMemorySampler
from biscuit.evaluate.runner import run_benchmark


def test_memory_sampler_records_peak_but_never_certifies_host_target() -> None:
    readings = iter([320.0, 480.0, 410.0])
    sampler = HostMemorySampler(
        interval_seconds=0.05, source=lambda: next(readings, 410.0)
    )
    with sampler:
        sampler._read()
    result = sampler.results()
    assert result["peak_host_used_mib"] == 480.0
    assert result["measurement_status"] == "PARTIAL_HOST_SAMPLES_ONLY"
    assert result["full_stack_8gib_verified"] is False
    assert result["sample_count"] >= 3


def test_memory_sampler_is_not_applied_to_synthetic_recorded_replay() -> None:
    outcome = run_benchmark("pep-505", recorded_replay=True, profile_host_memory=True)
    assert outcome.memory_profile["measurement_status"] == "NOT_MEASURED"


def test_memory_profile_wrapping_is_explicit_and_testable(monkeypatch) -> None:
    from biscuit.evaluate import runner

    class FakeSampler:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def results(self):
            return {
                "measurement_status": "PARTIAL_HOST_SAMPLES_ONLY",
                "peak_host_used_mib": 2048.0,
                "full_stack_8gib_verified": False,
            }

    monkeypatch.setattr(runner, "HostMemorySampler", FakeSampler)
    outcome = run_benchmark(
        "pep-505", predictor=lambda _: load_simulated_predictions("pep-505"),
        profile_host_memory=True,
    )
    assert outcome.memory_profile["peak_host_used_mib"] == 2048.0
    assert outcome.memory_profile["full_stack_8gib_verified"] is False
    assert outcome.verdict == "PASSED"
