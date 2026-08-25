"""
dip_client.py
--------------
Small, dependency-light client for the German Bundestag's DIP API
(Dokumentations- und Informationssystem für Parlamentsmaterialien).

1. Reusable    -> a single `DipClient.get_resource()` works for every
                  DIP resource type (vorgang, drucksache, aktivitaet, ...),
                  so adding a new resource later is a one-line call, not
                  new code.
2. Transparent -> every request is logged (params, status, item count,
                  duration) via the standard `logging` module, and every
                  raw response is written to a `fetch_log` table in
                  SQLite before we touch the payload. If something looks
                  wrong downstream, we can always trace it back to the
                  exact request that produced it.
3. Robust      -> automatic retries with backoff on transient network /
                  5xx errors, explicit (non-retried) handling of 4xx
                  errors (bad params, bad key) so mistakes fail loudly
                  instead of silently retrying forever.
4. Polite      -> DIP is a public-sector API with unpublished rate
                  limits; we page in reasonably sized batches and don't
                  hammer it in a tight loop.

NOTE ON FIELD NAMES: this module treats the JSON payload as opaque
(`dict`) and does not interpret resource-specific fields itself — that
happens downstream in dbt staging models. The `vorgang` schema used there
(id, typ, beratungsstand, vorgangstyp, wahlperiode, initiative, datum,
aktualisiert, titel, abstract, sachgebiet, deskriptor, gesta, ...) is taken
from the live Swagger UI at https://search.dip.bundestag.de/api/v1/
swagger-ui/. Note there is no page-size parameter (e.g. "rows") documented
for /vorgang -- the API returns a fixed page and pagination is entirely
cursor-driven, so `page_size` below is unused / not sent as a request param.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("dip_client")

BASE_URL = "https://search.dip.bundestag.de/api/v1"

# Resources documented at https://github.com/bundesAPI/dip-bundestag-api
VALID_RESOURCES = {
    "aktivitaet",
    "drucksache",
    "drucksache-text",
    "person",
    "plenarprotokoll",
    "plenarprotokoll-text",
    "vorgang",
    "vorgangsposition",
}


class DipApiError(Exception):
    """
    Base class for all DIP API errors. Carries the request context
    (resource, params, request_id) so a catch-all handler can still log
    something traceable even for error types it doesn't specifically know
    about.
    """

    def __init__(self, message: str, *, resource: str, params: dict, request_id: str,
                 status_code: int | None = None):
        super().__init__(message)
        self.resource = resource
        self.params = params
        self.request_id = request_id
        self.status_code = status_code

    def __str__(self) -> str:
        return (
            f"[{self.request_id}] {self.resource} status={self.status_code} "
            f"params={self.params}: {super().__str__()}"
        )


class DipAuthError(DipApiError):
    """401 - API key missing, invalid, or expired. Not retryable; needs a human."""


class DipBadRequestError(DipApiError):
    """400 - malformed filter/param (e.g. bad cursor, unknown field). Not retryable; a code bug."""


class DipNotFoundError(DipApiError):
    """404 - requested single-entity id does not exist. Not retryable, but not necessarily an error either."""


class DipTransientError(DipApiError):
    """
    429 (rate limited) or 5xx (server error) after retries are exhausted.
    Retryable in principle -- if this surfaces, urllib3's Retry already
    tried and gave up, so the caller (e.g. an orchestrator) should back
    off further and try again later, not immediately.
    """


class DipTimeoutError(DipTransientError):
    """Request timed out (connect or read) after all retries. Treated as transient."""


class DipInvalidResponseError(DipApiError):
    """
    HTTP status was OK, but the response body was not valid JSON.
    Corresponds to the "Valid JSON?" check in the validation flow --
    this should never happen against the real API, so if it does, it's
    worth surfacing loudly (e.g. an HTML error/maintenance page returned
    with a 200, or a proxy/CDN issue).
    """


class DipValidationError(DipApiError):
    """
    Response was valid JSON but didn't have the shape we expect (e.g.
    missing "documents"/"numFound" for a list endpoint). Corresponds to
    the "Expected fields present?" check -- this signals the API's
    response schema changed, or we're calling the wrong resource.
    """


@dataclass
class DipClient:
    api_key: str
    base_url: str = BASE_URL
    timeout: int = 30
    max_retries: int = 4
    backoff_factor: float = 1.5
    session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api_key is required (see .env.example)")

        self.session = requests.Session()
        retry = Retry(
            total=self.max_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "Authorization": f"ApiKey {self.api_key}",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------
    def _request(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Make one GET request and run it through the full validation chain:

            HTTP status OK?      -> no: raise typed error, logged
            Valid JSON?          -> no: log raw response body, raise DipInvalidResponseError
            Expected fields present? -> no: raise DipValidationError
            -> return the validated payload dict

        Every call gets a short request_id so a single log line (and a
        single fetch_log row) can be traced to exactly one HTTP request,
        even when hundreds run per day.
        """
        request_id = uuid.uuid4().hex[:8]
        url = f"{self.base_url}/{resource}"
        safe_params = {k: v for k, v in params.items() if k != "apikey"}
        common_kwargs = dict(resource=resource, params=safe_params, request_id=request_id)

        # --- Make the request (handles network errors + timeouts) ------
        start = time.monotonic()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            duration_ms = round((time.monotonic() - start) * 1000)
            logger.error("[%s] GET %s -> TIMEOUT after %d ms (limit=%ss): %s",
                         request_id, resource, duration_ms, self.timeout, exc)
            raise DipTimeoutError(f"Timed out calling {resource}: {exc}", **common_kwargs) from exc
        except requests.exceptions.RequestException as exc:
            # DNS failure, connection refused, retries exhausted, etc.
            # The API never responded at all -- treated as transient.
            duration_ms = round((time.monotonic() - start) * 1000)
            logger.error("[%s] GET %s -> NETWORK ERROR after %d ms: %s",
                         request_id, resource, duration_ms, exc)
            raise DipTransientError(f"Network error calling {resource}: {exc}", **common_kwargs) from exc

        duration_ms = round((time.monotonic() - start) * 1000)
        logger.info("[%s] GET %s params=%s -> %s (%d ms)",
                     request_id, resource, safe_params, response.status_code, duration_ms)

        # --- Check 1: HTTP status OK? -----------------------------------
        if response.status_code != 200:
            try:
                detail = response.json().get("message", response.text[:300])
            except ValueError:
                detail = response.text[:300]
            status_kwargs = dict(**common_kwargs, status_code=response.status_code)

            if response.status_code == 401:
                logger.error("[%s] AUTH FAILURE (401): %s -- key missing/invalid/expired", request_id, detail)
                raise DipAuthError(detail, **status_kwargs)
            if response.status_code == 400:
                logger.error("[%s] BAD REQUEST (400): %s -- check filter params", request_id, detail)
                raise DipBadRequestError(detail, **status_kwargs)
            if response.status_code == 404:
                logger.warning("[%s] NOT FOUND (404): %s", request_id, detail)
                raise DipNotFoundError(detail, **status_kwargs)
            if response.status_code == 429:
                logger.error("[%s] RATE LIMITED (429) after retries exhausted: %s", request_id, detail)
                raise DipTransientError(detail, **status_kwargs)
            if response.status_code >= 500:
                logger.error("[%s] SERVER ERROR (%d) after retries exhausted: %s",
                             request_id, response.status_code, detail)
                raise DipTransientError(detail, **status_kwargs)
            logger.error("[%s] UNEXPECTED STATUS %d: %s", request_id, response.status_code, detail)
            raise DipApiError(detail, **status_kwargs)

        # --- Check 2: Valid JSON? ---------------------------------------
        try:
            payload = response.json()
        except ValueError as exc:
            # Log the raw body (truncated) so the actual bad response is
            # visible in logs, not just "it wasn't JSON".
            logger.error("[%s] INVALID JSON in 200 response. Raw body (first 500 chars): %r",
                         request_id, response.text[:500])
            raise DipInvalidResponseError(
                f"Response was not valid JSON: {exc}", **common_kwargs, status_code=200,
            ) from exc

        if not isinstance(payload, dict):
            logger.error("[%s] JSON parsed but was not an object (got %s)", request_id, type(payload).__name__)
            raise DipInvalidResponseError(
                f"Expected a JSON object, got {type(payload).__name__}",
                **common_kwargs, status_code=200,
            )

        # --- Check 3: Expected fields present? --------------------------
        # List endpoints (vorgang, aktivitaet, ...) are documented to
        # always return "documents" and "numFound". A single-entity
        # endpoint (e.g. /vorgang/{id}) instead returns the entity
        # directly and won't have these keys -- callers of get_resource()
        # always hit the list form, so we validate for that shape here.
        missing = [f for f in ("documents", "numFound") if f not in payload]
        if missing:
            logger.error("[%s] Response missing expected field(s) %s. Keys present: %s",
                         request_id, missing, list(payload.keys()))
            raise DipValidationError(
                f"Response missing expected field(s): {missing}",
                **common_kwargs, status_code=200,
            )

        # --- Empty result set is valid, just worth noting distinctly ----
        if payload["numFound"] == 0 or not payload["documents"]:
            logger.info("[%s] Zero results for this query (numFound=%s) -- not an error",
                        request_id, payload.get("numFound"))

        return payload

    # ------------------------------------------------------------------
    # Public: paginated resource fetch
    # ------------------------------------------------------------------
    def get_resource(
        self,
        resource: str,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Yield individual documents (dicts) from a DIP resource, transparently
        following cursor-based pagination until exhausted or `max_items`
        is reached.

        Parameters
        ----------
        resource : one of VALID_RESOURCES, e.g. "vorgang"
        params   : query filters, e.g. {"f.titel": "Rente", "f.wahlperiode": 21}
                   (see live Swagger docs for the exact filter names/types
                   available per resource)
        max_items: optional cap, useful for smoke-testing a query before
                   pulling everything
        """
        if resource not in VALID_RESOURCES:
            raise ValueError(f"Unknown resource '{resource}', expected one of {VALID_RESOURCES}")

        params = dict(params or {})
        params.setdefault("format", "json")
        # No page-size parameter is documented for this API (confirmed via
        # the live Swagger UI) -- page size is fixed server-side and we page
        # purely via the `cursor` returned in each response.

        fetched = 0
        cursor = None
        page = 0
        seen_cursors: set[str] = set()

        while True:
            page += 1
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor

            payload = self._request(resource, page_params)
            documents = payload["documents"]
            next_cursor = payload.get("cursor")

            logger.info(
                "resource=%s page=%d items=%d numFound=%s",
                resource, page, len(documents), payload.get("numFound"),
            )

            for doc in documents:
                yield doc
                fetched += 1
                if max_items is not None and fetched >= max_items:
                    return

            # --- Pagination-problem guards -------------------------------
            if not documents or not next_cursor:
                # Normal end of results.
                break
            if next_cursor == cursor:
                # API signals "no more pages" by repeating the same cursor
                # (documented behaviour) -- normal termination, not a bug.
                break
            if next_cursor in seen_cursors:
                # The cursor changed but we've seen this exact value before
                # -- a genuine pagination anomaly (e.g. server-side loop or
                # inconsistent state), not documented behaviour. Fail loudly
                # rather than spin forever.
                logger.error(
                    "resource=%s page=%d: cursor '%s' was already seen earlier in this "
                    "pagination sequence -- possible pagination loop, stopping.",
                    resource, page, next_cursor,
                )
                raise DipApiError(
                    "Pagination loop detected: repeated cursor value",
                    resource=resource, params={k: v for k, v in params.items() if k != "apikey"},
                    request_id="pagination-guard",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor


# ----------------------------------------------------------------------
# SQLite persistence helpers
# ----------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Create the raw-layer tables if they don't already exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fetch_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            resource        TEXT NOT NULL,
            params_json     TEXT NOT NULL,
            requested_at    TEXT NOT NULL,
            item_count      INTEGER,
            error           TEXT
        );

        CREATE TABLE IF NOT EXISTS raw_documents (
            resource        TEXT NOT NULL,
            dip_id          TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            payload_json    TEXT NOT NULL,
            PRIMARY KEY (resource, dip_id, fetched_at)
        );
        """
    )
    conn.commit()


def persist_documents(
    conn: sqlite3.Connection,
    resource: str,
    documents: list[dict[str, Any]],
) -> int:
    """
    Write raw documents to SQLite, one JSON blob per row, keyed by
    (resource, dip_id, fetched_at). We intentionally keep every fetch as a
    new row (not an upsert) so the raw layer is an append-only audit trail;
    dbt's staging layer is responsible for deduplicating to "latest known
    state" (see stg_dip__vorgaenge.sql).

    Returns the number of rows actually inserted (not the number attempted)
    -- duplicates within a single batch (e.g. the same document appearing
    on two pages, a real pagination edge case, not just a hypothetical)
    are silently skipped by INSERT OR IGNORE, and the caller/log should
    reflect what really landed in the table, not what was attempted.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (resource, str(doc.get("id")), fetched_at, json.dumps(doc, ensure_ascii=False))
        for doc in documents
    ]
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO raw_documents (resource, dip_id, fetched_at, payload_json) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    inserted = conn.total_changes - before
    if inserted != len(rows):
        logger.warning(
            "persist_documents: %d document(s) attempted for resource=%s, but only %d "
            "new row(s) inserted (%d duplicate id(s) within this batch, ignored)",
            len(rows), resource, inserted, len(rows) - inserted,
        )
    return inserted


def log_fetch(
    conn: sqlite3.Connection,
    resource: str,
    params: dict[str, Any],
    item_count: int | None,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fetch_log (resource, params_json, requested_at, item_count, error) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            resource,
            json.dumps(params, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            item_count,
            error,
        ),
    )
    conn.commit()


def fetch_and_store(
    client: DipClient,
    conn: sqlite3.Connection,
    resource: str,
    params: dict[str, Any] | None = None,
    max_items: int | None = None,
) -> int:
    """
    High-level convenience wrapper: fetch a resource and persist it,
    logging success or failure either way. This is the function the
    notebook calls for each resource -- it's the one-line reuse point
    mentioned in the module docstring.
    """
    params = params or {}
    try:
        documents = list(client.get_resource(resource, params=params, max_items=max_items))
    except DipAuthError as exc:
        # Not retryable, needs a human -- in an automated (Dagster/cron)
        # setup this is the exception type that should page someone
        # immediately rather than trigger a job retry.
        logger.error("AUTH ERROR (needs human attention): %s", exc)
        log_fetch(conn, resource, params, item_count=None, error=f"AUTH: {exc}")
        raise
    except DipBadRequestError as exc:
        # Not retryable, it's a code bug (bad filter param) 
        logger.error("BAD REQUEST (check filter params in code): %s", exc)
        log_fetch(conn, resource, params, item_count=None, error=f"BAD_REQUEST: {exc}")
        raise
    except DipInvalidResponseError as exc:
        # HTTP was fine but the body wasn't valid JSON 
        logger.error("INVALID RESPONSE (not valid JSON): %s", exc)
        log_fetch(conn, resource, params, item_count=None, error=f"INVALID_RESPONSE: {exc}")
        raise
    except DipValidationError as exc:
        # Valid JSON, wrong shape -- likely the API's schema changed.
        logger.error("SCHEMA VALIDATION FAILED: %s", exc)
        log_fetch(conn, resource, params, item_count=None, error=f"VALIDATION: {exc}")
        raise
    except DipTransientError as exc:
        # Retries already happened at the HTTP layer and were exhausted.
        # An orchestrator should treat this as "try the whole job again
        # later" rather than "something is broken."
        logger.error("TRANSIENT ERROR (retries exhausted, try again later): %s", exc)
        log_fetch(conn, resource, params, item_count=None, error=f"TRANSIENT: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001 - catch-all for genuinely unexpected failures
        logger.exception("UNEXPECTED failure for resource=%s params=%s", resource, params)
        log_fetch(conn, resource, params, item_count=None, error=f"UNEXPECTED: {exc}")
        raise

    count = persist_documents(conn, resource, documents)
    log_fetch(conn, resource, params, item_count=count)
    logger.info("Stored %d '%s' documents", count, resource)
    return count
