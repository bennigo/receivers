"""Every `cfg` verb must stay wired to a handler.

`create_cfg_parser` was a single 3,329-line body holding all 27 verbs. It is
now one `_add_<verb>_parser` factory each, which makes the file navigable and
stops every new verb colliding in the same function — but it also introduces a
failure mode the monolith did not have: a factory can be added and never
called, or wired to the wrong handler, and `--help` still looks fine.

The split itself was verified by snapshotting the whole argparse tree (29
nodes, 422 actions, 27 `func` targets) before and after and diffing it to
byte-identity. These tests are the permanent version of that check: they assert
the properties that would actually break dispatch, rather than pinning a golden
file that any legitimate help-text edit would turn red.
"""

from __future__ import annotations

import argparse

import pytest


def _build():
    from receivers.cli.cfg import create_cfg_parser

    root = argparse.ArgumentParser(prog="receivers cfg")
    subs = root.add_subparsers(dest="cfg_command")
    create_cfg_parser(subs)
    action = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices["cfg"]


@pytest.fixture(scope="module")
def cfg_verbs():
    cfg = _build()
    sub = next(a for a in cfg._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices


def test_every_verb_dispatches_to_a_handler(cfg_verbs):
    """A missed set_defaults(func=...) makes a verb silently do nothing."""
    unwired = sorted(
        name for name, p in cfg_verbs.items() if not p._defaults.get("func")
    )
    assert unwired == [], f"cfg verbs with no func wired: {unwired}"


def test_no_two_verbs_share_a_handler(cfg_verbs):
    """Copy-paste between factories is easy; dispatching two verbs at one
    handler is the way that shows up."""
    seen: dict[str, str] = {}
    clashes = []
    for name, p in sorted(cfg_verbs.items()):
        fn = p._defaults.get("func")
        key = getattr(fn, "__name__", None)
        if key is None:
            continue
        if key in seen:
            clashes.append((seen[key], name, key))
        seen[key] = name
    assert clashes == [], f"verbs sharing a handler: {clashes}"


def test_the_expected_verb_set_is_registered(cfg_verbs):
    """Guards against a factory being defined but never called from
    create_cfg_parser — the split's characteristic failure."""
    expected = {
        "reconcile",
        "sync-from-tos",
        "list",
        "history",
        "extract",
        "add-tos-station",
        "add-receiver",
        "add-antenna",
        "add-monument",
        "import-campaigns",
        "set-continuity",
        "add-station",
        "discover-phone",
        "update-device",
        "move-device",
        "visit",
        "replace-receiver",
        "replace-modem",
        "replace-sim",
        "set-attr",
        "ensure-port-forwards",
        "ensure-conntrack-helper",
        "correct-date",
        "delete-join",
        "close-join",
        "replace-antenna",
        "replace-radome",
    }
    assert set(cfg_verbs) == expected


def test_every_factory_is_actually_called():
    """A `_add_*_parser` that nothing calls is dead weight that still reads as
    wired. Compare the definitions against the calls in create_cfg_parser."""
    import inspect
    import re

    from receivers.cli import cfg as mod

    defined = {n for n in dir(mod) if n.startswith("_add_") and n.endswith("_parser")}
    called = set(
        re.findall(
            r"(_add_\w+_parser)\(cfg_subparsers\)",
            inspect.getsource(mod.create_cfg_parser),
        )
    )
    assert defined - called == set(), (
        f"factories never called: {sorted(defined - called)}"
    )
