
    
    

select
    vorgang_id as unique_field,
    count(*) as n_records

from main."mart_rentenpolitik_monitor"
where vorgang_id is not null
group by vorgang_id
having count(*) > 1


