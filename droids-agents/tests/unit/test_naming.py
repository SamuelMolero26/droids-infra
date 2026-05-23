"""NamePool + agent_display + claim_for_role tests."""

from __future__ import annotations

import pytest

from droids_agents import naming


@pytest.fixture
def pool() -> naming.NamePool:
    return naming.NamePool(names=["A", "B", "C"], seed=1)


def test_load_names_default_path_exists() -> None:
    p = naming._default_names_file()
    assert p.exists(), f"droids-name.yml missing at {p}"
    names = naming.load_names(p)
    assert "C-3PO" in names
    assert "R2-D2" in names


def test_claim_unique_within_one_pool(pool: naming.NamePool) -> None:
    claims = [pool.claim() for _ in range(3)]
    assert len(set(claims)) == 3
    assert set(claims) == {"A", "B", "C"}


def test_claim_exhausted_appends_suffix(pool: naming.NamePool) -> None:
    [pool.claim() for _ in range(3)]
    extra = pool.claim()
    assert extra.endswith("-2") or extra.endswith("-3") or extra.endswith("-4")


def test_release_recycles_base_name(pool: naming.NamePool) -> None:
    first = pool.claim()
    pool.release(first)
    rest = {pool.claim() for _ in range(3)}
    assert first in rest


def test_release_does_not_recycle_suffixed_name(pool: naming.NamePool) -> None:
    [pool.claim() for _ in range(3)]
    suffixed = pool.claim()
    pool.release(suffixed)
    further = [pool.claim() for _ in range(2)]
    assert suffixed not in further


def test_two_pools_diverge_without_seed() -> None:
    n = naming.load_names()
    a = naming.NamePool(names=n)
    b = naming.NamePool(names=n)
    head_a = [a.claim() for _ in range(5)]
    head_b = [b.claim() for _ in range(5)]
    assert head_a != head_b, "two unseeded pools should differ (probabilistically)"


def test_agent_display_known_role() -> None:
    assert naming.agent_display("C-3PO", "competitor") == "C-3PO: [Researcher]"


def test_agent_display_unknown_role_falls_back_to_key() -> None:
    assert naming.agent_display("R2-D2", "weird") == "R2-D2: [weird]"


def test_claim_for_role_returns_full_metadata_bundle(pool: naming.NamePool) -> None:
    md = naming.claim_for_role(pool, "competitor")
    assert set(md.keys()) == {"droid_name", "role", "role_label", "agent_display"}
    assert md["role"] == "competitor"
    assert md["role_label"] == "Researcher"
    assert md["agent_display"] == f"{md['droid_name']}: [Researcher]"


def test_load_names_rejects_missing_yaml(tmp_path) -> None:
    bad = tmp_path / "nope.yml"
    with pytest.raises(naming.NamePoolError):
        naming.load_names(bad)


def test_load_names_rejects_malformed_yaml(tmp_path) -> None:
    p = tmp_path / "bad.yml"
    p.write_text("not_a_droids_key: []", encoding="utf-8")
    with pytest.raises(naming.NamePoolError):
        naming.load_names(p)
