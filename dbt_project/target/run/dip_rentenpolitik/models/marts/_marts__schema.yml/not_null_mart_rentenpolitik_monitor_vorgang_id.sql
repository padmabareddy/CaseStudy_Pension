
    select
      count(*) as failures,
      case when count(*) != 0
        then 'true' else 'false' end as should_warn,
      case when count(*) != 0
        then 'true' else 'false' end as should_error
    from (
      
    
  
    
    



select vorgang_id
from main."mart_rentenpolitik_monitor"
where vorgang_id is null



  
  
      
    ) dbt_internal_test