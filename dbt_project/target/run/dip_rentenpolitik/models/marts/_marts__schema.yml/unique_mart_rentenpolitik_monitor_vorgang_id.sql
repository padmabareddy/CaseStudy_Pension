
    select
      count(*) as failures,
      case when count(*) != 0
        then 'true' else 'false' end as should_warn,
      case when count(*) != 0
        then 'true' else 'false' end as should_error
    from (
      
    
  
    
    

select
    vorgang_id as unique_field,
    count(*) as n_records

from main."mart_rentenpolitik_monitor"
where vorgang_id is not null
group by vorgang_id
having count(*) > 1



  
  
      
    ) dbt_internal_test