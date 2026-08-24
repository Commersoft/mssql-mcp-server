"""
Minimal MSSQL MCP Server — only execute_sql tool, no resources.
"""
import os
import pyodbc
from mcp.server.fastmcp import FastMCP


def _load_env():
    """Load credentials from local .env (per-developer, gitignored).

    setdefault: explicit environment variables win over .env values.
    utf-8-sig: .env bywa zapisany z BOM — zwykły utf-8 psuje pierwszy klucz.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                # cudzysłowy w .env chronią '#' w haśle — do connection stringu iść nie mogą,
                # inaczej login pada 18456 (regresja wracała już 3× — patrz rag-mcp-tools §3)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)


_load_env()

mcp = FastMCP("mssql")

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


@mcp.tool()
def execute_sql(query: str) -> str:
    """
    Execute a SQL query or batch on MSSQL server.
    Supports DECLARE, SELECT, INSERT, UPDATE, DELETE, WITH, stored procedures etc.
    Returns results as a table or affected row count.
    """
    try:
        with pyodbc.connect(_connection_string()) as conn:
            with conn.cursor() as cursor:
                # Wyczyść ewentualne otwarte transakcje przed wykonaniem batcha.
                cursor.execute("while @@trancount > 0 rollback tran")
                cursor.execute(query)
                if cursor.description:
                    cols = [d[0] for d in cursor.description]
                    rows = cursor.fetchall()
                    lines = [" | ".join(cols)]
                    lines.append("-" * max(len(lines[0]), 3))
                    for row in rows:
                        lines.append(" | ".join(
                            "NULL" if v is None else str(v) for v in row
                        ))
                    lines.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")
                    return "\n".join(lines)
                else:
                    conn.commit()
                    rc = cursor.rowcount
                    return f"Query executed successfully. Rows affected: {rc if rc >= 0 else 0}"
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
