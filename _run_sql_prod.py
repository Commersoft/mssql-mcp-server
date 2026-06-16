"""Runner SQL dla serwera PROD cs-sql03 / baza cs04."""
import sys
import pyodbc

CS = "Driver={ODBC Driver 17 for SQL Server};Server=cs-sql03;Database=cs04;UID=csldap;PWD=91E218A7-C1F1-439A-A5C5-40662F6EE9AB;TrustServerCertificate=yes;"

def main():
    with open(sys.argv[1], encoding="utf-8") as fh:
        query = fh.read()
    with pyodbc.connect(CS, timeout=15) as conn:
        conn.setdecoding(pyodbc.SQL_CHAR, encoding="utf-8")
        conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-8")
        with conn.cursor() as cursor:
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
