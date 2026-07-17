"""Pomocniczy runner SQL — ta sama ścieżka co execute_sql w mssql_simple_mcp.py.
Użycie: python _run_sql.py <plik_z_sql>
Czyta .env z katalogu serwera, łączy się przez pyodbc i wypisuje wynik.
"""
import os
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


def main():
    _load_env()
    with open(sys.argv[1], encoding="utf-8") as fh:
        query = fh.read()
    with pyodbc.connect(_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("while @@trancount > 0 rollback tran")
            cursor.execute(query)
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


if __name__ == "__main__":
    main()
