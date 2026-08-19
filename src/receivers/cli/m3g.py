"""``receivers m3g`` — M3G (gnss-metadata.eu) site-log submission.

Exposes the M3G submission step (EPOS §3.2) as a standalone verb:

- ``receivers m3g submit --station RHOF``   → validate only (dry run, no publish)
- ``receivers m3g submit --station RHOF --publish`` → validate + **publish** to M3G
- ``receivers m3g validate --station RHOF``  → validate only (no token, no publish)
- ``receivers m3g diff --station RHOF``     → diff the local site log vs the live M3G version

**The M3G ``upload-sitelog`` API publishes directly** — there is no draft state
on the API path (the web UI's "Save all to draft" → "Submit saved draft for
publication" workflow is for manual form-editing only). So ``--publish`` is the
real publish trigger; without it the command only validates. The pre-upload
``validate`` call is the gate: a site log that fails M3G/EPOS validation is
never published.

See docs/architecture/epos-dissemination-plan.md (C6/T7).
"""

from __future__ import annotations

import argparse
import difflib
import logging
import sys
from pathlib import Path

logger = logging.getLogger("receivers.cli.m3g")


def _nine_char(station: str, country_code: str = "ISL", monument: str = "00") -> str:
    return f"{station.upper()}{monument}{country_code.upper()}"


def _print_validation(vr) -> None:
    """Render a ValidationResult to stdout for the operator."""
    if vr.ok:
        print(f"✅ validation OK (network={vr.network}, HTTP {vr.status_code})")
        return
    print(f"❌ validation FAILED (network={vr.network}, HTTP {vr.status_code})")
    errs = getattr(vr, "errors", []) or []
    warns = getattr(vr, "warnings", []) or []
    if errs:
        print(f"   {len(errs)} error(s):")
        for m in errs:
            f = m.get("field", "")
            msg = m.get("message", "")
            print(f"      • {f}: {msg}" if f else f"      • {msg}")
    if warns:
        print(f"   {len(warns)} warning(s):")
        for m in warns:
            f = m.get("field", "")
            msg = m.get("message", "")
            print(f"      • {f}: {msg}" if f else f"      • {msg}")
    if not errs and not warns:
        # 422 with no parseable messages — show the raw body for debugging.
        raw = getattr(vr, "raw", None)
        if raw is not None:
            print(f"   raw response: {str(raw)[:500]}")


def cmd_m3g_validate(args: argparse.Namespace) -> int:
    """Validate a locally generated site log against M3G network rules (no token)."""
    from ..dissemination.m3g_client import M3GClient, M3GError, read_sitelog
    from ..dissemination.sitelogs import generate_site_log, resolve_sitelogs_repo

    sid = args.station.upper()
    content: str
    src: str
    if args.file:
        content = read_sitelog(args.file)
        src = args.file
    else:
        out_dir = (
            Path(args.sitelog_dir) if args.sitelog_dir else resolve_sitelogs_repo()
        )
        path = generate_site_log(sid, out_dir)
        if path is None:
            print(f"Site log generation failed for {sid} (see log).")
            return 1
        content = read_sitelog(path)
        src = str(path)

    print(
        f"validating {sid} ({len(content)} bytes, src={src}) against M3G/{args.network}…"
    )
    client = M3GClient(endpoint=args.m3g_endpoint)
    try:
        vr = client.validate_sitelog(content, network=args.network)
    except M3GError as exc:
        print(f"❌ validate: {exc}")
        return 1
    _print_validation(vr)
    return 0 if vr.ok else 1


def cmd_m3g_submit(args: argparse.Namespace) -> int:
    """Validate + publish a site log to M3G (``--publish`` triggers the actual PUT)."""
    from ..dissemination.m3g_client import M3GError
    from ..dissemination.sitelogs import submit_to_m3g

    sid = args.station.upper()
    dry_run = not args.publish
    site_log_path = Path(args.file) if args.file else None

    action = (
        "DRY RUN (validate only)" if dry_run else "PUBLISH (validate + publish to M3G)"
    )
    print(f"M3G {action} for {sid} (endpoint resolved from --m3g-endpoint/config)…")

    try:
        result = submit_to_m3g(
            sid,
            site_log_path=site_log_path,
            out_dir=Path(args.sitelog_dir) if args.sitelog_dir else None,
            network=args.network,
            country_code=args.country_code,
            monument_number=args.monument_number,
            dry_run=dry_run,
            endpoint=args.m3g_endpoint,
            skip_validation=args.skip_validation,
        )
    except M3GError as exc:
        print(f"❌ {exc}")
        return 1

    # 1. Validation phase
    if result.validation is not None:
        _print_validation(result.validation)
        if not result.validated:
            print("\n⚠️  publish skipped — fix the validation errors above first.")
            return 1
    elif args.skip_validation:
        print("   (validation skipped via --skip-validation)")

    # 2. Publish phase — note: the M3G upload-sitelog API PUBLISHES directly.
    ur = result.upload
    if ur is None:
        # validate-only path or a skip (e.g. generation failed)
        if result.skipped:
            print(f"   ⚠ skipped: {result.skipped}")
        return 0 if result.validated else 1

    if ur.dry_run:
        print(f"\n✅ DRY RUN complete — site log NOT published for {sid}.")
        print("   Pass --publish to validate + publish to M3G.")
        return 0

    if not ur.ok:
        print(
            # Always show the HTTP status alongside the message. The message
            # alone degrades to a bare "upload failed" whenever M3G's JSON body
            # has no `message`/`error` key, which hid a 404 "Object not found"
            # (a station never published to M3G, so there is no object to
            # update) behind text that reads like a transport problem.
            f"\n❌ publish FAILED for {sid}: "
            f"HTTP {ur.status_code} — {ur.error or '(no message from M3G)'}"
        )
        return 1

    print(f"\n✅ PUBLISHED to M3G for {sid} (HTTP {ur.status_code}).")
    if ur.md5_sitelog:
        print(f"   md5:      {ur.md5_sitelog}")
    if ur.sitelog_name:
        print(f"   filename: {ur.sitelog_name}")
    if ur.date_update:
        print(f"   updated:  {ur.date_update}")

    # The post-upload yellow 'Alert(s)' banners are web-UI-only (not in the API
    # response). Print the station URL so the operator can review them.
    print("\n   🔔 Review the station + post-upload alerts (not available via API):")
    print(f"      {ur.draft_url}")
    return 0


def cmd_m3g_diff(args: argparse.Namespace) -> int:
    """Diff the locally generated site log against the live M3G draft."""
    from ..dissemination.m3g_client import M3GClient, nine_char_id, read_sitelog
    from ..dissemination.sitelogs import generate_site_log, resolve_sitelogs_repo

    sid = args.station.upper()
    if args.file:
        local = read_sitelog(args.file)
    else:
        out_dir = (
            Path(args.sitelog_dir) if args.sitelog_dir else resolve_sitelogs_repo()
        )
        path = generate_site_log(sid, out_dir)
        if path is None:
            print(f"Site log generation failed for {sid} (see log).")
            return 1
        local = read_sitelog(path)

    client = M3GClient(endpoint=args.m3g_endpoint)
    nine = nine_char_id(sid, args.country_code, args.monument_number)
    remote = client.view_sitelog(
        sid,
        country_code=args.country_code,
        monument_number=args.monument_number,
    )
    if remote is None:
        # Genuinely absent now that the endpoint is right — this used to fire for
        # every station, including published ones, because /sitelog/view does not
        # exist on M3G.
        print(f"❌ no live M3G site log for {nine} — not published yet.")
        return 1

    local_lines = local.splitlines(keepends=True)
    remote_lines = remote.splitlines(keepends=True)
    diff = difflib.unified_diff(
        remote_lines,
        local_lines,
        fromfile=f"m3g:{sid}",
        tofile=f"local:{sid}",
    )
    out = "".join(diff)
    if not out:
        print(f"✅ {sid}: local site log is identical to the live M3G draft.")
        return 0
    sys.stdout.write(out)
    return 0


def create_m3g_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "m3g",
        help="M3G site-log submission (validate / publish / diff).",
        description=(
            "M3G (gnss-metadata.eu) site-log submission. The M3G upload-sitelog "
            "API PUBLISHES directly — there is no draft state on the API path. "
            "`m3g submit` is dry-run (validate only) by default; pass --publish "
            "to publish. The pre-upload validate is the gate: a site log that "
            "fails M3G/EPOS validation is never published."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  receivers m3g validate --station RHOF          # check against EPOS rules\n"
            "  receivers m3g submit --station RHOF           # dry run: validate only\n"
            "  receivers m3g submit --station RHOF --publish  # validate + publish to M3G\n"
            "  receivers m3g submit --station RHOF --publish --m3g-endpoint test\n"
            "  receivers m3g diff --station RHOF              # local vs live M3G version\n"
        ),
    )
    m3g_sub = parser.add_subparsers(
        dest="m3g_command", title="m3g subcommands", description="Available m3g actions"
    )

    # Common args reused across subcommands
    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--station", required=True, help="4-char station id (e.g. RHOF)")
        p.add_argument(
            "--file",
            help="Use this existing site log file instead of rendering from TOS",
        )
        p.add_argument(
            "--sitelog-dir",
            help="Output dir when rendering (default: gps-sitelogs repo)",
        )
        p.add_argument(
            "--m3g-endpoint", help="M3G endpoint URL or alias: prod (default) / test"
        )
        p.add_argument(
            "--network", default="EPOS", help="M3G network short name (default: EPOS)"
        )

    # validate
    p_val = m3g_sub.add_parser(
        "validate",
        help="Validate a site log against M3G network rules (no token, no upload)",
    )
    add_common(p_val)
    p_val.set_defaults(func=cmd_m3g_validate)

    # submit
    p_sub = m3g_sub.add_parser(
        "submit", help="Validate + (with --publish) publish a site log to M3G"
    )
    add_common(p_sub)
    p_sub.add_argument(
        "--publish",
        action="store_true",
        help="Publish to M3G (default: dry run / validate only). NOTE: the M3G API "
        "publishes directly — there is no draft state.",
    )
    p_sub.add_argument(
        "--country-code", default="ISL", help="Country code (default: ISL)"
    )
    p_sub.add_argument(
        "--monument-number", default="00", help="Monument number (default: 00)"
    )
    p_sub.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip the pre-upload validate step",
    )
    p_sub.set_defaults(func=cmd_m3g_submit)

    # diff
    p_diff = m3g_sub.add_parser(
        "diff", help="Diff the local site log vs the live M3G draft"
    )
    add_common(p_diff)
    # Needed to build the nine-char id the exportlog endpoint keys on.
    p_diff.add_argument(
        "--country-code", default="ISL", help="Country code (default: ISL)"
    )
    p_diff.add_argument(
        "--monument-number", default="00", help="Monument number (default: 00)"
    )
    p_diff.set_defaults(func=cmd_m3g_diff)

    return parser
