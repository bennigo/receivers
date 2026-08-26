#!/usr/bin/env python3
"""
Isolated test script for NATT receiver authentication.

Tests HTTP Basic Auth with Trimble NetR9-style receivers using downgraded firmware.
This script validates the authentication approach before integrating into main codebase.

Usage:
    # Edit the configuration section below with NATT credentials
    python3 /tmp/test_natt_download.py

Requirements:
    - PYTHONPATH must include receivers, gps_parser, gtimes packages
    - Test station must be configured in stations.cfg
"""

import os
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add receivers package to path
sys.path.insert(0, "/home/bgo/work/projects/gpslibrary/receivers/src")
sys.path.insert(0, "/home/bgo/work/projects/gpslibrary/gps_parser/src")
sys.path.insert(0, "/home/bgo/work/projects/gpslibrary/gtimes/src")

# ============================================================================
# CONFIGURATION - EDIT THESE VALUES
# ============================================================================

# NATT Station to test (choose one configured in stations.cfg)
TEST_STATION = "ISAF"  # Ísafjörður

# Credentials (these will be added to stations.cfg later)
TEST_USERNAME = os.environ.get("NATT_TEST_USERNAME", "")
TEST_PASSWORD = os.environ.get("NATT_TEST_PASSWORD", "")

# Connection details
TEST_IP = "193.109.17.51"
TEST_PORT = 80  # Note: NATT uses port 80, not 8060!

# Test parameters
TEST_SESSION = "15s_24hr"  # Testing 15s_24hr session (file just started today)
DAYS_BACK = 0  # Download today's file (just started)

# ============================================================================
# END CONFIGURATION
# ============================================================================

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def test_authentication():
    """Test 1: Validate HTTP Basic Auth works with NATT receiver."""
    logger.info("=" * 70)
    logger.info("TEST 1: Authentication Test")
    logger.info("=" * 70)

    try:
        from receivers.trimble.http_client import TrimbleHTTPClient

        # Build station config with auth credentials
        station_config = {
            "router": {"ip": TEST_IP, "ftp_mode": "active"},
            "receiver": {
                "httpport": TEST_PORT,  # NATT uses port 80
                "user": TEST_USERNAME,  # Auth credentials
                "pwd": TEST_PASSWORD,
                "timeout_category": "mobile",
            },
        }

        logger.info(f"Testing connection to {TEST_STATION}")
        logger.info(f"IP: {station_config['router']['ip']}")
        logger.info(f"Port: {station_config['receiver']['httpport']}")
        logger.info(f"Username: {TEST_USERNAME}")
        logger.info(f"Password: {'*' * len(TEST_PASSWORD)}")

        # Create HTTP client with auth
        client = TrimbleHTTPClient(TEST_STATION, station_config)

        # Test connection
        result = client.test_connection()

        if result["success"]:
            logger.info("✅ Authentication SUCCESS!")
            logger.info(f"   Connection time: {result['duration']:.2f}s")
            logger.info(f"   Response size: {result['response_size']} bytes")
            return True
        else:
            logger.error("❌ Authentication FAILED!")
            logger.error(f"   Error: {result['error']}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_directory_listing():
    """Test 2: Verify we can list files with authentication."""
    logger.info("\n" + "=" * 70)
    logger.info("TEST 2: Directory Listing Test")
    logger.info("=" * 70)

    try:
        from receivers.trimble.http_download_client import NetR9HTTPDownloader

        station_config = {
            "router": {"ip": TEST_IP, "ftp_mode": "active"},
            "receiver": {
                "httpport": TEST_PORT,
                "user": TEST_USERNAME,
                "pwd": TEST_PASSWORD,
                "timeout_category": "mobile",
            },
        }

        downloader = NetR9HTTPDownloader(TEST_STATION, station_config)

        # Determine remote path based on session type
        today = datetime.now()
        year_month = today.strftime("%Y%m")

        if TEST_SESSION == "15s_24hr":
            remote_path = f"/Internal/{year_month}/15s_24hr"
        elif TEST_SESSION == "1Hz_1hr":
            remote_path = f"/Internal/{year_month}/1Hz_1hr"
        else:
            remote_path = f"/Internal/{year_month}/{TEST_SESSION}"

        logger.info(f"Listing directory: {remote_path}")

        files = downloader.get_directory_listing(remote_path)

        if files:
            logger.info("✅ Directory listing SUCCESS!")
            logger.info(f"   Found {len(files)} files")

            # Check for firmware bug with underscores (e.g., ISAF______... instead of ISAF...)
            underscore_files = [f for f in files if "______" in f[0]]
            if underscore_files:
                logger.warning(
                    f"⚠️  Detected firmware bug: {len(underscore_files)} files with underscore padding"
                )
                logger.warning(f"   Example: {underscore_files[0][0]}")
                logger.info(
                    "   This is expected for NATT receivers with downgraded firmware"
                )

            logger.info("   Sample files:")
            for filename, size in files[:5]:
                logger.info(f"     - {filename} ({size:,} bytes)")
            return True
        else:
            logger.warning(f"⚠️  No files found in {remote_path}")
            logger.info("   This might be expected if no data exists for this session")
            return True  # Not a failure, just empty directory

    except Exception as e:
        logger.error(f"❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_file_download():
    """Test 3: Download a real file with authentication."""
    logger.info("\n" + "=" * 70)
    logger.info("TEST 3: File Download Test")
    logger.info("=" * 70)

    try:
        from receivers.trimble.http_download_client import NetR9HTTPDownloader

        station_config = {
            "router": {"ip": TEST_IP, "ftp_mode": "active"},
            "receiver": {
                "httpport": TEST_PORT,
                "user": TEST_USERNAME,
                "pwd": TEST_PASSWORD,
                "timeout_category": "mobile",
            },
        }

        downloader = NetR9HTTPDownloader(TEST_STATION, station_config)

        # Calculate target date (today or specified days back)
        target_date = datetime.now() - timedelta(days=DAYS_BACK)
        year_month = target_date.strftime("%Y%m")

        # Build remote path and find available files
        if TEST_SESSION == "15s_24hr":
            remote_path = f"/Internal/{year_month}/15s_24hr"
        elif TEST_SESSION == "1Hz_1hr":
            remote_path = f"/Internal/{year_month}/1Hz_1hr"
        else:
            remote_path = f"/Internal/{year_month}/{TEST_SESSION}"

        logger.info(f"Looking for files in: {remote_path}")

        # Get directory listing
        files = downloader.get_directory_listing(remote_path)

        if not files:
            logger.error("❌ No files found for download test")
            return False

        # Pick the first file
        filename, expected_size = files[0]
        logger.info(f"Downloading test file: {filename} ({expected_size:,} bytes)")

        # Download to /tmp
        tmp_dir = Path("/tmp/natt_test")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        local_path = tmp_dir / filename

        # Attempt download
        success = downloader.download_file(
            remote_path, filename, local_path, expected_size
        )

        if success and local_path.exists():
            actual_size = local_path.stat().st_size
            logger.info("✅ Download SUCCESS!")
            logger.info(f"   File: {local_path}")
            logger.info(f"   Size: {actual_size:,} bytes")
            logger.info(f"   Expected: {expected_size:,} bytes")
            logger.info(
                f"   Match: {'✅ YES' if actual_size == expected_size else '❌ NO'}"
            )
            return actual_size == expected_size
        else:
            logger.error("❌ Download FAILED")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        return False


def get_station_ip(station_id: str) -> str:
    """Get station IP from gps_parser configuration."""
    try:
        import gps_parser

        parser = gps_parser.ConfigParser()
        station_info = parser.getStationInfo(station_id)
        return station_info["station"]["router_ip"]
    except Exception as e:
        logger.error(f"Failed to get IP for {station_id}: {e}")
        logger.error("Make sure station is configured in stations.cfg")
        raise


def main():
    """Run all NATT authentication tests."""
    logger.info("🔬 NATT Receiver Authentication Test Suite")
    logger.info(f"Station: {TEST_STATION}")
    logger.info(f"Session: {TEST_SESSION}")
    logger.info(f"Days back: {DAYS_BACK}")
    logger.info("")

    # Configuration is now set for ISAF - ready to test!
    logger.info("📝 Configuration:")
    logger.info(f"   IP: {TEST_IP}")
    logger.info(f"   Port: {TEST_PORT}")
    logger.info(f"   User: {TEST_USERNAME}")
    logger.info("")

    results = {}

    # Run tests
    results["auth"] = test_authentication()
    if results["auth"]:
        results["listing"] = test_directory_listing()
        if results["listing"]:
            results["download"] = test_file_download()

    # Summary
    logger.info("\n" + "=" * 70)
    logger.info("TEST SUMMARY")
    logger.info("=" * 70)
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"{test_name.capitalize()}: {status}")

    all_passed = all(results.values())
    if all_passed:
        logger.info("\n🎉 All tests PASSED! NATT authentication is working!")
        logger.info("\nNext steps:")
        logger.info("1. Add receiver_user and receiver_password to stations.cfg")
        logger.info("2. Set receiver_type = netr9 for NATT stations")
        logger.info("3. Test manual download: receivers download {TEST_STATION}")
        return 0
    else:
        logger.error("\n❌ Some tests FAILED. Check errors above.")
        return 1


if __name__ == "__main__":
    if not TEST_USERNAME or not TEST_PASSWORD:
        sys.exit(
            "NATT credentials are not set.\n"
            "These used to be hardcoded here and were committed to a PUBLIC repo;\n"
            "they have been rotated and moved to the environment. Run as:\n"
            "  NATT_TEST_USERNAME=... NATT_TEST_PASSWORD=... "
            "python tests/manual/test_natt_auth.py"
        )
    sys.exit(main())
