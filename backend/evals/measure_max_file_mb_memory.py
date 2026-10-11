"""Measure peak RSS for a ~250 MB PDF through local + Files-API-shaped paths.

Run:  python evals/measure_max_file_mb_memory.py
Raises MAX_FILE_MB to 250 only when both peaks stay under ~1.5 GB.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# Allow running from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET_MB = 250
SAFE_PEAK_MB = 1500


def _rss_mb() -> float:
    # Prefer psutil; Windows ctypes peak working set; Unix resource as last resort.
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return float(counters.WorkingSetSize) / (1024 * 1024)
    except Exception:
        pass
    try:
        import resource  # Unix only

        # Linux reports KB; macOS reports bytes.
        val = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return val / 1024.0 if val > 10_000_000 else val
    except Exception:
        return -1.0


def _make_250mb_pdf(path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    pages = 40
    per = int((TARGET_MB * 1024 * 1024) / pages)
    blob = (b"SCANNED-" * (per // 8 + 1))[:per]
    for i in range(pages):
        writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(blob)
        ref = writer._add_object(stream)
        writer.pages[i][NameObject("/CSPrivate")] = ref
    with open(path, "wb") as handle:
        writer.write(handle)


def _peak_during(fn) -> tuple[float, float]:
    baseline = _rss_mb()
    peak = baseline
    done = {"v": False}

    def _sample():
        nonlocal peak
        while not done["v"]:
            peak = max(peak, _rss_mb())
            time.sleep(0.05)

    import threading

    t = threading.Thread(target=_sample, daemon=True)
    t.start()
    try:
        fn()
    finally:
        done["v"] = True
        t.join(timeout=2)
        peak = max(peak, _rss_mb())
    return baseline, peak


def main() -> int:
    from pypdf import PdfReader

    from app import gemini_files
    from app.pdf_chunks import split_pdf

    tmp = Path(tempfile.mkdtemp(prefix="cs-mem-250-"))
    pdf = tmp / "scan250.pdf"
    print(f"building ~{TARGET_MB} MB PDF at {pdf} …")
    _make_250mb_pdf(pdf)
    size_mb = pdf.stat().st_size / (1024 * 1024)
    print(f"built size_mb={size_mb:.1f}")

    # Local-worker-shaped work: open + page count + chunk split (pypdf copy).
    def local_work():
        n = len(PdfReader(str(pdf), strict=False).pages)
        chunks = split_pdf(pdf, dest_dir=tmp / "chunks", max_mb=40, max_pages=300)
        assert n >= 1 and chunks
        # Touch each chunk once (as a local worker would before handoff).
        for c in chunks:
            _ = c.path.stat().st_size

    # Web / Files-API-shaped work: stream upload body via mocked connection.
    class _Resp:
        status = 200

        def read(self):
            return b'{"file":{"name":"files/mem","uri":"https://example.test/files/mem","state":"ACTIVE"}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, *a, **k):
            self._buf = bytearray()

        def putrequest(self, *a, **k):
            pass

        def putheader(self, *a, **k):
            pass

        def endheaders(self):
            pass

        def send(self, block):
            # Discard after counting — do not retain the 250 MB body.
            self._buf.append(block[0] if block else 0)

        def getresponse(self):
            return _Resp()

        def close(self):
            pass

    class _StartResp:
        headers = {"X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload/v1beta/upload-token"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def web_work():
        import http.client
        import urllib.request

        urllib.request.urlopen = lambda *a, **k: _StartResp()  # type: ignore
        http.client.HTTPSConnection = _Conn  # type: ignore
        from app.config import settings

        settings.gemini_api_key = "mem-test"
        gemini_files.upload_file_streaming(pdf, mime_type="application/pdf")

    b1, p1 = _peak_during(local_work)
    b2, p2 = _peak_during(web_work)
    print(f"local_worker baseline_mb={b1:.1f} peak_mb={p1:.1f}")
    print(f"web_process  baseline_mb={b2:.1f} peak_mb={p2:.1f}")
    ok = p1 <= SAFE_PEAK_MB and p2 <= SAFE_PEAK_MB and p1 > 0 and p2 > 0
    print(
        f"verdict={'RAISE_MAX_FILE_MB_250' if ok else 'KEEP_SAFE_CAP'} "
        f"safe_peak_mb={SAFE_PEAK_MB}"
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
