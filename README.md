# DIP Rentenpolitik Monitoring — Pipeline Prototype

Case study submission for Cosmonauts & Kings. Ingests German Bundestag
legislative procedures (`vorgang`) related to pension policy from the
[DIP API](https://dip.bundestag.de/über-dip/hilfe/api) into SQLite, then
transforms them with dbt into a client-ready monitoring feed.

## Structure

```
notebook/
  dip_client.py       reusable API client (retries, cursor pagination, logging)
  dip_ingest.ipynb     fetches Rentenpolitik-related `vorgang` records -> SQLite
  .env.example          copy to .env, add your DIP_API_KEY
  
dbt_project/
  models/staging/       raw JSON -> typed columns + unnested topic/initiator arrays
  models/marts/         mart_rentenpolitik_monitor.sql -- the client deliverable
  profiles.yml.example  copy to ~/.dbt/profiles.yml, points at ../dip.sqlite
concept/
  concept.pdf             2-page pipeline concept
AI_chat_history.pdf
README.md
dip.sqlite
requirements.txt       pinned deps for the ingest step (requests, python-dotenv, pandas, jupyter)
```

## Setup

1. **Get an API key.** Email `parlamentsdokumentation@bundestag.de` with your
   name + email (personal keys are valid ~10 years) if the API has expired. Check
   `https://dip.bundestag.de/über-dip/hilfe/api` first in case a fresh
   temporary public key is listed there
2. `cd notebook && cp .env.example .env` and fill in `DIP_API_KEY`.
3. Create and activate a virtual environment, then install dependencies:
   ```
   python3 -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate

   pip install -r requirements.txt
   ```
4. Run `dip_ingest.ipynb` top to bottom. This creates `dip.sqlite` in the
   repo root.
5. `pip install dbt-core dbt-sqlite`
6. `cd dbt_project && cp profiles.yml.example ~/.dbt/profiles.yml` (adjust
   the path in `schemas_and_paths` if you didn't run step 4 from the
   default location).
7. `dbt run` then `dbt test`. Inspect the result:
   `sqlite3 ../dip.sqlite "select titel, beratungsstand, aktualisiert from mart_rentenpolitik_monitor limit 10;"`

## Notes on scope and what I'd add with more time

- Only `vorgang` is ingested. `dip_client.py` is written so `aktivitaet`
  (individual speeches/Kleine Anfragen) and `plenarprotokoll-text` (full
  debate transcripts) are a one-line addition each — same client, same
  `fetch_and_store()` call, same raw-table pattern. I scoped to one
  resource to keep the first pass reviewable within the ~3h budget, not
  because the others are harder.
- The mart currently answers "what pension-related procedures exist and
  what state are they in." It does not yet do the AI-assisted relevance
  scoring / summarization` — that's the
  next layer, operating on top of this dbt output, not inside it.
- `stg_dip__vorgang_sachgebiete` / `..._initiativen` unnest DIP's array
  fields into proper bridge tables (grain: one row per tag/originator) so
  they can be filtered or joined on directly; `mart_rentenpolitik_monitor`
  re-flattens them back to one-row-per-Vorgang via `group_concat` because
  that's the report grain a human/client actually wants to read.
-
