
  
    
    
    create  table main."mart_rentenpolitik_monitor"
    as
        -- mart_rentenpolitik_monitor.sql
--
-- The client-facing deliverable: one row per legislative procedure (Vorgang)
-- relevant to pension policy, with its current status, originator(s), and
-- topic tags, ready to sort by most-recently-updated for a monitoring feed.
--
-- Initiativen and Sachgebiete are re-aggregated back into comma-separated
-- text here (via group_concat) because this table is meant to be read
-- directly by a human/report, not joined further -- one row per Vorgang is
-- the right grain for that. If a consumer needs to filter/join on a single
-- topic or initiator, use stg_dip__vorgang_sachgebiete /
-- stg_dip__vorgang_initiativen directly instead.

with sachgebiete_agg as (
    select
        vorgang_id,
        group_concat(distinct sachgebiet) as sachgebiete
    from main."stg_dip__vorgang_sachgebiete"
    group by vorgang_id
),

initiativen_agg as (
    select
        vorgang_id,
        group_concat(distinct initiative) as initiativen
    from main."stg_dip__vorgang_initiativen"
    group by vorgang_id
)

select
    v.vorgang_id,
    v.titel,
    v.vorgangstyp,
    v.beratungsstand,
    v.wahlperiode,
    v.datum,
    v.aktualisiert,
    initiativen_agg.initiativen,
    sachgebiete_agg.sachgebiete,
    v.abstract,
    v.gesta,
    'https://dip.bundestag.de/vorgang/-/' || v.vorgang_id as dip_url,
    v.fetched_at as last_ingested_at
from main."stg_dip__vorgaenge" v
left join sachgebiete_agg on v.vorgang_id = sachgebiete_agg.vorgang_id
left join initiativen_agg on v.vorgang_id = initiativen_agg.vorgang_id
order by v.aktualisiert desc

  