"""Second file group for long protected-lab subprocess scenarios.

The existing two-worker `--dist=loadfile` configuration schedules this file
independently of `test_protected_lab.py`, preserving all original scenario
windows, transport and cleanup assertions in the shared fixture.
"""

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "wrong_device",
        "reconnect_no_events",
        "get",
        "quota",
        "redirect",
        "tls",
    ],
)
def test_independent_protected_launcher_shard(case, protected_launcher):
    """Run the same full subprocess assertions as the original launcher test."""
    protected_launcher(case)
