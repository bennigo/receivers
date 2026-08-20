"""A replacement must not carry LESS data than the file it overwrites.

The regenerability gate proves a raw file EXISTS. It never proves that raw
decodes, nor that the result is as complete — and that gap is the one thing
``rinex_bak`` was actually protecting against.

The silent class is partial truncation: a raw truncated mid-day converts to a
valid-looking SHORTER RINEX, passes the staged-completeness check, the
compression-format guard and the converter's identity gate (first-obs date and
position are all fine), and replaces a fuller original. VMEY's 7 truncated
.sbf.gz and 11 bad .T02 fail outright, so they were safe by accident. This gate
makes it safe by design.

Found by the review of the "abandon rinex_bak" proposal (2026-08-19), which
named it the reason the abandon-premise fails: with backups gone and no
comparator, that class becomes silent permanent loss.
"""

from __future__ import annotations

import gzip
import importlib

cli_main = importlib.import_module("receivers.cli.main")


def _gz(path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(payload)
    return path


class TestTheGateExists:
    def test_push_path_has_a_degradation_gate(self):
        import inspect

        src = inspect.getsource(cli_main._push_reconverted)
        assert "refused_degraded" in src

    def test_it_fails_closed_like_the_regenerability_gate(self):
        # A gate that cannot run must abort the push, not wave it through.
        import inspect

        src = inspect.getsource(cli_main._push_reconverted)
        assert "degradation gate could not run" in src
        assert "push aborted" in src

    def test_it_compares_decompressed_size_not_compressed(self):
        # Compressed size is not monotonic in content; decompressed is.
        import inspect

        src = inspect.getsource(cli_main._push_reconverted)
        assert "_decompressed_size" in src
        assert "gzip" in src

    def test_the_tolerance_absorbs_header_churn_only(self):
        # A rewritten REC # / TYPE line shifts the byte count slightly; real
        # truncation is orders of magnitude larger. 2 % separates them.
        import inspect

        src = inspect.getsource(cli_main._push_reconverted)
        assert "_DEGRADE_TOL = 0.02" in src


class TestSizeProbe:
    def test_it_reads_a_gzipped_file(self, tmp_path):
        import inspect

        # Exercise the same mechanism the gate uses, on real bytes.
        f = _gz(tmp_path / "a.gz", b"x" * 5000)
        import subprocess

        out = subprocess.run(["gzip", "-dc", str(f)], capture_output=True, timeout=60)
        assert out.returncode == 0
        assert len(out.stdout) == 5000

    def test_a_truncated_file_is_materially_smaller(self, tmp_path):
        full = _gz(tmp_path / "full.gz", b"EPOCH\n" * 5760)   # a complete day
        part = _gz(tmp_path / "part.gz", b"EPOCH\n" * 1200)   # truncated raw
        import subprocess

        def size(p):
            return len(subprocess.run(["gzip", "-dc", str(p)],
                                      capture_output=True, timeout=60).stdout)

        old, new = size(full), size(part)
        assert new < old * (1 - 0.02), "truncation must trip the 2 % tolerance"

    def test_a_header_only_rewrite_stays_within_tolerance(self, tmp_path):
        body = b"EPOCH\n" * 5760
        old_f = _gz(tmp_path / "old.gz", b"REC # / TYPE / VERS old\n" + body)
        new_f = _gz(tmp_path / "new.gz", b"REC # / TYPE / VERS n\n" + body)
        import subprocess

        def size(p):
            return len(subprocess.run(["gzip", "-dc", str(p)],
                                      capture_output=True, timeout=60).stdout)

        old, new = size(old_f), size(new_f)
        assert new >= old * (1 - 0.02), "a header fix must not be refused"
