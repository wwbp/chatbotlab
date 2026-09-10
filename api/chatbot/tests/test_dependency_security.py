"""
Minimum versions for runtime dependencies with known advisories.

A lock file can quietly regress — a re-lock, a merge, or a transitive bump can
pull a vulnerable version back in, and Dependabot only notices after the fact.
These assertions fail the build instead.

Runtime dependencies only. Test and build tooling is not shipped to
participants, and one of them (pytest) is deliberately held below its patched
version because pytest 9 breaks pytest-asyncio — see the comment in Pipfile.
Encoding a floor we cannot meet would just mean a permanently red suite.
"""

from importlib.metadata import version

from packaging.version import Version

# package -> (minimum safe version, why)
SECURITY_FLOORS = {
    # GHSA-prg7-hcfm-mfcr, GHSA-pwgv-4x5q-6m9f, GHSA-f2ff-p2ww-7p4p (high) and
    # GHSA-cfqr-cjx5-5jcm, GHSA-3496-9g83-7v6x (medium): ReDoS and quadratic
    # CPU blowups parsing SQL, plus a string breakout in generated snippets.
    # Reached us transitively through Django.
    "sqlparse": ("0.6.0", "5 advisories, all fixed in 0.6.0"),
}


def test_runtime_dependencies_meet_security_floors():
    stale = []
    for package, (floor, reason) in SECURITY_FLOORS.items():
        installed = version(package)
        if Version(installed) < Version(floor):
            stale.append(f"{package} {installed} < {floor} — {reason}")

    assert not stale, "Vulnerable runtime dependencies installed:\n  " + "\n  ".join(
        stale
    )
