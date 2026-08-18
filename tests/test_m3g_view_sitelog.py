"""M3G site-log retrieval: endpoint, nine-char id, and absent-station handling.

``view_sitelog`` used to request ``/sitelog/view?id=<four-char>``, a path that
does not exist on M3G. Every call returned a 404 HTML page, so ``m3g diff``
reported "station may not exist yet" for EVERY station — including ones
demonstrably published. Verified against the live service 2026-08-18:

    /sitelog/view?id=RHOF                 -> 404 (HTML)
    /sitelog/view?id=RHOF00ISL            -> 404 (HTML)
    /sitelog/exportlog?station=RHOF00ISL  -> 200, 11 kB site log

These tests pin the request shape so a regression is caught offline, without
depending on the live portal.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.dissemination.m3g_client import M3GClient, nine_char_id


class TestNineCharId:
    def test_expands_a_four_char_marker(self):
        assert nine_char_id("rhof") == "RHOF00ISL"
        assert nine_char_id("ELDC") == "ELDC00ISL"

    def test_is_idempotent_on_an_already_nine_char_id(self):
        # Callers may pass either form; expanding twice would give RHOF00ISL00ISL.
        assert nine_char_id("RHOF00ISL") == "RHOF00ISL"

    def test_monument_is_zero_padded(self):
        assert nine_char_id("ELDC", "ISL", "0") == "ELDC00ISL"
        assert nine_char_id("ELDC", "ISL", "1") == "ELDC01ISL"

    def test_country_is_upcased(self):
        assert nine_char_id("eldc", "isl") == "ELDC00ISL"


class TestPortalRoot:
    def test_strips_the_api_version_segment(self):
        c = M3GClient(endpoint="https://gnss-metadata.eu/v1")
        assert c._portal_root() == "https://gnss-metadata.eu"

    def test_preserves_the_test_prefix(self):
        # Losing __test here would make a test-endpoint client silently read
        # production data.
        c = M3GClient(endpoint="https://gnss-metadata.eu/__test/v1")
        assert c._portal_root() == "https://gnss-metadata.eu/__test"

    def test_handles_a_two_digit_version(self):
        c = M3GClient(endpoint="https://gnss-metadata.eu/v14")
        assert c._portal_root() == "https://gnss-metadata.eu"


class TestViewSitelog:
    def _client(self):
        return M3GClient(endpoint="https://gnss-metadata.eu/v1")

    def test_requests_exportlog_with_the_nine_char_station_param(self):
        c = self._client()
        resp = MagicMock(ok=True, text="     RHOF00ISL Site Information Form\n")
        with patch(
            "receivers.dissemination.m3g_client.requests.get", return_value=resp
        ) as g:
            out = c.view_sitelog("RHOF")
        assert out.startswith("     RHOF00ISL")
        url = g.call_args[0][0]
        params = g.call_args[1]["params"]
        assert url == "https://gnss-metadata.eu/sitelog/exportlog"
        assert params == {"station": "RHOF00ISL"}

    def test_non_ok_response_is_absent_not_content(self):
        c = self._client()
        resp = MagicMock(ok=False, text="<!DOCTYPE html>…")
        with patch(
            "receivers.dissemination.m3g_client.requests.get", return_value=resp
        ):
            assert c.view_sitelog("ZZZZ") is None

    def test_html_body_with_200_is_also_treated_as_absent(self):
        # Returning a portal error page as "the live site log" would make the
        # diff show the whole HTML page as a change.
        c = self._client()
        resp = MagicMock(ok=True, text="\n<!DOCTYPE html>\n<html>…</html>")
        with patch(
            "receivers.dissemination.m3g_client.requests.get", return_value=resp
        ):
            assert c.view_sitelog("ZZZZ") is None

    def test_network_failure_is_absent(self):
        import requests as _requests

        c = self._client()
        with patch(
            "receivers.dissemination.m3g_client.requests.get",
            side_effect=_requests.RequestException("boom"),
        ):
            assert c.view_sitelog("RHOF") is None

    @pytest.mark.parametrize("marker", ["RHOF", "RHOF00ISL"])
    def test_either_id_form_reaches_the_same_url(self, marker):
        c = self._client()
        resp = MagicMock(ok=True, text="     RHOF00ISL Site Information Form\n")
        with patch(
            "receivers.dissemination.m3g_client.requests.get", return_value=resp
        ) as g:
            c.view_sitelog(marker)
        assert g.call_args[1]["params"] == {"station": "RHOF00ISL"}
