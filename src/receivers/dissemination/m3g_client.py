"""M3G (gnss-metadata.eu) API client for site-log submission.

EPOS §3.2 makes M3G the canonical metadata registry. Stations must be kept
current within one business day of any TOS change. This module is the
thin HTTP client over the M3G REST API (v1.3/v1.4):

- :meth:`M3GClient.validate_sitelog` → ``PUT /sitelog/validate-sitelog``
  (checks the site log against a network's rules, e.g. EPOS). No auth.
- :meth:`M3GClient.upload_sitelog` → ``PUT /sitelog/upload-sitelog?id=…``
  (uploads the site log text; **publishes it directly**). Requires the
  agency's *Application Access Token* (bearer).

**The ``upload-sitelog`` API publishes directly** — there is no draft state
on the API path. The web UI's "Save all to draft" → "Submit saved draft for
publication" workflow is for manual form-editing only; the API bypasses it.
So :meth:`upload_sitelog` is the real publish trigger. The pre-upload
:meth:`validate_sitelog` call is the gate: a site log that fails M3G/EPOS
validation is never published.

Credential resolution order (highest wins):
1. Constructor arg ``token``
2. ``M3G_TOKEN`` environment variable
3. ``[m3g]`` section in ``database.cfg``:
   - ``token_pass_path`` — retrieve from pass(1) store (recommended)
   - ``token`` — plaintext fallback (avoid)

Endpoint resolution: constructor arg → ``[m3g] endpoint`` in database.cfg →
:data:`DEFAULT_M3G_ENDPOINT` (production). Override to the test server via
``--m3g-endpoint test`` or ``[m3g] endpoint = test``.

See:
- API docs: https://gnss-metadata.eu/site/api-docs/
- Intro + examples: https://github.com/m3g-rob/doc4m3g
- Plan: docs/architecture/epos-dissemination-plan.md (C6/T7)
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger("receivers.dissemination.m3g")

# Production is the documented base; the test server is the ``__test`` prefix
# M3G uses in its own curl examples (see doc4m3g/docs/curl_upload.md).
DEFAULT_M3G_ENDPOINT = "https://gnss-metadata.eu/v1"
TEST_M3G_ENDPOINT = "https://gnss-metadata.eu/__test/v1"
_ENDPOINT_ALIASES = {"prod": DEFAULT_M3G_ENDPOINT, "test": TEST_M3G_ENDPOINT}


#: IGS site logs are ISO-8859-1 (Latin-1), not UTF-8. M3G serves Latin-1 back
#: (``file`` reports "ISO-8859 text" for every log fetched from
#: gnss-metadata.eu) and Latin-1 covers Icelandic completely (á é í ó ú ý þ æ ö ð).
SITELOG_ENCODING = "latin-1"


def decode_sitelog(raw: bytes) -> str:
    """Decode site-log bytes, tolerating the UTF-8 files written before the fix.

    Latin-1 decoding never raises — every byte is a valid code point — so a
    mis-encoded file cannot be detected by failure. UTF-8 decoding *does* raise
    on Latin-1 high bytes, which makes it a reliable discriminator: try UTF-8
    strict first, and fall back to Latin-1 when it fails. A pre-fix UTF-8 log
    (``ö`` as ``C3 B6``) decodes as UTF-8; a Latin-1 log (``ö`` as ``F6``) fails
    UTF-8 and falls through. Both end up as the same correct text.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(SITELOG_ENCODING)


def read_sitelog(path) -> str:
    """Read a site log from disk with :func:`decode_sitelog`."""
    from pathlib import Path as _Path

    return decode_sitelog(_Path(path).read_bytes())


def nine_char_id(
    station_id: str, country_code: str = "ISL", monument_number: str = "00"
) -> str:
    """IGS/M3G nine-character id: ``RHOF`` -> ``RHOF00ISL``.

    Idempotent: a value that is already nine characters is returned unchanged,
    so callers may pass either form.
    """
    sid = station_id.strip().upper()
    if len(sid) == 9:
        return sid
    mon = str(monument_number)[:2].rjust(2, "0")
    return f"{sid}{mon}{country_code.upper()}"


DEFAULT_TIMEOUT = 30
DEFAULT_NETWORK = "EPOS"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of ``PUT /sitelog/validate-sitelog``.

    M3G returns the validation verdict as JSON with a list of per-section
    messages. ``ok`` is True only when there are zero error-severity messages.
    """

    ok: bool
    network: str
    status_code: int
    raw: Any = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [
            m for m in self.messages if str(m.get("severity", "")).upper() == "ERROR"
        ]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [
            m for m in self.messages if str(m.get("severity", "")).upper() == "WARNING"
        ]


@dataclass
class UploadResult:
    """Outcome of ``PUT /sitelog/upload-sitelog``.

    On success, M3G returns the station's new draft metadata (md5, names,
    prepared/update dates, and ``_links`` to the view/export endpoints).
    """

    ok: bool
    station_id: str
    status_code: int
    dry_run: bool = False
    md5_sitelog: Optional[str] = None
    sitelog_name: Optional[str] = None
    prepared_date: Optional[str] = None
    date_update: Optional[str] = None
    links: dict[str, str] = field(default_factory=dict)
    raw: Any = None
    error: Optional[str] = None

    @property
    def draft_url(self) -> str:
        """Web-UI deep link to the station's draft editor (where the yellow
        post-upload 'Alert(s)' banners appear — not reachable via API)."""
        # Strip the /v1 or /v14 API suffix to get the portal root, then build
        # the modify URL the operator clicks to review + publish the draft.
        base = self._portal_root()
        return f"{base}/sitelog/modify?station={self.station_id}&sender=stationOverview"

    def _portal_root(self) -> str:
        # M3G API endpoints live under /v1, /v14, or /__test/v1; the portal
        # root is the host with no API path suffix.
        from urllib.parse import urlparse

        # Stored on the client at upload time via the links; fall back to the
        # default if absent (links usually carry _self which is the API view URL).
        api_url = self.links.get("self") or self.links.get("sitelog") or ""
        if api_url:
            parsed = urlparse(api_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        return "https://gnss-metadata.eu"


# ---------------------------------------------------------------------------
# Credential + endpoint resolution
# ---------------------------------------------------------------------------


def _find_database_cfg() -> Optional[Path]:
    """Locate ``database.cfg`` using the same search order as receivers/tostools."""
    candidates: list[Path] = []

    gps_config_env = os.environ.get("GPS_CONFIG_PATH")
    if gps_config_env:
        candidates.append(Path(gps_config_env) / "database.cfg")

    try:
        import gps_parser  # type: ignore[import]

        config_dir = gps_parser.ConfigParser().config_path
        if config_dir:
            candidates.append(Path(config_dir) / "database.cfg")
    except Exception:  # noqa: BLE001
        pass

    candidates.append(Path.home() / ".config" / "gpsconfig" / "database.cfg")

    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_from_pass(pass_spec: str) -> Optional[str]:
    """Return a value from a pass(1) entry, or None on any error.

    ``pass_spec`` is either a bare entry path (returns the first line — the
    token) or ``entry_path:field_name`` (returns a named field from the body).
    Stdout is captured and never logged; stderr is discarded.
    """
    if ":" in pass_spec:
        path_part, field_name = pass_spec.split(":", 1)
    else:
        path_part, field_name = pass_spec, None

    try:
        proc = subprocess.run(
            ["pass", "show", path_part],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - pass missing / locked / entry absent
        return None

    lines = proc.stdout.splitlines()
    if not lines:
        return None
    if field_name is None:
        return lines[0].strip()

    prefix = f"{field_name}:"
    for line in lines[1:]:
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix) :].strip()
    return None


def _resolve_endpoint(
    override: Optional[str],
    cfg_path: Optional[Path] = None,
) -> str:
    """Resolve the M3G API base URL.

    ``override`` may be a full URL or an alias (``prod`` / ``test``). When
    None, falls back to ``[m3g] endpoint`` in database.cfg, then the default.
    """
    if override:
        return _ENDPOINT_ALIASES.get(override.lower(), override.rstrip("/"))

    path = cfg_path or _find_database_cfg()
    if path is not None:
        try:
            cp = configparser.ConfigParser()
            cp.read(path)
            raw = cp.get("m3g", "endpoint", fallback=None)
            if raw:
                return _ENDPOINT_ALIASES.get(
                    raw.strip().lower(), raw.strip().rstrip("/")
                )
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_M3G_ENDPOINT


def _resolve_token(
    token: Optional[str],
    cfg_path: Optional[Path] = None,
) -> Optional[str]:
    """Resolve the M3G application access token.

    Order: explicit arg → ``M3G_TOKEN`` env → ``[m3g]`` section in
    database.cfg (``token_pass_path`` preferred over plaintext ``token``).
    """
    if token:
        return token
    env_tok = os.environ.get("M3G_TOKEN")
    if env_tok:
        return env_tok

    path = cfg_path or _find_database_cfg()
    if path is None:
        return None
    try:
        cp = configparser.ConfigParser()
        cp.read(path)
        if not cp.has_section("m3g"):
            return None
        pass_path = cp.get("m3g", "token_pass_path", fallback=None)
        if pass_path:
            tok = _load_from_pass(pass_path.strip())
            if tok:
                return tok
        return cp.get("m3g", "token", fallback=None) or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class M3GError(Exception):
    """Raised for unrecoverable M3G API failures (network, auth, parse)."""


class M3GClient:
    """Authenticated client for the M3G REST API.

    The token is resolved lazily on first mutating call and kept in memory
    only — never written to disk. :meth:`validate_sitelog` is unauthenticated
    and works without a token; :meth:`upload_sitelog` requires one.

    Args:
        endpoint: M3G base URL (full URL or ``prod``/``test`` alias). None
            → resolve from ``[m3g] endpoint`` / default to production.
        token: Application access token. None → resolve from env/database.cfg.
        cfg_path: Explicit ``database.cfg`` path. Auto-discovered if None.
        timeout: HTTP timeout in seconds.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        *,
        cfg_path: Optional[Path] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.endpoint = _resolve_endpoint(endpoint, cfg_path)
        self._cfg_path = cfg_path
        self._token: Optional[str] = token
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return self.endpoint

    def _ensure_token(self) -> str:
        tok = _resolve_token(self._token, self._cfg_path)
        if not tok:
            raise M3GError(
                "M3G application access token not found. Set M3G_TOKEN, or add a "
                "[m3g] section with token_pass_path (recommended) or token to "
                "database.cfg. The token is shown in M3G under 'Edit My Agency "
                "Information'."
            )
        self._token = tok
        return tok

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # -- read: validate (no auth) ------------------------------------------

    def validate_sitelog(
        self,
        content: str,
        *,
        network: str = DEFAULT_NETWORK,
    ) -> ValidationResult:
        """Validate a site log against a network's rules (``PUT /sitelog/validate-sitelog``).

        No authentication required. M3G returns:
        - ``200`` → a parsed :class:`SitelogForm` (the log is valid); this is the
          **pre-upload gate** that catches malformed logs and bad field values.
        - ``422`` → a bare array of ``ValidationError {field, message}`` (the
          hard errors that would block an upload).

        Returns a :class:`ValidationResult`; ``ok`` is True only on HTTP 200.
        The 422 errors are surfaced in :attr:`ValidationResult.errors`.
        """
        url = f"{self.endpoint}/sitelog/validate-sitelog"
        params = {"network": network}
        try:
            resp = requests.put(
                url,
                params=params,
                # UTF-8 on the WIRE, deliberately, even though the site log
                # itself is Latin-1 on disk and Latin-1 is what M3G serves back.
                # Sending Latin-1 here makes the API answer HTTP 500 (measured
                # 2026-08-18). Do not "fix" this to match SITELOG_ENCODING.
                data=content.encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise M3GError(f"validate: request failed: {exc}") from exc

        raw: Any = None
        messages: list[dict[str, Any]] = []
        try:
            if resp.content:
                raw = resp.json()
        except ValueError:
            pass  # non-JSON body — leave messages empty

        # 422 → bare array of {field, message}; treat each as an ERROR.
        # 200 → the SitelogForm (valid); no per-field messages to extract.
        if isinstance(raw, list):
            messages = [
                {
                    "field": m.get("field", ""),
                    "message": m.get("message", ""),
                    "severity": "ERROR",
                }
                for m in raw
                if isinstance(m, dict)
            ]
        elif isinstance(raw, dict):
            # Defensive: some M3G versions wrap errors in a dict.
            msgs = raw.get("messages") or raw.get("validationMessages") or []
            if isinstance(msgs, list):
                messages = [m for m in msgs if isinstance(m, dict)]

        ok = resp.ok and not messages
        if not resp.ok:
            logger.warning(
                "m3g validate (network=%s): HTTP %s, %d error(s)",
                network,
                resp.status_code,
                len(messages),
            )
        return ValidationResult(
            ok=ok,
            network=network,
            status_code=resp.status_code,
            raw=raw,
            messages=messages,
        )

    # -- write: upload as draft (auth) -------------------------------------

    def upload_sitelog(
        self,
        station_id: str,
        content: str,
        *,
        dry_run: bool = True,
    ) -> UploadResult:
        """Publish a site log to M3G (``PUT /sitelog/upload-sitelog?id=…``).

        Requires the agency application access token. In ``dry_run`` mode
        (default) the request is **not** sent — only validation feedback would
        have been gathered; the operator must pass ``dry_run=False`` to
        actually publish. **The M3G API publishes directly — there is no draft
        state**, so a non-dry-run call makes the content live immediately.

        Returns an :class:`UploadResult`.
        """
        sid = station_id.upper()
        if dry_run:
            logger.info(
                "m3g upload %s: DRY RUN — would PUT %s/sitelog/upload-sitelog?id=%s "
                "(%d bytes)",
                sid,
                self.endpoint,
                sid,
                len(content),
            )
            return UploadResult(
                ok=True,
                station_id=sid,
                status_code=0,
                dry_run=True,
            )

        url = f"{self.endpoint}/sitelog/upload-sitelog"
        params = {"id": sid}
        try:
            resp = requests.put(
                url,
                params=params,
                # UTF-8 on the WIRE, deliberately, even though the site log
                # itself is Latin-1 on disk and Latin-1 is what M3G serves back.
                # Sending Latin-1 here makes the API answer HTTP 500 (measured
                # 2026-08-18). Do not "fix" this to match SITELOG_ENCODING.
                data=content.encode("utf-8"),
                headers={
                    **self._auth_headers(),
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            return UploadResult(
                ok=False,
                station_id=sid,
                status_code=0,
                dry_run=False,
                error=f"request failed: {exc}",
            )

        if resp.status_code == 401:
            raise M3GError(
                "M3G rejected the token (HTTP 401). Check that the token in "
                "[m3g] / M3G_TOKEN is the current 'Application access token' "
                "from M3G 'Edit My Agency Information'."
            )

        raw: Any = None
        try:
            raw = resp.json() if resp.content else None
        except ValueError:
            pass

        if not resp.ok:
            msg = "upload failed"
            if isinstance(raw, dict):
                msg = str(raw.get("message") or raw.get("error") or msg)
            return UploadResult(
                ok=False,
                station_id=sid,
                status_code=resp.status_code,
                dry_run=False,
                raw=raw,
                error=msg,
            )

        data = raw if isinstance(raw, dict) else {}
        links = data.get("_links") or {}
        if not isinstance(links, dict):
            links = {}
        return UploadResult(
            ok=True,
            station_id=sid,
            status_code=resp.status_code,
            dry_run=False,
            md5_sitelog=data.get("md5Sitelog"),
            sitelog_name=data.get("sitelogName"),
            prepared_date=data.get("preparedDate"),
            date_update=data.get("dateUpdate"),
            links={
                k: (v.get("href") if isinstance(v, dict) else str(v))
                for k, v in links.items()
            },
            raw=raw,
        )

    # -- read: view (auth-free for public stations) -----------------------

    def _portal_root(self) -> str:
        """Root for the non-API paths (``/sitelog/exportlog``).

        API endpoints carry a trailing ``/v1`` / ``/v14`` segment; the portal
        paths sit one level up. The ``__test`` prefix is preserved, so a client
        pointed at the test endpoint does not silently read production.

        ``https://gnss-metadata.eu/v1``         -> ``https://gnss-metadata.eu``
        ``https://gnss-metadata.eu/__test/v1``  -> ``https://gnss-metadata.eu/__test``
        """
        return re.sub(r"/v\d+/?$", "", self.endpoint)

    def view_sitelog(
        self,
        station_id: str,
        *,
        country_code: str = "ISL",
        monument_number: str = "00",
    ) -> Optional[str]:
        """Fetch the live M3G site log text, or None when the station has none.

        Uses ``/sitelog/exportlog?station=<nine-char>``. This previously requested
        ``/sitelog/view?id=<four-char>``, which does not exist on M3G: every call
        got a 404 HTML page, so ``m3g diff`` reported "station may not exist yet"
        for EVERY station, including ones demonstrably published. Verified
        2026-08-18 against the live service:

            /sitelog/view?id=RHOF                 -> 404 (HTML)
            /sitelog/view?id=RHOF00ISL            -> 404 (HTML)
            /sitelog/exportlog?station=RHOF00ISL  -> 200, 11 kB site log

        A 4-char marker is expanded to the nine-char id; an id that is already
        nine characters is used as given.
        """
        nine = nine_char_id(station_id, country_code, monument_number)
        url = f"{self._portal_root()}/sitelog/exportlog"
        try:
            resp = requests.get(
                url,
                params={"station": nine},
                headers={"Accept": "application/sitelog"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("m3g exportlog %s: %s", nine, exc)
            return None
        if not resp.ok:
            return None
        # Decode from bytes: requests guesses the charset from headers, and M3G
        # serves Latin-1 without always declaring it — a wrong guess turns every
        # Icelandic character into U+FFFD and makes the diff show phantom changes.
        text = decode_sitelog(resp.content)
        # M3G serves an HTML error page for an unknown station. Guard on the body
        # as well as the status: returning that as "the live site log" would make
        # a diff show the entire portal page as a change.
        if text.lstrip().startswith("<"):
            logger.debug("m3g exportlog %s: HTML body, treating as absent", nine)
            return None
        return text
