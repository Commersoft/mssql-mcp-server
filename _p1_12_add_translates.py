# P1-12 — rejestracja brakujących tłumaczeń zakładek wizarda csSalesHeaders/main_ins.vue
# Dwustopniowo: csTranslateJSONSave (Content_PL/EN) -> csNGAppWindowTranslatesJSONSave (translateIdent)
import os, json, uuid
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
    f"Driver={{{driver}}};Server={host};Database={os.environ['MSSQL_DATABASE']};"
    f"UID={os.environ['MSSQL_USER']};PWD={os.environ['MSSQL_PASSWORD']};TrustServerCertificate=yes;"
)

NAMESPACE = "E4B58826-69B9-4180-8A58-953B13AB2C77"
APP_WINDOW = "csSalesHeaders"

# translateIdent -> (PL, EN)
ITEMS = {
    "PROMOTION_ELIGIBILITY": ("Kwalifikowalność", "Eligibility"),
    "PRODUCT_SCOPE": ("Zasięg produktowy", "Product scope"),
    "PROMOTION_REWARDS": ("Nagrody", "Rewards"),
    "PROMO_SAVE_HEADER_FOR_CONDITIONS": (
        "Zapisz nagłówek promocji, aby skonfigurować tę sekcję.",
        "Save the promotion header to configure this section.",
    ),
}


def exec_jsonsave(cur, proc, data):
    js = json.dumps(data, ensure_ascii=False).replace("'", "''")
    sql = (
        "DECLARE @response xml;\n"
        f"EXEC dbo.{proc} @data = N'{js}', "
        f"@csAppNameSpacesG = '{NAMESPACE}', @response = @response OUTPUT;\n"
        "SELECT CONVERT(nvarchar(max), @response) AS response;"
    )
    cur.execute(sql)
    row = cur.fetchone()
    resp = row[0] if row else None
    return resp


conn = pyodbc.connect(cs)
conn.autocommit = True
cur = conn.cursor()

# Krok 1 — csTranslate (Content_PL/EN) z nowymi GUID
translate_rows = []
ident_to_guid = {}
for ident, (pl, en) in ITEMS.items():
    g = str(uuid.uuid4()).upper()
    ident_to_guid[ident] = g
    translate_rows.append({"_opr": "I", "csTranslateG": g, "Content_PL": pl, "Content_EN": en})

r1 = exec_jsonsave(cur, "csTranslateJSONSave", translate_rows)
print("csTranslateJSONSave response:", r1, "(None = OK)")

# Krok 2 — csNGAppWindowTranslates (translateIdent -> csTranslateG) dla okna csSalesHeaders
win_rows = []
for ident, g in ident_to_guid.items():
    win_rows.append({
        "_opr": "I",
        "csNGAppWindowTranslatesG": str(uuid.uuid4()).upper(),
        "csAppNameSpacesG": NAMESPACE,
        "appWindowIdent": APP_WINDOW,
        "csTranslateG": g,
        "translateIdent": ident,
    })

r2 = exec_jsonsave(cur, "csNGAppWindowTranslatesJSONSave", win_rows)
print("csNGAppWindowTranslatesJSONSave response:", r2, "(None = OK)")

# Weryfikacja
cur.execute(
    """
    SELECT t.translateIdent, tr.Content_PL, tr.Content_EN
    FROM dbo.csNGAppWindowTranslates t WITH(NOLOCK)
    LEFT JOIN dbo.csTranslate tr WITH(NOLOCK) ON tr.csTranslateG = t.csTranslateG
    WHERE t.appWindowIdent = 'csSalesHeaders'
      AND t.translateIdent IN ('PROMOTION_ELIGIBILITY','PRODUCT_SCOPE','PROMOTION_REWARDS','PROMO_SAVE_HEADER_FOR_CONDITIONS')
    ORDER BY t.translateIdent
    """
)
print("\n=== VERIFY ===")
for ident, pl, en in cur.fetchall():
    print(f"  {ident:35} PL={pl!r} EN={en!r}")
conn.close()
