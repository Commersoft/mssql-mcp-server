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

IDENTS = [
    "PROMOTION_TYPE", "BASIC_DATA", "DATES_AND_PARAMETERS",
    "PROMOTION_CONDITIONS", "FLAGS_AND_SETTINGS", "ACTIVATORS",
    "PROMOTION_ELIGIBILITY", "PRODUCT_SCOPE", "PROMOTION_REWARDS",
    "PROMO_SAVE_HEADER_FOR_CONDITIONS",
    "STACKINGRULE_EXCLUSIVE", "STACKINGRULE_STACKABLE", "STACKINGRULE_BEST_PRICE",
    "ACTIONTYPE_GOTO", "ACTIONTYPE_SHOWITEMS",
    "POPUPKIND_FIRST", "POPUPKIND_EVERY",
    "PREVIOUS", "NEXT", "EXECUTE",
]

with pyodbc.connect(cs) as conn:
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in IDENTS)
    cur.execute(
        f"""
        SELECT t.translateIdent, tr.Content_PL, tr.Content_EN
        FROM dbo.csNGAppWindowTranslates t WITH(NOLOCK)
        LEFT JOIN dbo.csTranslate tr WITH(NOLOCK) ON tr.csTranslateG = t.csTranslateG
        WHERE t.appWindowIdent = 'csSalesHeaders'
          AND t.translateIdent IN ({placeholders})
        ORDER BY t.translateIdent
        """,
        IDENTS,
    )
    found = {}
    for ident, pl, en in cur.fetchall():
        found[ident] = (pl, en)

    print("=== EXISTING (csSalesHeaders) ===")
    for ident in IDENTS:
        if ident in found:
            pl, en = found[ident]
            print(f"  OK   {ident:35} PL={pl!r} EN={en!r}")
    print("\n=== MISSING (csSalesHeaders) ===")
    missing = [i for i in IDENTS if i not in found]
    for ident in missing:
        print(f"  MISS {ident}")
    print(f"\nTotal: {len(found)} existing, {len(missing)} missing")
