"""
src/ingestion/alt_data/base_scraper.py — Resilient AltData Ingestion Scaffolding
================================================================================

Abstract base class (`BaseAltDataScraper`) for Indian public-portal alternative data
scrapers feeding the GraphRAG and Macro Lake engines.

Capabilities:
  1. HTTP Session Management: persistent `CookieJar` opener with TLS context and
     injectable `fetch_fn` for 100% hermetic offline unit testing.
  2. Rotating Browser Headers: realistic desktop User-Agent rotation + standard
     Accept / Accept-Language / Connection headers for government portals.
  3. Exponential Backoff with Jitter: automatic retries on transient network drops,
     timeouts, and HTTP 429/5xx responses common on NIC/MoRTH/GeM portals.
  4. Idempotent Atomic CSV Persistence: writes to `data/lake/alt_data/<dataset>.csv`
     via temp-file + `os.replace`, deduplicating on configurable primary-key columns
     so re-runs never duplicate rows or leave half-written files.
  5. Fail-Open Execution (`run`): portals that drop connections or return malformed
     markup log a structured warning (and append to `logs/alt_data.jsonl`) and exit
     cleanly with `status="degraded"` — never crashing the cron job.
"""
from __future__ import annotations

import abc
import csv
import http.cookiejar
import io
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
ALT_DATA_LAKE = ROOT / "data" / "lake" / "alt_data"
OUTAGE_LOG = ROOT / "logs" / "alt_data.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
)

RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def now_ist_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


class BaseAltDataScraper(abc.ABC):
    """Abstract base scraper for Indian alternative data portals (Vahan, GeM, etc.)."""

    dataset_name: str = "alt_data"
    default_columns: tuple[str, ...] = ()
    primary_key_columns: tuple[str, ...] = ()

    def __init__(
        self,
        lake_dir: Path | str | None = None,
        log_path: Path | str | None = None,
        timeout: float = 25.0,
        max_retries: int = 3,
        base_backoff: float = 1.5,
        max_backoff: float = 20.0,
        jitter: float = 0.25,
        fetch_fn: Callable[..., bytes | str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.lake_dir = Path(lake_dir) if lake_dir is not None else ALT_DATA_LAKE
        self.log_path = Path(log_path) if log_path is not None else OUTAGE_LOG
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.base_backoff = float(base_backoff)
        self.max_backoff = float(max_backoff)
        self.jitter = float(jitter)
        self._fetch_fn = fetch_fn
        self._sleep_fn = sleep_fn or time.sleep
        self._rng = rng or random.Random()
        self._ua_index = 0
        self.cookie_jar = http.cookiejar.CookieJar()
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        cookie_handler = urllib.request.HTTPCookieProcessor(self.cookie_jar)
        return urllib.request.build_opener(https_handler, cookie_handler)

    def rotating_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return standard browser headers with a rotated User-Agent."""
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        headers = {
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        if extra:
            headers.update(extra)
        return headers

    def compute_backoff(self, attempt: int) -> float:
        """Exponential backoff `base * 2^(attempt-1)` capped at `max_backoff` + uniform jitter."""
        exp = min(self.max_backoff, self.base_backoff * (2 ** max(0, attempt - 1)))
        if self.jitter > 0:
            exp += self._rng.uniform(0.0, self.jitter * exp)
        return round(min(self.max_backoff, exp), 4)

    def log_warning(self, code: str, detail: str) -> None:
        """Log a non-fatal portal warning to standard logging and `logs/alt_data.jsonl`."""
        msg = f"[{self.dataset_name}] {code}: {detail}"
        logger.warning(msg)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": now_ist_iso(),
                            "dataset": self.dataset_name,
                            "code": code,
                            "detail": str(detail)[:300],
                        }
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _raw_http_call(
        self,
        url: str,
        method: str = "GET",
        data: bytes | dict[str, Any] | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        req_headers = self.rotating_headers(headers)
        if self._fetch_fn is not None:
            try:
                res = self._fetch_fn(url, method=method, data=data, headers=req_headers)
            except TypeError:
                res = self._fetch_fn(url)
            return res.encode("utf-8") if isinstance(res, str) else bytes(res)

        payload: bytes | None = None
        if isinstance(data, dict):
            payload = urllib.parse.urlencode(data).encode("utf-8")
            req_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
            )
        elif isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            payload = bytes(data)

        req = urllib.request.Request(
            url, data=payload, headers=req_headers, method=method.upper()
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | dict[str, Any] | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Execute an HTTP request with rotating headers and exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._raw_http_call(url, method=method, data=data, headers=headers)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code not in RETRYABLE_HTTP_CODES or attempt >= self.max_retries:
                    break
                delay = self.compute_backoff(attempt)
                self.log_warning(
                    f"HTTP-{exc.code}",
                    f"attempt {attempt}/{self.max_retries} on {url} failed ({exc}); retrying in {delay:.2f}s",
                )
                self._sleep_fn(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = self.compute_backoff(attempt)
                self.log_warning(
                    "NET-DROP",
                    f"attempt {attempt}/{self.max_retries} on {url} dropped ({type(exc).__name__}: {exc}); "
                    f"retrying in {delay:.2f}s",
                )
                self._sleep_fn(delay)
        assert last_exc is not None
        raise last_exc

    @property
    def csv_path(self) -> Path:
        return self.lake_dir / f"{self.dataset_name}.csv"

    def read_existing_rows(self, path: Path | None = None) -> list[dict[str, str]]:
        target = path or self.csv_path
        if not target.is_file():
            return []
        try:
            text = target.read_text(encoding="utf-8")
            return list(csv.DictReader(io.StringIO(text)))
        except OSError:
            return []

    def _row_key(self, row: dict[str, Any], pk_cols: Sequence[str]) -> tuple[str, ...]:
        return tuple(str(row.get(col, "")).strip() for col in pk_cols)

    def write_csv_idempotent(
        self,
        rows: Iterable[dict[str, Any]],
        columns: Sequence[str] | None = None,
        pk_columns: Sequence[str] | None = None,
        path: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Idempotently merge `rows` into `data/lake/alt_data/<dataset>.csv` via temp-file + rename.

        Rows are keyed by `pk_columns` (default `self.primary_key_columns` or all `columns`).
        Existing keys are preserved without duplication; new keys are appended in deterministic
        sorted order.
        """
        cols = tuple(columns or self.default_columns)
        if not cols:
            raise ValueError(f"{self.dataset_name}: columns must be specified for CSV write")
        pks = tuple(pk_columns or self.primary_key_columns or cols)
        target = path or self.csv_path

        existing = self.read_existing_rows(target)
        by_key: dict[tuple[str, ...], dict[str, Any]] = {}
        for r in existing:
            k = self._row_key(r, pks)
            if any(k):
                by_key[k] = {c: r.get(c, "") for c in cols}

        before_count = len(by_key)
        added = 0
        updated = 0
        for r in rows or []:
            normalized = {c: ("" if r.get(c) is None else str(r.get(c)).strip()) for c in cols}
            k = self._row_key(normalized, pks)
            if not any(k):
                continue
            if k not in by_key:
                by_key[k] = normalized
                added += 1
            elif by_key[k] != normalized:
                by_key[k] = normalized
                updated += 1

        merged = [by_key[k] for k in sorted(by_key.keys())]
        if not dry_run and (added > 0 or updated > 0 or not target.exists()):
            target.parent.mkdir(parents=True, exist_ok=True)
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(cols), lineterminator="\n")
            writer.writeheader()
            writer.writerows(merged)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(buf.getvalue(), encoding="utf-8")
            tmp.replace(target)

        return {
            "dataset": self.dataset_name,
            "path": str(target),
            "rows_before": before_count,
            "rows_added": added,
            "rows_updated": updated,
            "rows_total": len(merged),
            "dry_run": bool(dry_run),
        }

    @abc.abstractmethod
    def fetch_raw(self, **kwargs: Any) -> Any:
        """Fetch raw HTML/JSON payload(s) from the target portal."""

    @abc.abstractmethod
    def parse(self, raw: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Parse raw portal payload(s) into normalized row dicts."""

    def run(self, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Fail-open pipeline entry point: fetch -> parse -> idempotent CSV write.
        Never raises on network/portal failure; logs a warning and exits cleanly."""
        try:
            raw = self.fetch_raw(**kwargs)
            rows = self.parse(raw, **kwargs)
            if not rows:
                self.log_warning("EMPTY-PARSE", "portal returned zero extractable records")
                return {
                    "ok": False,
                    "status": "degraded",
                    "dataset": self.dataset_name,
                    "reason": "empty_parse",
                    "rows_added": 0,
                    "rows_total": len(self.read_existing_rows()),
                    "dry_run": bool(dry_run),
                    "as_of": now_ist_iso(),
                }
            write_summary = self.write_csv_idempotent(rows, dry_run=dry_run)
            return {
                "ok": True,
                "status": "ok",
                "dataset": self.dataset_name,
                "rows_parsed": len(rows),
                **write_summary,
                "as_of": now_ist_iso(),
            }
        except Exception as exc:
            self.log_warning("PORTAL-DOWN", f"{type(exc).__name__}: {exc}")
            return {
                "ok": False,
                "status": "degraded",
                "dataset": self.dataset_name,
                "reason": f"{type(exc).__name__}: {exc}",
                "rows_added": 0,
                "rows_total": len(self.read_existing_rows()),
                "dry_run": bool(dry_run),
                "as_of": now_ist_iso(),
            }
