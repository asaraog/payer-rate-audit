#!/usr/bin/env python3
"""Download the current-year CMS PPRRVU file into data/.

The file is not committed. It ships with long CPT descriptors, which are
AMA-copyrighted; this repository carries code, not licensed content.

CMS publishes the relative value files as quarterly releases (RVU26A, RVU26B,
...) each with its own landing page linking one zip. This script discovers the
latest release for the configured year, downloads it, verifies the archive, and
extracts only the PPRRVU CSV. It is idempotent: an already-downloaded release is
left alone unless --force is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from payer_rate_audit.config import ConfigError, load_config  # noqa: E402
from payer_rate_audit.rvu import RVUFormatError, parse_pprrvu  # noqa: E402

CMS_BASE = "https://www.cms.gov"
INDEX_URL = f"{CMS_BASE}/medicare/payment/fee-schedules/physician/pfs-relative-value-files"
USER_AGENT = "payer-rate-audit/0.1 (+https://github.com/)"
MIN_ZIP_BYTES = 500_000


class FetchError(RuntimeError):
    """Raised when CMS pages or downloads do not look the way this script expects."""


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return response.read()


def discover_release(year: int, release: str | None = None) -> str:
    """Return the release slug (e.g. ``rvu26c``) to download for ``year``."""
    suffix = f"{year % 100:02d}"
    if release:
        slug = release.lower()
        if not slug.startswith("rvu"):
            slug = f"rvu{suffix}{slug}"
        return slug
    html = _get(INDEX_URL).decode("utf-8", errors="replace")
    slugs = sorted(set(re.findall(rf"/(rvu{suffix}[a-z0-9]+)\b", html, flags=re.IGNORECASE)))
    if not slugs:
        raise FetchError(
            f"No RVU{suffix} release links found on {INDEX_URL}. "
            "The CMS page layout may have changed; pass --release explicitly."
        )
    # Releases run A through D through the year; a revision of a release appends
    # "R" (rvu24ar) and supersedes the release it revises.
    return max(slugs, key=lambda slug: (slug[len(f"rvu{suffix}") :], len(slug)))


def discover_zip_url(slug: str) -> str:
    page = f"{INDEX_URL}/{slug}"
    html = _get(page).decode("utf-8", errors="replace")
    match = re.search(r'href="(/files/zip/[^"]*\.zip)"', html)
    if not match:
        raise FetchError(f"No .zip download link found on {page}.")
    return CMS_BASE + match.group(1)


def download(url: str, destination: Path, force: bool = False) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        print(f"[skip] {destination} already downloaded ({destination.stat().st_size:,} bytes)")
        return destination
    print(f"[get ] {url}")
    payload = _get(url)
    if len(payload) < MIN_ZIP_BYTES:
        raise FetchError(
            f"{url} returned only {len(payload):,} bytes; expected a multi-megabyte archive. "
            "This is usually an error page rather than the RVU file."
        )
    destination.write_bytes(payload)
    print(
        f"[ok  ] {destination} ({len(payload):,} bytes, sha256 "
        f"{hashlib.sha256(payload).hexdigest()[:16]}...)"
    )
    return destination


def extract_pprrvu(archive: Path, data_dir: Path, force: bool = False) -> Path:
    if not zipfile.is_zipfile(archive):
        raise FetchError(f"{archive} is not a valid zip archive; the download is corrupt.")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        # The non-QPP file lists every code on the fee schedule; the QPP file is a
        # subset limited to codes eligible for the APM-participant differential.
        candidates = [
            name
            for name in names
            if re.search(r"PPRRVU.*nonQPP.*\.csv$", name, flags=re.IGNORECASE)
        ] or [name for name in names if re.search(r"PPRRVU.*\.csv$", name, flags=re.IGNORECASE)]
        if not candidates:
            raise FetchError(f"{archive} contains no PPRRVU CSV. Archive members: {names}")
        member = sorted(candidates)[0]
        target = data_dir / Path(member).name
        if target.exists() and not force:
            print(f"[skip] {target} already extracted")
            return target
        target.write_bytes(bundle.read(member))
        print(f"[ok  ] extracted {member} -> {target}")
        return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.toml"))
    parser.add_argument("--year", type=int, help="Override [rvu].year from config.toml")
    parser.add_argument("--release", help="Force a specific release, e.g. rvu26c or c")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--force", action="store_true", help="Re-download and re-extract")
    args = parser.parse_args(argv)

    try:
        year = args.year or load_config(args.config).rvu_year
    except ConfigError as error:
        if not args.year:
            print(f"error: {error}", file=sys.stderr)
            return 2
        year = args.year

    data_dir = Path(args.data_dir)
    try:
        slug = discover_release(year, args.release)
        print(f"[find] {year} release: {slug}")
        url = discover_zip_url(slug)
        archive = download(url, data_dir / f"{slug}.zip", force=args.force)
        csv_path = extract_pprrvu(archive, data_dir, force=args.force)
        table = parse_pprrvu(csv_path, year=year)
    except (FetchError, RVUFormatError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    payable = table.frame["status_code"].isin({"A", "R", "T"}).sum()
    print(
        f"[ok  ] parsed {table.row_count:,} PPRRVU rows from {csv_path.name} "
        f"({payable:,} with status code A/R/T)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
