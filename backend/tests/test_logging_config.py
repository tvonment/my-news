"""Third-party loggers stay quiet inside the Functions worker.

Regression: `function_app` silenced azure-core's HTTP logger but not the Cosmos
SDK's own, `azure.cosmos._cosmos_http_logging_policy`, which dumps every
request's URL and headers at INFO. That one logger produced 941k traces and
~1.8 GB of ingestion in 30 days, almost all of the Log Analytics bill, and the
sampling it triggered dropped the run summaries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

import function_app  # noqa: F401  (importing it is what applies the levels)

EXPECTED = {
    "azure": logging.WARNING,
    "azure.cosmos._cosmos_http_logging_policy": logging.WARNING,
    "azure.core.pipeline.policies.http_logging_policy": logging.WARNING,
    "azure.identity": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "trafilatura": logging.ERROR,
}


@pytest.fixture(autouse=True)
def _permissive_root() -> Iterator[None]:
    """Test under a root logger at least as permissive as the worker's.

    Python's default root level is WARNING, so without this every assertion
    below would pass with the configuration deleted: an unconfigured logger
    simply inherits WARNING. The worker forwards the root at INFO or lower;
    DEBUG here exposes any logger that is only quiet by inheritance.
    """
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(previous)


@pytest.mark.parametrize(("name", "level"), sorted(EXPECTED.items()))
def test_effective_level(name: str, level: int) -> None:
    assert logging.getLogger(name).getEffectiveLevel() == level


def test_the_cosmos_http_policy_logs_where_we_silenced_it() -> None:
    # Guards the name, not just the list above: if an SDK upgrade moved the
    # policy onto a logger outside `azure`, the list would go on passing while
    # the header dumps came back. The policy asks the same question before it
    # formats a line.
    from azure.cosmos._cosmos_http_logging_policy import CosmosHttpLoggingPolicy

    assert not CosmosHttpLoggingPolicy().logger.isEnabledFor(logging.INFO)


def test_the_azure_core_http_policy_is_silent_too() -> None:
    from azure.core.pipeline.policies import HttpLoggingPolicy

    assert not HttpLoggingPolicy().logger.isEnabledFor(logging.INFO)
