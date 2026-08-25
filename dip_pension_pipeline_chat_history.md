# Chat History — DIP API Pension Policy Monitoring Pipeline
Case study context: 3-hour Political Data Engineering case study using the German Bundestag's DIP API (Dokumentations- und Informationssystem für Parlamentsmaterialien), building a raw-data → SQLite → dbt pipeline for a pension-policy early-warning system.

---

## 1. Finding the DIP API documentation

**Ask:** Search for official DIP API documentation — base URL, authentication, interactive docs.

**Findings:**
- Base URL: `https://search.dip.bundestag.de/api/v1`
- Authentication: API key, via `Authorization: ApiKey <key>` header or `?apikey=<key>` query param. Individual keys requested via email to parlamentsdokumentation@bundestag.de; a temporary public key is also available (expires periodically).
- Interactive docs (Swagger UI): `https://search.dip.bundestag.de/api/v1/swagger-ui/`
- Official help page: `https://dip.bundestag.de/über-dip/hilfe/api`
- Community resource: `https://github.com/bundesAPI/dip-bundestag-api` (endpoint list, Python client, raw OpenAPI YAML)

---

## 2. What information the API exposes

Core entities, each with a list endpoint and an `/{id}` endpoint:

- **Aktivität** (`/aktivitaet`) — parliamentary activities (speeches, etc.)
- **Drucksache** (`/drucksache`, `/drucksache-text`) — printed documents (bills, motions, reports); `-text` variant includes full text
- **Person** (`/person`) — MP master data
- **Plenarprotokoll** (`/plenarprotokoll`, `/plenarprotokoll-text`) — plenary session transcripts; `-text` variant includes full text
- **Vorgang** (`/vorgang`) — legislative/parliamentary procedures — the aggregation point tying documents and activities together
- **Vorgangsposition** (`/vorgangsposition`) — individual procedural steps within a Vorgang

Pattern: list endpoints support filtering + cursor pagination (max 100 items/page for metadata, max 10/page for full-text resources); ID endpoints return one record.

---

## 3. `/vorgang` endpoint — exact schema (from OpenAPI YAML)

**Documented query parameters:**
`f.datum.start`, `f.datum.end`, `f.aktualisiert.start`, `f.aktualisiert.end`, `f.wahlperiode`, `f.drucksache`, `f.id`, `f.plenarprotokoll`, `f.dokumentart`, `f.dokumentnummer`, `f.drucksachetyp`, `f.frage_nummer`, `f.gesta`, `format`, `cursor`

**Response shape:**
```json
{
  "numFound": 176,
  "cursor": "...",
  "documents": [ /* Vorgang objects, max 100/page */ ]
}
```

**Vorgang object fields:** `id`, `typ`, `beratungsstand`, `vorgangstyp`, `wahlperiode`, `initiative`, `datum`, `aktualisiert`, `titel`, `abstract`, `sachgebiet`, `deskriptor`, `gesta`, `zustimmungsbeduerftigkeit`, `kom`, `ratsdok`, `verkuendung`, `inkrafttreten`, `archiv`, `mitteilung`, `vorgang_verlinkung`, `sek`.

Required fields: `id`, `typ`, `vorgangstyp`, `wahlperiode`, `aktualisiert`, `titel`.

Errors: `400` (bad request), `401` (missing/invalid key), `404` (ID not found).

---

## 4. Pipeline planning

**Objective:** monitoring pipeline for pension policy ("Rentenpolitik") as an early-warning system.

**Key design decisions made:**
- Primary entity: `Vorgang` (the trackable political process); `Vorgangsposition` and `Drucksache` as drill-down/link tables.
- No dedicated topic filter exists in the API (`f.sachgebiet`/`f.deskriptor` don't exist) — topic matching happens via keyword search.
- **Discovery:** `f.titel` — though undocumented in the OpenAPI spec — works empirically as a real filter (confirmed via distinct `numFound` per search term). Lesson: verify empirically rather than trust the spec as fully exhaustive.
- Search terms used:
  ```python
  QUERIES = [
      {"f.titel": "Rente"},
      {"f.titel": "Rentenversicherung"},
      {"f.titel": "Altersvorsorge"},
      {"f.titel": "Grundrente"},
      {"f.titel": "Rentenreform"},
  ]
  ```
- Result counts (stable across 3 separate runs): Rente=692, Rentenversicherung=949, Altersvorsorge=163, Grundrente=37, Rentenreform=25.
- Decision: keep raw data un-scoped by Wahlperiode (full historical pull), and build a *filtered* mart on top for the actual early-warning feed — rather than restricting the raw fetch.
- Deduplication verified: across overlapping keyword queries, final row count in mart = 1,767 with **1,767 distinct IDs** — confirms dedup logic is correct.

---

## 5. Manual entity-relationship tracing (before automating)

**Vorgang → Vorgangsposition → Drucksache:**
- `/vorgangsposition?f.vorgang={id}` returns procedural steps for a Vorgang.
- Each `Vorgangsposition.fundstelle` object holds the actual document link: `fundstelle.id` = Drucksache ID, `fundstelle.dokumentart` = `"Drucksache"` or `"Plenarprotokoll"`.
- Traced example: Vorgang `100154` (1998 budget matter, later identified as a false-positive keyword match on "Rentenversicherung") → Vorgangsposition `209602` → Drucksache `82891` (BR Unterrichtung, 1998-11-13).
- Found: `fundstelle` already carries `dokumentnummer`, `datum`, `drucksachetyp`, `herausgeber`, `pdf_url` — may remove the need for a separate `/drucksache/{id}` call in many cases.

**Drucksache → Drucksache-Text:**
- Confirmed schema relationship: `/drucksache-text/{id}` returns everything `/drucksache/{id}` does, plus a `text` field. Same `id`, consistent metadata across both calls.
- **Key finding:** when full text isn't indexed (e.g., pre-2003 Bundesrat documents), the `text` field is not null/empty — it's the literal string `"[NoTextAvailable]"`. This sentinel must be explicitly handled (e.g., converted to `NULL` + flagged) in any downstream transform, since naive truthy checks would treat it as real content.
- Decision: full-text fetching deferred/out of scope for the early-warning use case — `titel`, `abstract`, `deskriptor`, `beratungsstand`, and `pdf_url` are sufficient for alerting; full text only needed on manual drill-down.

---

## 6. Pipeline build & test (fetch → SQLite → dbt)

- Built `fetch_log` table: records every API call (resource, params, timestamp, item_count, error) — supports auditability/reproducibility.
- Verified `dbt run` and `dbt test` execute successfully against the SQLite raw data.
- Confirmed via terminal: `mart_rentenpolitik_monitor` mart exists and is queryable.

**Data quality check on `mart_rentenpolitik_monitor` (1,767 rows):**
- No duplicate IDs (1,767 rows = 1,767 distinct `vorgang_id`).
- `beratungsstand` distribution: dominated by closed/historical statuses (`Beantwortet`: 1209, `Abgeschlossen - Ergebnis siehe Vorgangsablauf`: 174, `Abgelehnt`: 118, `Verkündet`: 62, etc.), with a small number of genuinely open items (`Noch nicht beraten`: 4, `Überwiesen`: 3, `In der Beratung`: 1, `Beschlussempfehlung liegt vor`: 2).
- `wahlperiode` spans WP 7 (1976) through WP 21 (current) — confirms this is a full historical archive, not a scoped current-term dataset.
- Null patterns in `initiativen`, `sachgebiete`, `gesta`, `abstract` largely expected given high proportion of `Schriftliche Frage` (written question) records, which don't populate bill-specific fields.
- `abstract` field contains raw HTML (e.g., `<br />`) — noted for potential cleanup if surfaced to end users later.
- 70 nulls in `beratungsstand` — flagged for follow-up investigation (check for correlation with a specific `vorgangstyp`).

---

## 7. Early-warning mart design

**Approach:** dbt seed-based status mapping (rather than hardcoded SQL `WHERE beratungsstand NOT IN (...)`) for transparency and maintainability.

**`seeds/dip_status_mapping.csv`:**
```csv
beratungsstand,status_category
Beantwortet,closed
Abgeschlossen - Ergebnis siehe Vorgangsablauf,closed
Abgelehnt,closed
Verkündet,closed
Nicht abgeschlossen - Einzelheiten siehe Vorgangsablauf,open
Erledigt durch Ablauf der Wahlperiode,closed
Für erledigt erklärt,closed
Angenommen,closed
Abgeschlossen,closed
Zusammengeführt mit... (siehe Vorgangsablauf),closed
Noch nicht beraten,open
Überwiesen,open
Zurückgezogen,closed
Beschlussempfehlung liegt vor,open
In der Beratung (Einzelheiten siehe Vorgangsablauf),open
```
(Analyst should review this mapping — it's a judgment call, not derived from API semantics.)

**`dbt_project.yml` var:**
```yaml
vars:
  current_wahlperiode: 21
```

**`models/marts/mart_rentenpolitik_alerts.sql`:**
```sql
with base as (
    select * from {{ ref('mart_rentenpolitik_monitor') }}
),

status_mapped as (
    select
        b.*,
        coalesce(s.status_category, 'unknown') as status_category
    from base b
    left join {{ ref('dip_status_mapping') }} s
        on b.beratungsstand = s.beratungsstand
)

select
    vorgang_id,
    titel,
    vorgangstyp,
    beratungsstand,
    status_category,
    wahlperiode,
    datum,
    aktualisiert,
    initiativen,
    sachgebiete,
    abstract,
    dip_url,
    last_ingested_at,
    julianday('now') - julianday(aktualisiert) <= 14 as is_recent_update
from status_mapped
where wahlperiode = {{ var('current_wahlperiode') }}
  and status_category in ('open', 'unknown')
order by aktualisiert desc
```

Design rationale: unmapped (`unknown`) statuses are surfaced rather than silently dropped, so a new/unrecognized `beratungsstand` value doesn't cause a real active Vorgang to disappear from the feed unnoticed.

**Tests added (`schema.yml`):**
- `mart_rentenpolitik_alerts.vorgang_id`: unique, not_null
- `mart_rentenpolitik_alerts.status_category`: not_null, accepted_values `['open', 'unknown']`
- `dip_status_mapping.beratungsstand`: unique, not_null
- `dip_status_mapping.status_category`: not_null, accepted_values `['open', 'closed']`

**Run commands:**
```bash
dbt seed
dbt run
dbt test
```

Expected result: dramatic shrinkage from 1,767 rows down to a small, high-signal set of currently-active WP 21 pension-related Vorgänge — the actual early-warning feed.

---

## Open items / next steps at time of export
- Confirm the 70 null `beratungsstand` rows (check correlation with `vorgangstyp`).
- Review/refine the `dip_status_mapping.csv` categorization with domain judgment.
- Run `mart_rentenpolitik_alerts` and validate row count/content.
- Consider HTML-stripping for `abstract` if surfaced downstream.
- Decide on repeated-retrieval strategy for production use (`f.aktualisiert.start` incremental fetch vs. full re-pull + dedup).
