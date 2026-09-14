from __future__ import annotations

from simlab.rng import derive_seed


def test_namespaced_seed_is_stable_and_independent() -> None:
    first = derive_seed(42, "service:registration")
    assert first == derive_seed(42, "service:registration")
    assert first != derive_seed(42, "service:specialist")
    assert first != derive_seed(43, "service:registration")
