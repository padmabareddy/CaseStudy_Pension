
    
    create view main."stg_dip__vorgang_initiativen" as
    -- stg_dip__vorgang_initiativen.sql
--
-- Unnests the `initiative` array (who introduced/originated the procedure,
-- e.g. a Fraktion or the Bundesregierung) into one row per
-- (vorgang_id, initiative). Relevant for Public Affairs work: knowing
-- *who* is driving a procedure is often as important as its status.

select
    v.vorgang_id,
    je.value as initiative
from main."stg_dip__vorgaenge" v
join json_each(v.initiative_json) je
where v.initiative_json is not null;