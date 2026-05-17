"""
RAG (Retrieval-Augmented Generation) tools for the MSSQL MCP Server.

Step 1 (keyword-based, no embeddings):
- rag_search_sql        : search across sys.sql_modules + sys.objects
- rag_search_ng_window  : search across csNGAppWindow* metadata
- rag_search_components : grep across .vue / .md files under WORKSPACE_ROOT
- rag_get_sql_object    : full text of a SQL object via dbo.csSysScriptSqlObject
- rag_get_file          : read a file under WORKSPACE_ROOT

Environment:
- RAG_WORKSPACE_ROOT : absolute path to the workspace root used by file-based tools.
                       Defaults to the parent of the MCP server folder.
- RAG_COMPONENT_DIRS : comma-separated list of glob roots scanned by component search.
                       Defaults to "csNuxtComponents,app/configuration/definitions,.github".
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from pyodbc import connect

logger = logging.getLogger("mssql_mcp_server.rag")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_DEFAULT_COMPONENT_DIRS = "csNuxtComponents,app/configuration/definitions,.github"
_MAX_SNIPPET_CHARS = 240
_MAX_FILE_BYTES = 256 * 1024
_VALID_SQL_TYPES = {"P", "FN", "IF", "TF", "V", "TR", "U"}


def workspace_root() -> Path:
    raw = os.getenv("RAG_WORKSPACE_ROOT")
    if raw:
        return Path(raw).resolve()
    # default: parent of mssql-mcp-server folder
    return Path(__file__).resolve().parents[2]


def component_dirs() -> List[str]:
    raw = os.getenv("RAG_COMPONENT_DIRS", _DEFAULT_COMPONENT_DIRS)
    return [d.strip() for d in raw.split(",") if d.strip()]


# ---------------------------------------------------------------------------
# Tokenization / ranking
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def tokenize(query: str) -> List[str]:
    """Extract alpha tokens of length >= 2. Case-insensitive matches downstream."""
    return [t for t in _TOKEN_RE.findall(query or "") if len(t) >= 2]


def score_text(text: str, tokens: Sequence[str]) -> int:
    """Total case-insensitive occurrences of all tokens in text."""
    if not text or not tokens:
        return 0
    lower = text.lower()
    return sum(lower.count(t.lower()) for t in tokens)


def first_hit_snippet(text: str, tokens: Sequence[str]) -> str:
    """Return a short snippet around the first token hit, single-line."""
    if not text:
        return ""
    lower = text.lower()
    pos = -1
    for t in tokens:
        i = lower.find(t.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        snippet = text[:_MAX_SNIPPET_CHARS]
    else:
        start = max(0, pos - 80)
        end = min(len(text), pos + _MAX_SNIPPET_CHARS - 80)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
    return " ".join(snippet.split())


# ---------------------------------------------------------------------------
# SQL search
# ---------------------------------------------------------------------------

@dataclass
class SqlHit:
    schema: str
    name: str
    type: str
    score: int
    snippet: str

    def format(self) -> str:
        return f"[{self.type}] {self.schema}.{self.name}  (score={self.score})\n    {self.snippet}"


def search_sql_modules(
    connection_string: str,
    query: str,
    kinds: Optional[Sequence[str]] = None,
    top: int = 8,
) -> List[SqlHit]:
    tokens = tokenize(query)
    if not tokens:
        return []

    types = [k.upper() for k in (kinds or []) if k.upper() in _VALID_SQL_TYPES] or [
        "P", "FN", "IF", "TF", "V", "TR"
    ]

    # SQL Server: ascii() is byte-cost cheap. We do a pre-filter by LIKE for the
    # FIRST token (best selectivity) and post-rank in Python over the rest.
    primary = tokens[0]

    sql = """
        select
            s.name        as schema_name,
            o.name        as object_name,
            o.type        as object_type,
            isnull(m.definition, N'') as definition
        from sys.objects o
        join sys.schemas s on s.schema_id = o.schema_id
        left join sys.sql_modules m on m.object_id = o.object_id
        where o.is_ms_shipped = 0
          and o.type in ({type_list})
          and (
              o.name like ? escape N'\\'
              or m.definition like ? escape N'\\'
          )
    """.format(type_list=",".join(f"'{t}'" for t in types))

    like_term = "%" + _escape_like(primary) + "%"

    hits: List[SqlHit] = []
    with connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, like_term, like_term)
            for row in cur.fetchall():
                schema, name, otype, definition = row
                # Score against name (weighted) + definition body
                name_score = score_text(name, tokens) * 5
                body_score = score_text(definition, tokens)
                total = name_score + body_score
                if total <= 0:
                    continue
                snippet = first_hit_snippet(definition or name, tokens)
                hits.append(
                    SqlHit(
                        schema=schema,
                        name=name,
                        type=otype.strip(),
                        score=total,
                        snippet=snippet,
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")


# ---------------------------------------------------------------------------
# NG window search
# ---------------------------------------------------------------------------

@dataclass
class NgWindowHit:
    window_ident: str
    window_g: str
    score: int
    snippet: str

    def format(self) -> str:
        return f"[NG] {self.window_ident}  (score={self.score}, G={self.window_g})\n    {self.snippet}"


def search_ng_windows(
    connection_string: str,
    query: str,
    top: int = 8,
) -> List[NgWindowHit]:
    tokens = tokenize(query)
    if not tokens:
        return []

    primary = tokens[0]
    like_term = "%" + _escape_like(primary) + "%"

    # csNGAppWindows.dataSets holds the aggregated JSON of the whole window
    # (datasets, fields, actions, layouts, lookups) — searching it is enough.
    # Read-only access, no modifications.
    sql = """
        select
            w.csNGAppWindowsG                  as window_g,
            w.appWindowIdent                   as window_ident,
            isnull(w.appWindowDesc_PL, N'')    as desc_pl,
            isnull(w.appWindowDesc_EN, N'')    as desc_en,
            isnull(w.dataSets, N'')            as data_sets
        from dbo.csNGAppWindows w
        where w.appWindowIdent             like ? escape N'\\'
           or isnull(w.appWindowDesc_PL, N'') like ? escape N'\\'
           or isnull(w.appWindowDesc_EN, N'') like ? escape N'\\'
           or isnull(w.dataSets, N'')         like ? escape N'\\'
    """

    hits: List[NgWindowHit] = []
    with connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, like_term, like_term, like_term, like_term)
            for row in cur.fetchall():
                window_g, window_ident, desc_pl, desc_en, data_sets = row
                ident_score = score_text(window_ident or "", tokens) * 10
                desc_score = score_text(f"{desc_pl} {desc_en}", tokens) * 3
                body_score = score_text(data_sets or "", tokens)
                total = ident_score + desc_score + body_score
                if total <= 0:
                    continue
                # Prefer snippet from the JSON body (most informative), else ident/desc
                if body_score > 0:
                    snippet = first_hit_snippet(data_sets, tokens)
                else:
                    snippet = first_hit_snippet(
                        f"{window_ident} | {desc_pl} | {desc_en}", tokens
                    )
                hits.append(
                    NgWindowHit(
                        window_ident=window_ident or "",
                        window_g=str(window_g),
                        score=total,
                        snippet=snippet,
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top]


# ---------------------------------------------------------------------------
# Component (file) search
# ---------------------------------------------------------------------------

@dataclass
class FileHit:
    relpath: str
    score: int
    line_no: int
    snippet: str

    def format(self) -> str:
        return f"[FILE] {self.relpath}:{self.line_no}  (score={self.score})\n    {self.snippet}"


_DEFAULT_EXTS = {".vue", ".ts", ".js", ".mjs", ".cjs", ".scss", ".md", ".sql"}
_SKIP_DIRS = {"node_modules", ".git", "dist", ".nuxt", ".output", "bin", "obj"}


def search_files(
    query: str,
    roots: Optional[Sequence[str]] = None,
    exts: Optional[Sequence[str]] = None,
    top: int = 10,
) -> List[FileHit]:
    tokens = tokenize(query)
    if not tokens:
        return []

    base = workspace_root()
    use_roots = list(roots) if roots else component_dirs()
    use_exts = set(e.lower() for e in (exts or _DEFAULT_EXTS))

    candidates: List[FileHit] = []
    for rel_root in use_roots:
        root_path = (base / rel_root).resolve()
        if not _is_within(root_path, base) or not root_path.exists():
            continue
        for path in _iter_files(root_path, use_exts):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            total = score_text(text, tokens)
            if total <= 0:
                continue
            line_no, snippet = _first_line_hit(text, tokens)
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = str(path)
            candidates.append(
                FileHit(relpath=rel, score=total, line_no=line_no, snippet=snippet)
            )

    candidates.sort(key=lambda h: h.score, reverse=True)
    return candidates[:top]


def _iter_files(root: Path, exts: set) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts:
                yield Path(dirpath) / fn


def _first_line_hit(text: str, tokens: Sequence[str]) -> Tuple[int, str]:
    lower_tokens = [t.lower() for t in tokens]
    for i, line in enumerate(text.splitlines(), start=1):
        l = line.lower()
        if any(t in l for t in lower_tokens):
            snippet = line.strip()
            if len(snippet) > _MAX_SNIPPET_CHARS:
                snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."
            return i, snippet
    return 1, ""


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Full-text getters
# ---------------------------------------------------------------------------

def get_sql_object(connection_string: str, object_name: str) -> str:
    """Return the full text of a SQL object via dbo.csSysScriptSqlObject.

    Falls back to sys.sql_modules.definition when the helper is unavailable.
    """
    if not object_name or not re.fullmatch(r"[A-Za-z0-9_.\[\]]+", object_name):
        raise ValueError("Invalid object_name")

    with connect(connection_string) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "exec dbo.csSysScriptSqlObject @object_name = ?",
                    object_name,
                )
                rows = cur.fetchall()
                if rows:
                    # Concatenate first column of every row
                    return "\n".join(
                        (r[0] if r[0] is not None else "") for r in rows
                    )
            except Exception as ex:
                logger.warning("csSysScriptSqlObject failed for %s: %s", object_name, ex)

            # Fallback
            cur.execute(
                """
                select isnull(m.definition, N'')
                from sys.objects o
                join sys.schemas s on s.schema_id = o.schema_id
                left join sys.sql_modules m on m.object_id = o.object_id
                where (s.name + N'.' + o.name) = ? or o.name = ?
                """,
                object_name,
                object_name,
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else ""


def get_file(relpath: str) -> str:
    if not relpath:
        raise ValueError("relpath is required")
    base = workspace_root()
    target = (base / relpath).resolve()
    if not _is_within(target, base):
        raise ValueError("Path escapes workspace root")
    if not target.is_file():
        raise FileNotFoundError(relpath)
    size = target.stat().st_size
    if size > _MAX_FILE_BYTES:
        # Truncate to keep responses bounded
        with target.open("rb") as fh:
            data = fh.read(_MAX_FILE_BYTES)
        return data.decode("utf-8", errors="ignore") + f"\n\n[... truncated, file size={size} bytes ...]"
    return target.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# MCP tool descriptors
# ---------------------------------------------------------------------------

def tool_descriptors():
    """Return Tool() objects for the MCP server to advertise."""
    from mcp.types import Tool

    return [
        Tool(
            name="rag_search_sql",
            description=(
                "Keyword search over SQL Server objects (procedures, functions, views, "
                "triggers, tables) in the configured database. Returns top matches with "
                "schema, name, type, score and snippet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search text"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by object types: P, FN, IF, TF, V, TR, U",
                    },
                    "top": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="rag_search_ng_window",
            description=(
                "Keyword search over NG windows. Scans csNGAppWindows.appWindowIdent, "
                "appWindowDesc_PL/EN and the aggregated dataSets JSON (which contains "
                "all datasets, fields, actions, layouts and lookup definitions). "
                "Read-only — does not modify dataSets/viewHTML."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="rag_search_components",
            description=(
                "Keyword search over Vue components, instructions and docs under the "
                "workspace root. Default roots: csNuxtComponents, app/configuration/definitions, .github."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "roots": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Override search roots (relative to workspace).",
                    },
                    "exts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Override file extensions (e.g. '.vue').",
                    },
                    "top": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="rag_get_sql_object",
            description=(
                "Return the full text of a SQL object via dbo.csSysScriptSqlObject "
                "(fallback to sys.sql_modules). Use after rag_search_sql."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {
                        "type": "string",
                        "description": "Object name, e.g. 'dbo.csCustomersJSONSave' or just 'csCustomersJSONSave'.",
                    },
                },
                "required": ["object_name"],
            },
        ),
        Tool(
            name="rag_get_file",
            description="Return the content of a file under the workspace root (read-only).",
            inputSchema={
                "type": "object",
                "properties": {
                    "relpath": {"type": "string", "description": "Workspace-relative path"},
                },
                "required": ["relpath"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

RAG_TOOL_NAMES = {
    "rag_search_sql",
    "rag_search_ng_window",
    "rag_search_components",
    "rag_get_sql_object",
    "rag_get_file",
}


def handle_tool(name: str, arguments: dict, connection_string: str) -> str:
    """Dispatch a RAG tool call. Returns a single text payload."""
    arguments = arguments or {}

    if name == "rag_search_sql":
        hits = search_sql_modules(
            connection_string,
            query=arguments.get("query", ""),
            kinds=arguments.get("kinds"),
            top=int(arguments.get("top") or 8),
        )
        if not hits:
            return "(no matches)"
        return "\n\n".join(h.format() for h in hits)

    if name == "rag_search_ng_window":
        hits = search_ng_windows(
            connection_string,
            query=arguments.get("query", ""),
            top=int(arguments.get("top") or 8),
        )
        if not hits:
            return "(no matches)"
        return "\n\n".join(h.format() for h in hits)

    if name == "rag_search_components":
        hits = search_files(
            query=arguments.get("query", ""),
            roots=arguments.get("roots"),
            exts=arguments.get("exts"),
            top=int(arguments.get("top") or 10),
        )
        if not hits:
            return "(no matches)"
        return "\n\n".join(h.format() for h in hits)

    if name == "rag_get_sql_object":
        text = get_sql_object(connection_string, arguments.get("object_name", ""))
        return text or "(empty)"

    if name == "rag_get_file":
        return get_file(arguments.get("relpath", ""))

    raise ValueError(f"Unknown RAG tool: {name}")
