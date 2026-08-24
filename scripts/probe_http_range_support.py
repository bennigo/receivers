#!/usr/bin/env python3
"""Probe whether a Trimble NetR9's HTTP API honours Range requests.

Answers whether resume-on-interrupt is implementable for a given receiver.
It talks to the LIVE receiver over the station network, so it is a manual
diagnostic, not a test — it lived in ``tests/`` as
``test_http_range_support.py`` until 2026-08-24, where pytest collected
``test_range_support`` as a test case, could not supply its ``station_id``
argument as a fixture, and errored on every run.

Usage:
    python scripts/probe_http_range_support.py STATION_ID

Example:
    python scripts/probe_http_range_support.py MANA
"""

import sys
from pathlib import Path

import requests

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from receivers.config_utils import get_station_config


def probe_range_support(station_id: str):
    """Test if a NetR9 receiver supports HTTP range requests.

    Args:
        station_id: Station identifier (e.g., 'MANA')
    """
    print(f"Testing HTTP Range support for station: {station_id}")
    print("=" * 60)

    # Load station configuration
    try:
        station_config = get_station_config(station_id)
    except Exception as e:
        print(f"❌ Failed to load station config: {e}")
        return False

    # Extract connection details
    router_ip = station_config["router"]["ip"]
    http_port = station_config["receiver"].get("httpport", 8060)
    base_url = f"http://{router_ip}:{http_port}"

    print(f"Connection: {base_url}")
    print()

    # Test 1: Use a known file from recent download
    print("Step 1: Using known test file...")
    # From your recent download log: MANA202509300900b.T02 in /Internal/202509/1Hz_1hr
    test_file = f"{station_id}202509300900b.T02"
    test_path = f"/download/Internal/202509/1Hz_1hr/{test_file}"

    print(f"✅ Using test file: {test_file}")
    print(f"   Path: {test_path}")
    print()

    # Test 2: Get file size with HEAD request
    print("Step 2: Getting file size...")
    try:
        file_url = f"{base_url}{test_path}"
        print(f"   URL: {file_url}")

        # Try HEAD first
        try:
            head_response = requests.head(file_url, timeout=60)
            print(f"   HEAD response status: {head_response.status_code}")
            content_length = head_response.headers.get("Content-Length")
        except Exception as e:
            print(f"   HEAD request failed: {e}")
            head_response = None
            content_length = None

        if not content_length or (head_response and head_response.status_code != 200):
            print("   Trying GET request with streaming...")
            # Some servers don't support HEAD, try GET
            get_response = requests.get(file_url, timeout=60, stream=True)
            print(f"   GET response status: {get_response.status_code}")
            content_length = get_response.headers.get("Content-Length")
            print(f"   Headers: {dict(get_response.headers)}")
            get_response.close()

        if not content_length:
            print("❌ Could not determine file size")
            return False

        file_size = int(content_length)
        print(f"✅ File size: {file_size:,} bytes")
        print()

    except Exception as e:
        print(f"❌ Failed to get file size: {e}")
        import traceback

        traceback.print_exc()
        return False

    # Test 3: Test Range request (request middle 1024 bytes)
    print("Step 3: Testing Range request...")
    try:
        # Request bytes from middle of file
        range_start = min(1024, file_size // 2)
        range_end = min(range_start + 1024, file_size - 1)

        headers = {"Range": f"bytes={range_start}-{range_end}"}

        print(f"   Requesting bytes {range_start}-{range_end}")
        range_response = requests.get(file_url, headers=headers, timeout=30)

        print(f"   Response status: {range_response.status_code}")
        print("   Response headers:")
        for key, value in range_response.headers.items():
            if key.lower() in ["content-length", "content-range", "accept-ranges"]:
                print(f"      {key}: {value}")

        # Check if range request was honored
        if range_response.status_code == 206:
            print(
                "✅ SUCCESS: Server supports range requests (HTTP 206 Partial Content)"
            )
            content_range = range_response.headers.get("Content-Range")
            if content_range:
                print(f"   Content-Range: {content_range}")
            return True
        elif range_response.status_code == 200:
            # Server ignored range request and sent full file
            received_size = len(range_response.content)
            expected_size = range_end - range_start + 1

            if received_size == file_size:
                print("❌ Server does NOT support range requests")
                print(
                    "   (Returned full file with status 200 instead of partial content)"
                )
                return False
            elif received_size == expected_size:
                print(
                    "⚠️  Server sent partial content but with status 200 (non-standard)"
                )
                print("   This might work but is not standard HTTP behavior")
                return "partial"
            else:
                print(f"❌ Unexpected response size: {received_size} bytes")
                return False
        else:
            print(f"❌ Unexpected status code: {range_response.status_code}")
            return False

    except Exception as e:
        print(f"❌ Failed to test range request: {e}")
        return False


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/probe_http_range_support.py STATION_ID")
        print("Example: python scripts/probe_http_range_support.py MANA")
        sys.exit(1)

    station_id = sys.argv[1].upper()

    result = probe_range_support(station_id)

    print()
    print("=" * 60)
    if result is True:
        print("✅ RESULT: HTTP Range requests ARE SUPPORTED")
        print("   Resume download functionality can be implemented!")
    elif result == "partial":
        print("⚠️  RESULT: Partial range support (non-standard)")
        print("   Resume might work but needs careful testing")
    else:
        print("❌ RESULT: HTTP Range requests NOT SUPPORTED")
        print("   Will need to rely on better timeouts and retry logic")
    print("=" * 60)


if __name__ == "__main__":
    main()
