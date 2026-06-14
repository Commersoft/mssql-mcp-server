import os
import pyodbc
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()

driver = os.environ.get("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
host = os.environ.get("MSSQL_HOST") or os.environ.get("MSSQL_SERVER", "localhost")
cs = (
    f"Driver={{{driver}}};"
    f"Server={host};"
    f"Database={os.environ['MSSQL_DATABASE']};"
    f"UID={os.environ['MSSQL_USER']};"
    f"PWD={os.environ['MSSQL_PASSWORD']};"
    f"TrustServerCertificate=yes;"
)


def run(title, sql, params=None):
    print(f"\n=== {title} ===")
    with pyodbc.connect(cs) as conn:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        if not cur.description:
            print("(no result set)")
            return []
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        print(" | ".join(cols))
        print("-" * 80)
        for row in rows[:80]:
            print(" | ".join("NULL" if v is None else str(v) for v in row))
        if len(rows) > 80:
            print(f"... +{len(rows) - 80} more ({len(rows)} total)")
        return rows


tables = [
    "csSalesHeaders",
    "csSalesHeadersConditions",
    "csSalesHeadersCustomers",
    "csSalesHeadersItems",
    "csSalesHeadersGifts",
    "csSalesHeadersPricingPolicies",
    "csPromotionTypes",
    "csPromotionConditionTypes",
    "csSalesHeadersApplications",
    "csSalesHeadersParticipants",
    "csSalesHeadersKPI",
    "csSalesHeadersBudget",
]
placeholders = ",".join("?" * len(tables))
found = run(
    "P0-01 TABLES",
    f"SELECT t.name FROM sys.tables t WHERE t.name IN ({placeholders}) ORDER BY t.name",
    tables,
)
found_names = {r[0] for r in found}
print("\nMISSING:", ", ".join(t for t in tables if t not in found_names) or "(none)")

for tbl in [
    "csSalesHeaders",
    "csSalesHeadersConditions",
    "csPromotionTypes",
    "csPromotionConditionTypes",
]:
    run(
        f"P0-01 COLUMNS {tbl}",
        """
        SELECT c.name, t.name AS type_name, c.max_length, c.is_nullable
        FROM sys.columns c
        JOIN sys.types t ON c.user_type_id = t.user_type_id
        WHERE c.object_id = OBJECT_ID(?)
        ORDER BY c.column_id
        """,
        [f"dbo.{tbl}"],
    )

windows = [
    "csSalesHeaders",
    "csSalesHeadersConditions",
    "csSalesHeadersCustomers",
    "csSalesHeadersCustomersExclusions",
    "csSalesHeadersCustomersGroups",
    "csSalesHeadersItems",
    "csSalesHeadersItemsGroups",
    "csSalesHeadersItemsExclusions",
    "csSalesHeadersItemsActivators",
    "csSalesHeadersGifts",
    "csSalesHeadersPricingPolicies",
    "csSalesHeadersWarehouses",
    "csSalesHeadersGroups",
    "csSalesHeadersB2BPortalsExclusions",
    "csSalesHeaders4Customers",
    "csPromotionTypes",
    "csPromotionConditionTypes",
]
wph = ",".join("?" * len(windows))
ng_rows = run(
    "P0-02 NG WINDOWS",
    f"""
    SELECT w.appWindowIdent,
           CASE WHEN w.dataSets IS NULL OR LEN(w.dataSets) < 5 THEN 'EMPTY' ELSE 'OK' END AS dataSetsStatus,
           LEN(ISNULL(w.dataSets, '')) AS dataSetsLen
    FROM dbo.csNGAppWindows w
    WHERE w.appWindowIdent IN ({wph})
    ORDER BY w.appWindowIdent
    """,
    windows,
)
ng_found = {r[0] for r in ng_rows}
print("\nNG MISSING:", ", ".join(w for w in windows if w not in ng_found) or "(none)")

run(
    "P0-03 PROC SEARCH csSalesHeaders (non-NG)",
    """
    SELECT TOP 30 o.name, o.type_desc
    FROM sys.objects o
    JOIN sys.sql_modules m ON o.object_id = m.object_id
    WHERE m.definition LIKE '%csSalesHeaders%'
      AND o.type IN ('P', 'FN', 'IF', 'TF')
      AND o.name NOT LIKE 'csNG%'
    ORDER BY o.name
    """,
)

run(
    "P0-03 PROC SEARCH pricing/discount",
    """
    SELECT TOP 30 o.name, o.type_desc
    FROM sys.objects o
    JOIN sys.sql_modules m ON o.object_id = m.object_id
    WHERE (
      m.definition LIKE '%getCUnitPrice%'
      OR m.definition LIKE '%useForGetCUnitPriceAndDiscount%'
      OR m.definition LIKE '%SalesHeader%Discount%'
    )
      AND o.type IN ('P', 'FN', 'IF', 'TF')
    ORDER BY o.name
    """,
)

run(
    "P0-04 csSalesHeaders status-related columns",
    """
    SELECT c.name
    FROM sys.columns c
    WHERE c.object_id = OBJECT_ID('dbo.csSalesHeaders')
      AND (
        c.name LIKE '%Status%'
        OR c.name IN (
          'StackingRule', 'Priority', 'BaselinePeriodFrom', 'BaselinePeriodTo',
          'SupplierDesc', 'OwnerDesc', 'Tags', 'csSuppliersId', 'csUsrsId'
        )
      )
    ORDER BY c.name
    """,
)

run(
    "P0-04 csStatusesValues columns",
    """
    SELECT c.name
    FROM sys.columns c
    WHERE c.object_id = OBJECT_ID('dbo.csStatusesValues')
    ORDER BY c.column_id
    """,
)

run(
    "P0-04 statuses used by csSalesHeaders (via csSalesHeadersStatusId)",
    """
    SELECT sv.csStatusesValuesG, sv.csStatusesValuesId, COUNT(*) cnt
    FROM dbo.csSalesHeaders sh
    JOIN dbo.csStatusesValues sv ON sv.csStatusesValuesId = sh.csSalesHeadersStatusId
    GROUP BY sv.csStatusesValuesG, sv.csStatusesValuesId
    ORDER BY cnt DESC
    """,
)

run(
    "P0-04 csStatuses linked to sales headers",
    """
    SELECT DISTINCT s.csStatusesG, s.csStatusesId, COUNT(sh.csSalesHeadersId) AS promo_count
    FROM dbo.csStatuses s
    JOIN dbo.csStatusesValues sv ON sv.csStatusesId = s.csStatusesId
    JOIN dbo.csSalesHeaders sh ON sh.csSalesHeadersStatusId = sv.csStatusesValuesId
    GROUP BY s.csStatusesG, s.csStatusesId
    ORDER BY promo_count DESC
    """,
)

run(
    "P0-04 transits for sales header statuses",
    """
    SELECT cst.csStatusesValuesIdFrom,
           cst.csStatusesValuesIdTo,
           cst.StatusValueTransitDesc_PL,
           cst.Auto
    FROM dbo.csStatusesValuesTransits cst
    WHERE cst.csStatusesValuesIdFrom IN (
      SELECT DISTINCT sh.csSalesHeadersStatusId
      FROM dbo.csSalesHeaders sh
      WHERE sh.csSalesHeadersStatusId IS NOT NULL
    )
    ORDER BY cst.csStatusesValuesIdFrom, cst.csStatusesValuesIdTo
    """,
)

run(
    "P0-03 getCUnitPrice procedures",
    """
    SELECT o.name, o.type_desc
    FROM sys.objects o
    WHERE o.name LIKE '%getCUnitPrice%' OR o.name LIKE '%CUnitPrice%'
    ORDER BY o.name
    """,
)

run(
    "P0-03 SalesHeaders discount procedures",
    """
    SELECT o.name, o.type_desc
    FROM sys.objects o
    WHERE (
      o.name LIKE '%SalesHeader%'
      AND (o.name LIKE '%Discount%' OR o.name LIKE '%Price%' OR o.name LIKE '%Calc%')
    )
    ORDER BY o.name
    """,
)

run(
    "P0-05 csPromotionTypes",
    """
    SELECT pt.csPromotionTypesG, pt.PromotionType, pt.PromotionTypeDesc_PL
    FROM dbo.csPromotionTypes pt
    ORDER BY pt.PromotionType
    """,
)

run(
    "P0-06 csPromotionConditionTypes",
    """
    SELECT ct.csPromotionConditionTypesG, ct.ConditionType, ct.ConditionTypeDesc_PL, ct.ValueKind, ct.AllowedOperators
    FROM dbo.csPromotionConditionTypes ct
    ORDER BY ct.ConditionType
    """,
)

PDF_CONDITION_TYPES = [
    "MIN_ORDER_VALUE",
    "MIN_ORDER_QTY",
    "PERIOD_REVENUE",
    "PERIOD_QTY",
    "PRODUCT_IN_ORDER",
    "MANUFACTURER",
    "PRODUCT_CATEGORY",
    "CUSTOMER_TIER",
    "FIRST_ORDER",
    "NEW_PRODUCT_TRIAL",
    "NO_OVERDUE_BALANCE",
    "ORDER_CHANNEL",
]
rows = run(
    "P0-06 missing condition types vs PDF",
    f"""
    SELECT v.expected_type,
           ct.ConditionType AS existing_type,
           ct.ConditionTypeDesc_PL
    FROM (VALUES {','.join('(?)' for _ in PDF_CONDITION_TYPES)}) AS v(expected_type)
    LEFT JOIN dbo.csPromotionConditionTypes ct ON ct.ConditionType = v.expected_type
    ORDER BY v.expected_type
    """,
    PDF_CONDITION_TYPES,
)