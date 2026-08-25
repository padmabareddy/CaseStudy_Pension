
    
    create view main."stg_dip__vorgang_sachgebiete" as
    -- stg_dip__vorgang_sachgebiete.sql
--
-- Unnests the `sachgebiet` (topic area) array into one row per
-- (vorgang_id, sachgebiet), using SQLite's json_each table-valued function.
-- This is what lets the mart filter/group by topic without string matching
-- on JSON text.

select
    v.vorgang_id,
    je.value as sachgebiet
from main."stg_dip__vorgaenge" v
join json_each(v.sachgebiet_json) je
where v.sachgebiet_json is not null;