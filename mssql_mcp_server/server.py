#!/usr/bin/env python3
"""
MSSQL MCP Server - A Model Context Protocol server for Microsoft SQL Server
Provides SQL query execution and table introspection capabilities via MCP
"""

import asyncio
import logging
import os
import re
import sys
from typing import Optional, Tuple, List, Dict, Any
from pyodbc import connect, Error as PyODBCError
from mcp.server import Server
from mcp.types import Resource, ResourceTemplate, Tool, TextContent
from urllib.parse import quote
from pydantic import AnyUrl

from . import rag_tools
from . import cs_tools
from . import mail_tools

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mssql_mcp_server")

# Version information
__version__ = "1.0.0"
__author__ = "MSSQL MCP Server Contributors"


class QueryPreprocessor:
    """Handles preprocessing of SQL queries to fix common issues"""
    
    @staticmethod
    def preprocess_query(query: str) -> str:
        """
        Preprocess SQL query to handle newlines and other formatting issues.
        
        - Preserves newlines within string literals
        - Replaces other newlines with spaces
        - Handles GO statements
        - Cleans up excessive whitespace
        
        Args:
            query: Raw SQL query string
            
        Returns:
            Preprocessed query string
        """
        if not query:
            return query
            
        # Remove leading/trailing whitespace
        query = query.strip()
        
        # Handle GO statements (SQL Server batch separator)
        if re.search(r'\bGO\b', query, re.IGNORECASE | re.MULTILINE):
            # Split by GO and return first batch with warning
            parts = re.split(r'\bGO\b', query, flags=re.IGNORECASE | re.MULTILINE)
            if len(parts) > 1:
                logger.warning("Query contains GO statements. Only executing first batch.")
                query = parts[0].strip()
        
        # Process the query character by character
        in_string = False
        in_comment = False
        in_multiline_comment = False
        result = []
        i = 0
        
        while i < len(query):
            # Handle multi-line comments
            if not in_string and i < len(query) - 1:
                if query[i:i+2] == '/*':
                    in_multiline_comment = True
                    result.append(query[i:i+2])
                    i += 2
                    continue
                elif query[i:i+2] == '*/' and in_multiline_comment:
                    in_multiline_comment = False
                    result.append(query[i:i+2])
                    i += 2
                    continue
            
            # Handle single-line comments
            if not in_string and not in_multiline_comment and i < len(query) - 1:
                if query[i:i+2] == '--':
                    in_comment = True
                    result.append(query[i:i+2])
                    i += 2
                    continue
            
            char = query[i]
            
            # Handle string literals
            if char == "'" and not in_comment and not in_multiline_comment:
                # Check for escaped quote
                if i + 1 < len(query) and query[i+1] == "'":
                    result.append("''")
                    i += 2
                    continue
                else:
                    in_string = not in_string
                    result.append(char)
            
            # Handle newlines
            elif char == '\n':
                if in_string or in_multiline_comment:
                    result.append(char)
                elif in_comment:
                    # End single-line comment
                    in_comment = False
                    result.append(char)
                else:
                    # Replace newline with space outside strings/comments
                    if result and result[-1] not in (' ', '\t'):
                        result.append(' ')
            
            # Handle carriage returns
            elif char == '\r':
                if in_string or in_multiline_comment:
                    result.append(char)
                # Skip carriage returns outside strings
            
            # Handle tabs
            elif char == '\t' and not in_string and not in_comment and not in_multiline_comment:
                # Replace tabs with spaces outside strings
                if result and result[-1] != ' ':
                    result.append(' ')
            
            else:
                result.append(char)
            
            i += 1
        
        processed_query = ''.join(result)
        
        # Clean up multiple spaces (but not in strings)
        if not in_string and not in_comment and not in_multiline_comment:
            processed_query = re.sub(r'[ ]{2,}', ' ', processed_query)
        
        return processed_query.strip()


class DatabaseConfig:
    """Handles database configuration from environment variables"""
    
    @staticmethod
    def get_config() -> Tuple[Dict[str, Any], str]:
        """
        Get database configuration from environment variables.
        
        Environment variables:
        - MSSQL_HOST or MSSQL_SERVER: Server hostname (default: localhost)
        - MSSQL_PORT: Server port (default: 1433)
        - MSSQL_USER: Username (required for non-trusted connections)
        - MSSQL_PASSWORD: Password (required for non-trusted connections)
        - MSSQL_DATABASE: Database name (required)
        - MSSQL_DRIVER: ODBC driver (default: ODBC Driver 17 for SQL Server)
        - MSSQL_TRUSTED_CONNECTION: Use Windows authentication (default: no)
        - MSSQL_TRUST_SERVER_CERTIFICATE: Trust server certificate (default: yes)
        - MSSQL_ENCRYPT: Encrypt connection (default: yes)
        - MSSQL_CONNECTION_TIMEOUT: Connection timeout in seconds (default: 30)
        - MSSQL_MULTI_SUBNET_FAILOVER: Enable multi-subnet failover (default: no)
        
        Returns:
            Tuple of (config dict, connection string)
        """
        # Get server from either MSSQL_HOST or MSSQL_SERVER
        server = os.getenv("MSSQL_HOST") or os.getenv("MSSQL_SERVER", "localhost")
        port = os.getenv("MSSQL_PORT", "1433")
        
        # Authentication
        user = os.getenv("MSSQL_USER")
        password = os.getenv("MSSQL_PASSWORD")
        trusted_connection = os.getenv("MSSQL_TRUSTED_CONNECTION", "no").lower() in ('yes', 'true', '1')
        
        # Database
        database = os.getenv("MSSQL_DATABASE")
        if not database:
            raise ValueError("MSSQL_DATABASE environment variable is required")
        
        # Driver configuration
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
        
        # Connection options
        trust_cert = os.getenv("MSSQL_TRUST_SERVER_CERTIFICATE", "yes").lower() in ('yes', 'true', '1')
        encrypt = os.getenv("MSSQL_ENCRYPT", "yes").lower() in ('yes', 'true', '1')
        timeout = int(os.getenv("MSSQL_CONNECTION_TIMEOUT", "30"))
        multi_subnet = os.getenv("MSSQL_MULTI_SUBNET_FAILOVER", "no").lower() in ('yes', 'true', '1')
        
        # Validate configuration
        if not trusted_connection and not all([user, password]):
            raise ValueError(
                "MSSQL_USER and MSSQL_PASSWORD are required when not using trusted connection. "
                "Set MSSQL_TRUSTED_CONNECTION=yes for Windows authentication."
            )
        
        # Build configuration dictionary
        config = {
            "driver": driver,
            "server": server,
            "port": port,
            "database": database,
            "trusted_connection": trusted_connection,
            "trust_server_certificate": trust_cert,
            "encrypt": encrypt,
            "timeout": timeout,
            "multi_subnet_failover": multi_subnet
        }
        
        if not trusted_connection:
            config["user"] = user
            config["password"] = password
        
        # Build connection string
        conn_parts = [
            f"Driver={{{driver}}}",
            f"Server={server},{port}" if port != "1433" else f"Server={server}",
            f"Database={database}",
            f"TrustServerCertificate={'yes' if trust_cert else 'no'}",
            f"Encrypt={'yes' if encrypt else 'no'}",
            f"Connection Timeout={timeout}",
            f"MultiSubnetFailover={'yes' if multi_subnet else 'no'}"
        ]
        
        if trusted_connection:
            conn_parts.append("Trusted_Connection=yes")
        else:
            conn_parts.extend([
                f"UID={user}",
                f"PWD={password}"
            ])
        
        connection_string = ";".join(conn_parts) + ";"
        
        # Log configuration (without password)
        safe_config = config.copy()
        if "password" in safe_config:
            safe_config["password"] = "***"
        logger.info(f"Database configuration: {safe_config}")
        
        return config, connection_string


# ---------------------------------------------------------------------------
# Server profiles — named environments reachable from execute_sql(server=...).
# DEV is the .env default. Non-DEV profiles are READ-ONLY unless allow_write=true
# (TESTGRODNO writes unlocked 2026-07-16 — decyzja jmk, kopia PROD do testów).
# Passwords come from env (.env), never inline.
# ---------------------------------------------------------------------------

SERVER_PROFILES: Dict[str, Dict[str, Any]] = {
    # name: {server, database, user_env/user, password_env, hard_readonly}
    "PROD": {
        "server": "cs-sql03",
        "database": "cs04",
        "user": "adminjmk",
        "password_env": "CSPROD_PWD",
        "hard_readonly": False,
        "hint": "PROD Grodno (cs-sql03/cs04). Zmiany danych tylko przez pakiety csSysChanges!",
    },
    "PLAY": {
        "server": None,  # None = ten sam serwer co DEV
        "database": "csPlay",
        "user": None,    # None = creds DEV
        "password_env": None,
        "hard_readonly": False,
        "hint": "Baza csPlay (projekt csNuxtPlay) na serwerze DEV.",
    },
    "LOT": {
        "server": None,
        "database": "csLot",
        "user": None,
        "password_env": None,
        "hard_readonly": False,
        "hint": "Baza csLot (projekt csNuxtLot) na serwerze DEV.",
    },
    "CSSQL01": {
        "server": r"cs-sql01\cs",
        "database": "cs",
        "user_env": "CSSQL01_USER",
        "user": "adminjmk",  # fallback gdy CSSQL01_USER nieustawiony
        "password_env": "CSSQL01_PWD",
        "hard_readonly": False,
        "hint": "cs-sql01\\cs (czas pracy). CustomerDesc/ProjectDesc, nie Desc_PL.",
    },
    "SAVPOL": {
        "server": r"CS-SQL02\SAVPOL",
        "database": "cs06",
        "user": "adminjmk",
        "password_env": "CSSAVPOL_PWD",
        "hard_readonly": False,
        "hint": "Środowisko klienta Savpol. Hasło podaje user per-sesja (env CSSAVPOL_PWD).",
    },
    "TESTGRODNO": {
        "server": r"CS-BCKP01\GRODNO",
        "database": "test04",
        # ten sam serwer co SLGRODNO — jeśli nie ma osobnych creds, bierz tamte
        "user_env": ("CSTESTGRODNO_USER", "CSSLGRODNO_USER"),
        "password_env": ("CSTESTGRODNO_PWD", "CSSLGRODNO_PWD"),
        "hard_readonly": False,
        "hint": "Baza testowa Grodno (kopia PROD). Zapis wymaga allow_write=true (odblokowane 2026-07-16 dla testów wyszukiwarki).",
    },
    "CERES_TEST": {
        "server": "CERES_TEST",
        "database": "test13",
        "user_env": "CSCERESTEST_USER",
        "password_env": "CSCERESTEST_PWD",
        "hard_readonly": False,
        "hint": "Ceres TEST (CERTUSOFT-SQL-T/test13) — klient Ceres, projekt VueCeres. Zapis wymaga allow_write=true.",
    },
    "SLGRODNO": {
        "server": r"CS-BCKP01\GRODNO",
        "database": "sl_grodno",
        # ten sam serwer co TESTGRODNO — jeśli nie ma osobnych creds, bierz tamte
        "user_env": ("CSSLGRODNO_USER", "CSTESTGRODNO_USER"),
        "password_env": ("CSSLGRODNO_PWD", "CSTESTGRODNO_PWD"),
        "hard_readonly": False,
        "hint": "Baza sl_grodno na CS-BCKP01\\GRODNO — POZA modelem cs* (brak <T>JSONSave/csSysChanges). Zapis wymaga allow_write=true.",
    },
}

_WRITE_TOKEN_RE = re.compile(
    r"\b(insert|update|delete|merge|truncate|alter|create|drop|grant|revoke|exec|execute)\b",
    re.IGNORECASE,
)


def _strip_sql_literals_and_comments(sql: str) -> str:
    """Remove '...' literals, -- comments and /* */ blocks so the write-guard
    does not trip on keywords inside strings/comments."""
    out: List[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            out.append(" ")
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def find_write_token(sql: str) -> Optional[str]:
    """Return the first write-capable keyword outside strings/comments, or None."""
    m = _WRITE_TOKEN_RE.search(_strip_sql_literals_and_comments(sql))
    return m.group(1).lower() if m else None


# Guard: create/alter of a MANAGED cs* code object outside the csAddObjVer framework.
# Searched on the RAW query (including string literals) — catches also the
# sp_executesql/replace('create procedure','alter procedure') hotfix path that
# caused the csPlanningPolicyProposalsCompute incident (registry carried the stale
# body and the upgrade package shipped the bug to PROD).
_MANAGED_DDL_RE = re.compile(
    r"(?is)\b(create|alter)\s+(procedure|proc|function|view|trigger)\s+(\[?dbo\]?\.)?\[?cs\w+"
)

# cs tools that accept a `server` argument (resolved here, transparent for the tool).
MULTI_SERVER_TOOLS = {
    "describe", "sql_grep", "get_cs_object_versions", "rebuild_user_rights",
    "register_job", "rag_get_sql_object", "deploy_sql_object",
    "ng_replicate_window", "ng_ensure_privileges", "help_upsert_topic", "ng_preview_dataset",
}

# tools that additionally need the DEV connection (cross-server: DEV = source of truth)
CROSS_SERVER_TOOLS = {"deploy_sql_object", "ng_replicate_window"}


def check_managed_ddl(query: str) -> Optional[str]:
    """Return an error message when the query patches a managed cs* object outside
    the versioning framework (no csAddObjVer in the same script)."""
    if not _MANAGED_DDL_RE.search(query):
        return None
    low = query.lower()
    if "csaddobjver" in low or "cssysdropobjectforcreate" in low:
        return None
    return (
        "Error: query contains CREATE/ALTER of a managed cs* object WITHOUT csAddObjVer. "
        "A raw ALTER leaves the STALE body in csSysObjVer and the next upgrade package "
        "ships the bug (incydent csPlanningPolicyProposalsCompute 2026-07-16). "
        "Use the deploy_sql_object tool (it handles @pv/@v/3-batch; server=PROD for "
        "cross-env with a consistent chain). If this is intentional raw DDL "
        "(e.g. a temp/unmanaged object), pass allow_raw_ddl=true."
    )


def _reload_env_file() -> None:
    """Re-read mssql-mcp-server/.env into os.environ (setdefault — existing values win).
    Per-session credentials (e.g. CSSAVPOL_PWD) can be appended to .env while the
    server is running; without this, only a process restart would pick them up."""
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    if not os.path.exists(env_path):
        return
    # utf-8-sig: BOM w .env zamieniłby pierwszy klucz na "﻿MSSQL_SERVER" i cicho go zgubił
    with open(env_path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _getenv_reloading(name: str) -> Optional[str]:
    """os.getenv with a one-shot .env re-read fallback when the variable is missing."""
    val = os.getenv(name)
    if not val:
        _reload_env_file()
        val = os.getenv(name)
    return val


def _env_name_list(spec) -> List[str]:
    """Profile creds may name one env var or a fallback chain (tuple/list)."""
    return [spec] if isinstance(spec, str) else list(spec)


def _first_env(spec) -> Optional[str]:
    """First non-empty value from the profile's env-var chain."""
    for name in _env_name_list(spec):
        val = _getenv_reloading(name)
        if val:
            return val
    return None


def _env_names(spec) -> str:
    return " / ".join(_env_name_list(spec))


def resolve_profile_connection(profile_name: str) -> Tuple[str, str]:
    """Build (connection_string, label) for a named server profile.
    Raises ValueError with an actionable message when creds are missing."""
    prof = SERVER_PROFILES.get(profile_name)
    if prof is None:
        raise ValueError(
            f"Unknown server profile '{profile_name}'. "
            f"Available: DEV, {', '.join(sorted(SERVER_PROFILES))}."
        )
    dev_config, _ = DatabaseConfig.get_config()
    server = prof["server"] or dev_config["server"]
    database = prof["database"]
    if prof.get("user_env"):
        # env wygrywa nad wpisem w profilu; brak obu = błąd z podpowiedzią, którą zmienną ustawić
        user = _first_env(prof["user_env"]) or prof.get("user")
        if not user:
            raise ValueError(
                f"Profile {profile_name}: set env {_env_names(prof['user_env'])} (user)."
            )
    else:
        user = prof["user"] or os.getenv("MSSQL_USER")
    if prof.get("password_env"):
        password = _first_env(prof["password_env"])
        if not password:
            raise ValueError(
                f"Profile {profile_name}: missing env {_env_names(prof['password_env'])} (password). "
                f"Poproś użytkownika o hasło / ustaw w mssql-mcp-server/.env."
            )
    else:
        password = os.getenv("MSSQL_PASSWORD")
    driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
    conn = (
        f"Driver={{{driver}}};Server={server};Database={database};"
        f"UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;"
    )
    return conn, f"{server}/{database}"


# Tabele referowane w zapytaniu — do auto-hinta po "Invalid column name".
_INVALID_COL_TABLE_RE = re.compile(
    r"(?:\bfrom\b|\bjoin\b|\bupdate\b|\binto\b)\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_][A-Za-z0-9_]*)\]?",
    re.IGNORECASE,
)
_INVALID_COL_SKIP = {"openjson", "string_split", "values", "select", "sys"}


class SQLExecutor:
    """Handles SQL query execution"""

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.preprocessor = QueryPreprocessor()

    def _enrich_invalid_column_error(self, query: str, error_msg: str) -> str:
        """Po 'Invalid column name' doklej realne kolumny tabel z zapytania.

        Oszczędza rundę describe/ponownego zgadywania: agent dostaje listę kolumn
        w tej samej odpowiedzi, w której dostał błąd. Best-effort — każdy problem
        z metadanymi zwraca oryginalny błąd bez zmian.
        """
        if "Invalid column name" not in error_msg:
            return error_msg
        names: List[str] = []
        seen = set()
        for m in _INVALID_COL_TABLE_RE.finditer(query):
            t = m.group(1)
            tl = t.lower()
            if tl in seen or tl in _INVALID_COL_SKIP:
                continue
            seen.add(tl)
            names.append(t)
            if len(names) >= 5:
                break
        if not names:
            return error_msg
        lines: List[str] = []
        try:
            with connect(self.connection_string) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    for t in names:
                        cur.execute(
                            "select name from sys.columns "
                            "where object_id = object_id(N'dbo.' + ?) order by column_id",
                            t,
                        )
                        cols = [r[0] for r in cur.fetchall()]
                        if not cols:
                            continue
                        shown = ", ".join(cols[:80]) + (", ..." if len(cols) > 80 else "")
                        lines.append(f"--   dbo.{t}: {shown}")
        except Exception:
            return error_msg
        if not lines:
            return error_msg
        return (
            error_msg
            + "\n-- auto-hint (Invalid column name) — realne kolumny tabel z zapytania: --\n"
            + "\n".join(lines)
        )
    
    def execute_query(self, query: str, _retried: bool = False) -> Tuple[bool, List[str], Optional[str]]:
        """
        Execute a SQL query and return results.

        Args:
            query: SQL query to execute

        Returns:
            Tuple of (success, results, error_message)
        """
        try:
            # Preprocessing wyłączone — pyodbc/SQL Server obsługuje newline, taby,
            # GO-batche (rozbijamy sami poniżej) i komentarze natywnie.
            # Stary QueryPreprocessor gubił newline'y, zwijał spacje i obcinał wszystko po pierwszym GO.
            processed_query = query

            # Rozbij na batche po linii zawierającej tylko `GO` (konwencja sqlcmd).
            # Opcjonalna liczba powtórzeń `GO N` jest honorowana.
            batches: List[Tuple[str, int]] = []
            current: List[str] = []
            go_re = re.compile(r'^\s*GO\s*(\d+)?\s*(?:--.*)?$', re.IGNORECASE)
            for line in processed_query.splitlines():
                m = go_re.match(line)
                if m:
                    text = "\n".join(current).strip()
                    if text:
                        batches.append((text, int(m.group(1)) if m.group(1) else 1))
                    current = []
                else:
                    current.append(line)
            tail = "\n".join(current).strip()
            if tail:
                batches.append((tail, 1))
            if not batches:
                return True, ["(empty query)"], None

            aggregated: List[str] = []
            with connect(self.connection_string) as conn:
                conn.autocommit = True  # nie otwieraj niejawnej transakcji
                # PROD (cs-sql03) zwraca krzaki bez jawnego dekodowania; na DEV neutralne.
                try:
                    import pyodbc as _pyodbc
                    conn.setdecoding(_pyodbc.SQL_CHAR, encoding="cp1250")
                    conn.setdecoding(_pyodbc.SQL_WCHAR, encoding="utf-16-le")
                except Exception:
                    pass
                with conn.cursor() as cursor:
                    for idx, (batch_sql, repeat) in enumerate(batches, start=1):
                        for _ in range(repeat):
                            batch_upper = batch_sql.lstrip().upper()
                            # Backward-compatible alias used by some clients.
                            if batch_upper == "SHOW TABLES":
                                cursor.execute(batch_sql)
                                ok, rows, err = self._handle_show_tables(cursor, conn)
                                if not ok:
                                    return False, aggregated, err
                                aggregated.extend(rows)
                                continue

                            # Wyczyść ewentualne otwarte transakcje pozostawione
                            # przez poprzedni batch/zapytanie zanim wykonamy bieżący.
                            cursor.execute("while @@trancount > 0 rollback tran")
                            cursor.execute(batch_sql)
                            if len(batches) > 1:
                                aggregated.append(f"-- batch {idx} --")

                            # Iterate every result set produced by the batch. A single
                            # batch may emit multiple rowsets (multiple SELECTs, procs
                            # returning intermediate result sets, etc.). The previous
                            # implementation only looked at cursor.description when the
                            # batch text started with SELECT/WITH, so DECLARE/EXEC/SET
                            # batches silently dropped every SELECT in the script and
                            # only reported "Rows affected: 0".
                            result_set_idx = 0
                            had_any_rowset = False
                            total_rows_affected = 0
                            while True:
                                if cursor.description:
                                    had_any_rowset = True
                                    result_set_idx += 1
                                    if result_set_idx > 1:
                                        aggregated.append(f"-- result set {result_set_idx} --")
                                    ok, rows, err = self._handle_select_query(cursor)
                                    if not ok:
                                        return False, aggregated, err
                                    aggregated.extend(rows)
                                else:
                                    rc = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
                                    total_rows_affected += rc
                                try:
                                    if not cursor.nextset():
                                        break
                                except PyODBCError:
                                    # Some drivers raise after the last result set has
                                    # been consumed — treat as end of batch.
                                    break
                            if not had_any_rowset:
                                aggregated.append(f"Query executed successfully. Rows affected: {total_rows_affected}")
                            conn.commit()
                    return True, aggregated, None

        except PyODBCError as e:
            error_msg = str(e)
            # 2801 = "The definition of object ... has changed since it was compiled"
            # — stale plan right after a redeploy; a single retry always fixes it.
            if not _retried and "2801" in error_msg:
                logger.warning("Error 2801 (definition changed since compiled) — retrying once.")
                return self.execute_query(query, _retried=True)
            logger.error(f"Database error executing query: {error_msg}")
            return False, [], self._enrich_invalid_column_error(query, error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return False, [], error_msg
    
    def _handle_select_query(self, cursor) -> Tuple[bool, List[str], Optional[str]]:
        """Handle SELECT query results"""
        try:
            # Get column names
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            
            # Fetch all rows
            rows = cursor.fetchall()
            
            # Format results
            results = []
            if columns:
                # Header row
                results.append("|".join(columns))
                results.append("|".join(["-" * len(col) for col in columns]))
                
                # Data rows
                for row in rows:
                    formatted_row = []
                    for value in row:
                        if value is None:
                            formatted_row.append("NULL")
                        else:
                            formatted_row.append(str(value))
                    results.append("|".join(formatted_row))
            else:
                results.append("Query returned no columns")
            
            # Add row count
            results.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''} affected)")
            
            return True, results, None
            
        except Exception as e:
            return False, [], f"Error processing query results: {str(e)}"
    
    def _handle_show_tables(self, cursor, conn) -> Tuple[bool, List[str], Optional[str]]:
        """Handle SHOW TABLES command (MySQL compatibility)"""
        try:
            # Get database name
            db_config, _ = DatabaseConfig.get_config()
            database = db_config['database']
            
            # Execute SQL Server equivalent
            cursor.execute("""
                SELECT TABLE_NAME 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_TYPE = 'BASE TABLE' 
                  AND TABLE_CATALOG = ?
                ORDER BY TABLE_NAME
            """, database)
            
            tables = cursor.fetchall()
            
            # Format results
            results = [f"Tables_in_{database}"]
            results.append("-" * len(results[0]))
            results.extend([table[0] for table in tables])
            results.append(f"\n({len(tables)} table{'s' if len(tables) != 1 else ''})")
            
            return True, results, None
            
        except Exception as e:
            return False, [], f"Error listing tables: {str(e)}"


# Initialize MCP server
app = Server("mssql_mcp_server")


@app.list_resource_templates()
async def list_resource_templates() -> List[ResourceTemplate]:
    """Return empty list — no resource templates defined."""
    return []


@app.list_resources()
async def list_resources() -> List[Resource]:
    """List MSSQL tables as resources."""
    try:
        config, connection_string = DatabaseConfig.get_config()
        database = config['database']
        
        with connect(connection_string) as conn:
            with conn.cursor() as cursor:
                # Get all user tables
                cursor.execute("""
                    SELECT 
                        s.name AS schema_name,
                        t.name AS table_name,
                        t.create_date,
                        t.modify_date
                    FROM sys.tables t
                    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                    WHERE t.type = 'U'  -- User tables only
                    ORDER BY s.name, t.name
                """)
                
                tables = cursor.fetchall()
                logger.info(f"Found {len(tables)} tables in database '{database}'")
                
                resources = []
                for schema, table, created, modified in tables:
                    full_table_name = f"{schema}.{table}"
                    safe_name = quote(full_table_name, safe="")
                    resources.append(
                        Resource(
                            uri=f"mssql:///{database}/{safe_name}/schema",
                            name=f"Schema: {full_table_name}",
                            mimeType="application/json",
                            description=f"Schema definition for table {full_table_name}"
                        )
                    )
                    resources.append(
                        Resource(
                            uri=f"mssql:///{database}/{safe_name}/data",
                            name=f"Data: {full_table_name}",
                            mimeType="text/plain",
                            description=f"Sample data from table {full_table_name} (limited to 100 rows)"
                        )
                    )
                
                return resources
                
    except Exception as e:
        logger.error(f"Failed to list resources: {str(e)}")
        return []


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read table schema or data."""
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")
    
    if not uri_str.startswith("mssql://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")
    
    try:
        # Parse URI: mssql://database/schema.table/type
        parts = uri_str[8:].split('/')
        if len(parts) != 3:
            raise ValueError(f"Invalid URI format: {uri_str}")
        
        database, table_full, resource_type = parts
        
        # Split schema.table
        if '.' in table_full:
            schema, table = table_full.split('.', 1)
        else:
            schema = 'dbo'
            table = table_full
        
        config, connection_string = DatabaseConfig.get_config()
        
        with connect(connection_string) as conn:
            with conn.cursor() as cursor:
                if resource_type == 'schema':
                    return await _read_table_schema(cursor, schema, table)
                elif resource_type == 'data':
                    return await _read_table_data(cursor, schema, table)
                else:
                    raise ValueError(f"Unknown resource type: {resource_type}")
                    
    except Exception as e:
        logger.error(f"Error reading resource {uri}: {str(e)}")
        raise RuntimeError(f"Error reading resource: {str(e)}")


async def _read_table_schema(cursor, schema: str, table: str) -> str:
    """Read table schema information."""
    cursor.execute("""
        SELECT 
            c.COLUMN_NAME,
            c.DATA_TYPE,
            c.CHARACTER_MAXIMUM_LENGTH,
            c.NUMERIC_PRECISION,
            c.NUMERIC_SCALE,
            c.IS_NULLABLE,
            c.COLUMN_DEFAULT,
            CASE 
                WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES'
                ELSE 'NO'
            END AS IS_PRIMARY_KEY
        FROM INFORMATION_SCHEMA.COLUMNS c
        LEFT JOIN (
            SELECT ku.TABLE_SCHEMA, ku.TABLE_NAME, ku.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
            INNER JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS ku
                ON tc.CONSTRAINT_TYPE = 'PRIMARY KEY' 
                AND tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
        ) pk ON c.TABLE_SCHEMA = pk.TABLE_SCHEMA 
            AND c.TABLE_NAME = pk.TABLE_NAME 
            AND c.COLUMN_NAME = pk.COLUMN_NAME
        WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
        ORDER BY c.ORDINAL_POSITION
    """, schema, table)
    
    columns = cursor.fetchall()
    
    # Format schema information
    result = [f"Schema for {schema}.{table}:", "=" * 50, ""]
    result.append(f"{'Column':<30} {'Type':<20} {'Nullable':<10} {'PK':<5} {'Default':<20}")
    result.append("-" * 100)
    
    for col in columns:
        name, dtype, char_len, num_prec, num_scale, nullable, default, is_pk = col
        
        # Format data type
        if char_len:
            type_str = f"{dtype}({char_len})"
        elif num_prec and num_scale:
            type_str = f"{dtype}({num_prec},{num_scale})"
        elif num_prec:
            type_str = f"{dtype}({num_prec})"
        else:
            type_str = dtype
        
        # Format default
        default_str = str(default)[:20] if default else ""
        
        result.append(
            f"{name:<30} {type_str:<20} {nullable:<10} {is_pk:<5} {default_str:<20}"
        )
    
    return "\n".join(result)


async def _read_table_data(cursor, schema: str, table: str) -> str:
    """Read sample data from table."""
    # Get row count
    cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
    total_rows = cursor.fetchone()[0]
    
    # Get sample data
    cursor.execute(f"SELECT TOP 100 * FROM [{schema}].[{table}]")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    
    # Format results
    result = [f"Sample data from {schema}.{table} (showing {len(rows)} of {total_rows} rows):", ""]
    
    if rows:
        # Create formatted table
        result.append("|".join(columns))
        result.append("|".join(["-" * len(col) for col in columns]))
        
        for row in rows:
            formatted_row = []
            for value in row:
                if value is None:
                    formatted_row.append("NULL")
                else:
                    str_value = str(value)
                    # Truncate long values
                    if len(str_value) > 50:
                        str_value = str_value[:47] + "..."
                    formatted_row.append(str_value)
            result.append("|".join(formatted_row))
    else:
        result.append("(No data)")
    
    return "\n".join(result)


@app.list_tools()
async def list_tools() -> List[Tool]:
    """List available MSSQL tools."""
    return [
        Tool(
            name="execute_sql",
            description=(
                "Execute an SQL query (REQUIRED param: query — NOT 'sql'). Default target = DEV (from .env). Optional `server` targets a named "
                "profile: PROD (cs-sql03/cs04 — Grodno), PLAY (csPlay), LOT (csLot), CSSQL01 (cs-sql01\\cs — czas pracy), "
                "SAVPOL (CS-SQL02\\SAVPOL/cs06), TESTGRODNO (CS-BCKP01\\GRODNO/test04 — kopia PROD do testów), "
                "CERES_TEST (CERTUSOFT-SQL-T/test13 — klient Ceres), "
                "SLGRODNO (CS-BCKP01\\GRODNO/sl_grodno — baza spoza modelu cs*). "
                "Non-DEV profiles are READ-ONLY by default: insert/update/delete/exec/DDL are rejected unless "
                "allow_write=true. Schema changes on PROD are forbidden regardless (use csSysChanges packages)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    },
                    "server": {
                        "type": "string",
                        "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "SLGRODNO"],
                        "description": "Target environment (default DEV)."
                    },
                    "allow_write": {
                        "type": "boolean",
                        "description": "Required to run write statements (DML/exec/DDL) on a non-DEV profile (also on TESTGRODNO — writes unlocked 2026-07-16)."
                    },
                    "allow_raw_ddl": {
                        "type": "boolean",
                        "description": (
                            "Override for the managed-DDL guard: CREATE/ALTER of a cs* "
                            "procedure/function/view/trigger without csAddObjVer is rejected "
                            "(registry divergence ships bugs in upgrade packages) — prefer "
                            "deploy_sql_object; pass true only for intentional raw DDL."
                        )
                    }
                },
                "required": ["query"]
            }
        ),
        *rag_tools.tool_descriptors(),
        *cs_tools.tool_descriptors(),
        *mail_tools.tool_descriptors(),
    ]


def _resolve_tool_connection(name: str, arguments: dict) -> Tuple[str, str]:
    """Return (connection_string, header) for a cs/rag tool call honouring the
    optional `server` argument (whitelisted tools only)."""
    _, connection_string = DatabaseConfig.get_config()
    header = ""
    profile = (arguments.get("server") or "DEV").upper()
    if profile != "DEV":
        if name not in MULTI_SERVER_TOOLS:
            raise ValueError(
                f"Tool '{name}' does not support server='{profile}' — only: "
                f"{', '.join(sorted(MULTI_SERVER_TOOLS))}."
            )
        target_conn, label = resolve_profile_connection(profile)
        header = f"-- server: {profile} ({label}) --\n"
        if name in CROSS_SERVER_TOOLS:
            # cross-server: target conn + DEV conn (DEV = source of truth)
            arguments["_dev_connection_string"] = connection_string
            arguments["_target_label"] = profile
        connection_string = target_conn
    return connection_string, header


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    """Execute SQL commands."""
    logger.info(f"Calling tool: {name}")

    if name in rag_tools.RAG_TOOL_NAMES:
        try:
            connection_string, header = _resolve_tool_connection(name, arguments or {})
            text = rag_tools.handle_tool(name, arguments or {}, connection_string)
            return [TextContent(type="text", text=header + text)]
        except Exception as e:
            logger.error(f"Error in RAG tool {name}: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {e}")]

    if name in cs_tools.CS_TOOL_NAMES:
        try:
            connection_string, header = _resolve_tool_connection(name, arguments or {})
            text = cs_tools.handle_tool(name, arguments or {}, connection_string)
            return [TextContent(type="text", text=header + text)]
        except Exception as e:
            logger.error(f"Error in cs tool {name}: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {e}")]

    if name in mail_tools.MAIL_TOOL_NAMES:
        try:
            text = mail_tools.handle_tool(name, arguments or {}, "")
            return [TextContent(type="text", text=text)]
        except Exception as e:
            logger.error(f"Error in mail tool {name}: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {e}")]

    if name != "execute_sql":
        raise ValueError(f"Unknown tool: {name}")
    
    query = arguments.get("query")
    if not query:
        raise ValueError("Query parameter is required")

    profile_name = (arguments.get("server") or "DEV").upper()
    allow_write = bool(arguments.get("allow_write"))
    allow_raw_ddl = bool(arguments.get("allow_raw_ddl"))

    # Guard: managed cs* object patched outside the csAddObjVer framework
    # (registry divergence -> upgrade package ships the stale body).
    if not allow_raw_ddl:
        ddl_err = check_managed_ddl(query)
        if ddl_err:
            return [TextContent(type="text", text=ddl_err)]

    # Log query info (truncated for security)
    query_preview = query[:100] + "..." if len(query) > 100 else query
    logger.info(f"Executing query on {profile_name}: {query_preview}")

    try:
        if profile_name == "DEV":
            config, connection_string = DatabaseConfig.get_config()
            label = f"{config['server']}/{config['database']}"
        else:
            prof = SERVER_PROFILES.get(profile_name)
            if prof is None:
                return [TextContent(type="text", text=(
                    f"Error: unknown server profile '{profile_name}'. "
                    f"Available: DEV, {', '.join(sorted(SERVER_PROFILES))}."
                ))]
            token = find_write_token(query)
            if token and prof["hard_readonly"]:
                return [TextContent(type="text", text=(
                    f"Error: profile {profile_name} is ALWAYS read-only (statement contains '{token}'). "
                    f"{prof.get('hint', '')}"
                ))]
            if token and not allow_write:
                return [TextContent(type="text", text=(
                    f"Error: profile {profile_name} is read-only by default and the statement contains "
                    f"'{token}'. Pass allow_write=true ONLY if the write is intended and allowed on this "
                    f"environment. {prof.get('hint', '')}"
                ))]
            connection_string, label = resolve_profile_connection(profile_name)

        executor = SQLExecutor(connection_string)
        success, results, error = executor.execute_query(query)

        header = [] if profile_name == "DEV" else [f"-- server: {profile_name} ({label}) --"]
        if success:
            return [TextContent(type="text", text="\n".join(header + results))]
        else:
            return [TextContent(type="text", text="\n".join(header + [f"Error: {error}"]))]

    except Exception as e:
        logger.error(f"Error in call_tool: {str(e)}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Main entry point to run the MCP server."""
    from mcp.server.stdio import stdio_server
    
    logger.info(f"Starting MSSQL MCP Server v{__version__}")
    
    try:
        # Validate configuration on startup
        config, connection_string = DatabaseConfig.get_config()
        logger.info(f"Connecting to {config['server']}:{config['port']}/{config['database']}")
        
        # Test connection
        with connect(connection_string) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT @@VERSION")
                version = cursor.fetchone()[0]
                logger.info(f"Connected to SQL Server: {version.split('\\n')[0]}")
        
    except Exception as e:
        logger.error(f"Failed to connect to database: {str(e)}")
        sys.exit(1)
    
    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())