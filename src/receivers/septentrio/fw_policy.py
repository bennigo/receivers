"""Firmware-version policy for talking to Septentrio receivers.

Every path that opens a connection to a PolaRx5 must know the station's
firmware, because fw 5.7.0 changed the rules:

* **fw ≥ 5.7.0** — authentication is ENFORCED. Without a successful ``login``
  the receiver rejects every subsequent command, so a failed login must be a
  hard error; reporting success afterwards is a lie.
* **fw < 5.7.0** — the ``login`` command exists but is NOT enforced;
  unauthenticated commands work regardless. A rejected login here is benign,
  and warning about "wrong credentials" before succeeding anyway is just
  misleading noise.
* **fw ≤ 5.5.0** — ``login`` does not exist at all (``$E: Invalid command``).

Getting this wrong is not cosmetic. Repeated rejected logins accumulate against
the receiver's brute-force counter, and on 5.7.0 that produces a **global,
~9-hour lockout that locks out every user including the factory account**. That
is exactly how ISAK became unreachable on 2026-08-01: the scheduler had been
firing ~10 rejected logins every 5 minutes for as long as cfg said 5.6.0, which
was harmless until the box was upgraded and the same traffic started counting.

So: when the firmware is KNOWN to be pre-5.7, do not send ``login`` at all.
There is nothing to gain (auth is not enforced) and a lockout to lose.

This module is the single source of that policy. It lives under ``septentrio``
rather than ``health`` because it governs all receiver comms — the download
manager, the config-push TCP client and the health extractor were previously
each reaching into ``health.polarx5_tcp_extractor`` for a private helper.
"""

from __future__ import annotations

from typing import Optional

#: First firmware release that enforces TCP authentication.
AUTH_REQUIRED_FROM = (5, 7, 0)


def parse_firmware(firmware_version: Optional[str]) -> Optional[tuple]:
    """Return ``(major, minor, patch)`` or ``None`` when unparseable."""
    if not firmware_version:
        return None
    try:
        return tuple(int(x) for x in str(firmware_version).strip().split("."))
    except (ValueError, AttributeError):
        return None


def firmware_requires_auth(firmware_version: Optional[str]) -> bool:
    """True when this firmware ENFORCES TCP auth (fw ≥ 5.7.0).

    Unknown/unparseable → ``True``: assume auth is required, because the cost
    of guessing wrong in that direction is a confusing failure, whereas
    guessing "no auth needed" on a 5.7.0 box means every command is silently
    rejected while the caller reports success.
    """
    parsed = parse_firmware(firmware_version)
    if parsed is None:
        return True
    return parsed >= AUTH_REQUIRED_FROM


def should_attempt_login(firmware_version: Optional[str]) -> bool:
    """True ONLY when the firmware is KNOWN to be >= 5.7.0.

    Login is the sole thing that feeds the receiver's brute-force counter, and
    on 5.7.0 a tripped counter is a GLOBAL lockout — every user including the
    factory account, blocking ``rec-provision`` itself. So a login is sent only
    where it can actually succeed.

    **Unknown firmware does NOT attempt a login** (changed 2026-08-17). The
    earlier default was "unknown -> try", on the reasoning that a stale
    stations.cfg would otherwise silently break auth. That reasoning is obsolete:
    the extractor detects the condition at the COMMAND level instead —
    ``"Not authorized"`` raises a warning naming the exact fix
    (``polarx5_tcp_extractor.py:1153-1161``), which is how ELDC's post-flash
    state was diagnosed. The failure is loud, self-describing and clears with one
    ``rec-provision``; the lockout it replaces cost 3.5 h of downtime and blocked
    its own remedy.

    Measured on rek-d01 2026-08-17: 113 Septentrio stations — 91 pre-5.7 (stop
    logging in), 18 on 5.7.0 (unchanged), 4 unknown (ICEB/ICEC/INGC/NORV, all
    ``station_status = inactive`` with zero health samples in 7 days).
    """
    parsed = parse_firmware(firmware_version)
    if parsed is None:
        return False  # unknown — do NOT risk the counter
    return parsed >= AUTH_REQUIRED_FROM
