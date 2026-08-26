"""Shared pytest fixtures for the receivers suite.

Two jobs: keep logging state from leaking between tests, and keep the
``manual/`` scripts out of collection entirely.
"""

from __future__ import annotations

import logging

import pytest

# ``tests/manual/`` holds operator-run scripts that talk to REAL receivers over
# the network. They are named ``test_*.py`` and define ``test_*`` functions, and
# ``pyproject.toml`` sets ``testpaths = ["tests"]`` — so without this line a bare
# ``pytest`` collects them and authenticates against production hardware.
# Run them deliberately instead: ``python tests/manual/test_natt_auth.py``.
collect_ignore_glob = ["manual/*"]


@pytest.fixture(autouse=True)
def isolate_receivers_logging():
    """Stop one test's logging setup from silencing ``caplog`` in the next.

    ``receivers.logging_config.setup_logging()`` sets
    ``logging.getLogger("receivers").propagate = False`` — correct in
    production, where the package owns its own handlers and must not
    double-log through the root logger. But it also latches a module-global
    ``_configured`` flag, so the FIRST test that triggers it reconfigures
    logging for the entire pytest process.

    ``caplog`` captures by installing a handler on the ROOT logger and
    relying on propagation. Once propagation is off, every later test that
    asserts on a ``receivers.*`` log record sees an empty ``caplog.text``
    while the message still appears in captured stderr — which reads as
    "the code stopped logging", not "the harness stopped listening".

    That is an order-dependent failure: the affected tests pass in isolation
    and fail in the suite, and which tests are affected changes whenever
    files are added or reordered. Two were failing this way
    (``test_scheduler_disabled_jobs``, ``test_tool_manager``); this fixture
    fixes the class rather than those two.
    """
    import receivers.logging_config as logging_config

    logger = logging.getLogger("receivers")
    saved_propagate = logger.propagate
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    saved_configured = logging_config._configured

    # Propagation ON for the duration: caplog cannot see anything without it,
    # and no test should depend on the package swallowing its own records.
    logger.propagate = True

    try:
        yield
    finally:
        logger.propagate = saved_propagate
        logger.level = saved_level
        logger.handlers[:] = saved_handlers
        logging_config._configured = saved_configured


def test_logging_isolation_fixture_is_active():
    """Guard the guard — the fixture is autouse, so this must hold everywhere.

    If it ever fails, `caplog` has gone blind for every test that runs after
    whichever one configured logging, and the symptom will look like
    unrelated assertions about missing log messages.
    """
    assert logging.getLogger("receivers").propagate is True
