select csAIAgentsId, csAIAgentsG, name, left(role,100) as role_preview, csCompaniesId, isDefault, availableForEveryone
from dbo.csAIAgents
where name like N'%Radek%'
   or name like N'%radek%'
order by csAIAgentsId
