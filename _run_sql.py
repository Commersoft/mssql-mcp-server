"""Pomocniczy runner SQL — ta sama ścieżka co execute_sql w mssql_simple_mcp.py.
Użycie: python _run_sql.py <plik_z_sql>
Czyta .env z katalogu serwera, łączy się przez pyodbc i wypisuje wynik.
Plik może zawierać separatory GO (osobna linia) — batche wykonują się po kolei
na jednym połączeniu, np. skrypt wdrożeniowy 3-batch z rag_get_sql_object.
"""
import os
import re
import sys
import pyodbc


def _load_env():
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _connection_string() -> str:
    driver = os.environ.get("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
    server = os.environ.get("MSSQL_HOST") or os.environ.get("MSSQL_SERVER", "localhost")
    database = os.environ["MSSQL_DATABASE"]
    user = os.environ["MSSQL_USER"]
    password = os.environ["MSSQL_PASSWORD"]
    return (
        f"Driver={{{driver}}};"
        f"Server={server};"
        f"Database={database};"
        f"UID={user};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
    )


def _print_result(cursor, batch_label=""):
    prefix = f"[{batch_label}] " if batch_label else ""
    # batch może zwrócić wiele zestawów wyników — bez pętli nextset() ginęły
    # kolejne SELECT-y ORAZ błędy rzucane przez dalsze instrukcje batcha
    set_no = 0
    while True:
        set_no += 1
        tag = prefix if set_no == 1 else f"{prefix}[set {set_no}] "
        if cursor.description:
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            print(tag + " | ".join(cols))
            print("-" * max(len(" | ".join(cols)), 3))
            for row in rows:
                print(" | ".join("NULL" if v is None else str(v) for v in row))
            print(f"\n{tag}({len(rows)} rows)")
        else:
            rc = cursor.rowcount
            print(f"{tag}OK. Rows affected: {rc if rc >= 0 else 0}")
        if not cursor.nextset():
            break


def main():
    _load_env()
    # utf-8-sig, nie utf-8 — PowerShell 5.1 (Out-File/Set-Content -Encoding UTF8) zapisuje BOM,
    # a MSSQL traktuje go jako znak: "Incorrect syntax near '﻿'"
    with open(sys.argv[1], encoding="utf-8-sig") as fh:
        query = fh.read()
    # split na separatorach GO (case-insensitive, osobna linia) — pyodbc nie zna GO
    batches = [b.strip() for b in re.split(r"(?im)^\s*GO\s*$", query) if b.strip()]
    with pyodbc.connect(_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("while @@trancount > 0 rollback tran")
            multi = len(batches) > 1
            for i, batch in enumerate(batches, 1):
                cursor.execute(batch)
                _print_result(cursor, f"batch {i}/{len(batches)}" if multi else "")


if __name__ == "__main__":
    main()
