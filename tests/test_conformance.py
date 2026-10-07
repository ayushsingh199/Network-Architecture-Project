"""The interop checker passes against this repo's server."""

from __future__ import annotations

from bhp.conformance import run


def test_all_conformance_checks_pass(server: tuple[str, int]) -> None:
    results = run(*server, "/index.html")
    assert [name for name, failure in results if failure] == []
    assert len(results) == 8
