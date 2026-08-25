-- stg_dip__vorgaenge.sql
--
-- Deduplicates the append-only raw_documents table down to the latest
-- known state per Vorgang (legislative procedure), and flattens the JSON
-- payload into typed columns. Scalar fields are extracted directly;
-- array fields (initiative, sachgebiet, deskriptor) are kept as raw JSON
-- text here and unnested into separate bridge tables below, since a
-- Vorgang can have zero, one, or many of each -- flattening them into
-- this table would either lose information or require pivoting.
--
-- Field names/types are taken from the live DIP Swagger schema for
-- GET /vorgang (id, typ, beratungsstand, vorgangstyp, wahlperiode,
-- initiative, datum, aktualisiert, titel, abstract, sachgebiet,
-- deskriptor, gesta, ...).

with raw as (
    select *
    from {{ source('dip_raw', 'raw_documents') }}
    where resource = 'vorgang'
),

-- Keep only the most recently fetched payload per Vorgang. The raw table
-- is intentionally append-only (see dip_client.py docstring); this is
-- where "latest known state" gets defined.
latest_fetch as (
    select
        dip_id,
        max(fetched_at) as fetched_at
    from raw
    group by dip_id
),

deduped as (
    select raw.*
    from raw
    inner join latest_fetch
        on raw.dip_id = latest_fetch.dip_id
        and raw.fetched_at = latest_fetch.fetched_at
)

select
    cast(json_extract(payload_json, '$.id') as integer)          as vorgang_id,
    json_extract(payload_json, '$.titel')                        as titel,
    json_extract(payload_json, '$.abstract')                     as abstract,
    json_extract(payload_json, '$.vorgangstyp')                  as vorgangstyp,
    json_extract(payload_json, '$.beratungsstand')                as beratungsstand,
    cast(json_extract(payload_json, '$.wahlperiode') as integer) as wahlperiode,
    json_extract(payload_json, '$.datum')                        as datum,
    json_extract(payload_json, '$.aktualisiert')                 as aktualisiert,
    json_extract(payload_json, '$.gesta')                        as gesta,
    -- kept as raw JSON arrays; unnested in stg_dip__vorgang_sachgebiete
    json_extract(payload_json, '$.initiative')                   as initiative_json,
    json_extract(payload_json, '$.sachgebiet')                   as sachgebiet_json,
    json_extract(payload_json, '$.deskriptor')                   as deskriptor_json,
    fetched_at
from deduped
