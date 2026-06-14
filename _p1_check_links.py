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

with pyodbc.connect(cs) as conn:
    cur = conn.cursor()
    print("=== csNGAppWindowsLinks FROM csSalesHeaders ===")
    cur.execute(
        """
        SELECT l.placement, l.ord, l.appWindowIdentTo, l.appWindowLinkDesc_PL
        FROM dbo.csNGAppWindowsLinks l
        WHERE l.appWindowIdentFrom = 'csSalesHeaders'
        ORDER BY l.placement, l.ord
        """
    )
    for row in cur.fetchall():
        print(" | ".join("NULL" if v is None else str(v) for v in row))

    print("\n=== csNGAppWindowsLinksFields sample ===")
    cur.execute(
        """
        SELECT TOP 20 l.appWindowIdentTo, lf.dataFieldIdentFrom, lf.dataFieldIdentTo, lf.sourceKindFrom
        FROM dbo.csNGAppWindowsLinksFields lf
        JOIN dbo.csNGAppWindowsLinks l
          ON l.appWindowIdentFrom = lf.appWindowIdentFrom
         AND l.appWindowIdentTo = lf.appWindowIdentTo
        WHERE l.appWindowIdentFrom = 'csSalesHeaders'
        ORDER BY l.appWindowIdentTo
        """
    )
    for row in cur.fetchall():
        print(" | ".join("NULL" if v is None else str(v) for v in row))