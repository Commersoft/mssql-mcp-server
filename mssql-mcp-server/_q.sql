select ColumnName, ColumnOrder, BaseType, ColumnParams, IsNullable, DefaultDef
from dbo.csSysColumns with(nolock)
where csSysTablesG = N'C82D8025-8680-4EF5-A848-E31ED0A5EEBE'
order by ColumnOrder;
