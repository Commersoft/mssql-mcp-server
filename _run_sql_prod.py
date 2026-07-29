"""Runner SQL dla serwera PROD cs-sql03 / baza cs04.

Login: adminjmk, haslo z env CSPROD_PWD (nie trzymamy hasel w pliku).
"""
import os
import sys
import pyodbc

PWD = os.environ.get("CSPROD_PWD")
if not PWD:
    sys.exit("Brak env CSPROD_PWD (haslo adminjmk do cs-sql03).")

CS = (
    "Driver={ODBC Driver 17 for SQL Server};Server=cs-sql03;Database=cs04;"
    f"UID=adminjmk;PWD={PWD};TrustServerCertificate=yes;"
)

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # utf-8-sig, nie utf-8 — PowerShell 5.1 (Out-File/Set-Content -Encoding UTF8) zapisuje BOM,
    # a MSSQL traktuje go jako znak: "Incorrect syntax near '﻿'"
    with open(sys.argv[1], encoding="utf-8-sig") as fh:
        query = fh.read()
    # autocommit=True: bez tego pyodbc trzyma niejawna transakcje -> procedury cs* widza
    # @@trancount=1, nie robia wlasnego rollbacku przy bledzie, a blok `with` commituje
    # czesciowe zapisy przy wyjsciu (realny przypadek: osierocone wiersze po bledzie 515).
    with pyodbc.connect(CS, timeout=15, autocommit=True) as conn:
        conn.setdecoding(pyodbc.SQL_CHAR, encoding="cp1250")
        conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16-le")
        with conn.cursor() as cursor:
            cursor.execute(query)
            while True:
                if cursor.description:
                    cols = [d[0] for d in cursor.description]
                    rows = cursor.fetchall()
                    print(" | ".join(cols))
                    print("-" * max(len(" | ".join(cols)), 3))
                    for row in rows:
                        print(" | ".join("NULL" if v is None else str(v) for v in row))
                    print(f"\n({len(rows)} rows)")
                else:
                    rc = cursor.rowcount
                    print(f"OK. Rows affected: {rc if rc >= 0 else 0}")
                if not cursor.nextset():
                    break

if __name__ == "__main__":
    main()
