"""Wspólne stałe i niskopoziomowe helpery (JSONSave, GUID, batche GO, translaty)."""

from __future__ import annotations

import json
import logging
import re
import uuid

from typing import List, Optional, Sequence

logger = logging.getLogger("mssql_mcp_server.cs")


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Domyślny csAppNameSpacesG dla obiektów systemowych NG.
DEFAULT_NAMESPACE_G = "E4B58826-69B9-4180-8A58-953B13AB2C77"


SCRIPT_CONVERTERS_G = "D107A1E4-0F2F-4D35-BA78-7308C9854044"


# Komplet 12 języków wymaganych przez csSysColumnsJSONSave przy INSERT.
COLUMN_DESC_LANGS = ["EN", "PL", "DE", "FR", "ES", "IT", "NL", "PT", "RU", "UK", "SK", "SE"]


# Języki fizycznie obecne w csNGAppWindowDataSetsFields (lab/col/watermark) — bez SK/SE.
NG_LABEL_LANGS = ["PL", "EN", "DE", "FR", "ES", "NL", "PT", "RU", "UK", "IT"]


# Języki w csNGAppWindowColsGroups.dataFieldColsGroupDesc_* — pełne 12.
NG_COLSGROUP_LANGS = ["PL", "EN", "DE", "FR", "ES", "NL", "PT", "RU", "UK", "IT", "SE", "SK"]


# Kolumny csNGAppWindowDataSets dozwolone w ng_set_dataset_props (bez kluczy,
# stmSQL — dedykowany tool z testem — oraz kolumn cache 'fields').
NG_DATASET_PROPS_WHITELIST = {
    "pageSize", "pagingDisabled", "notUseSort", "addSortInSQLStm", "addPagingInSQLStm",
    "dataFieldIdent4ReadOnly", "sourceKind4ReadOnly", "loadDataImmediate",
    "calcAggregates", "calcAggrsSeparately", "loadDataVirt", "addAdvancedFilters",
    "addLayout", "isForAppWindowDataEmpty", "dataFieldIdentToIdentifyNewRow",
    "doNotSetCurrentRowIndexOnLoad", "crLocData", "addRoutingData", "rowData",
    "pivotData", "hasSessionData", "asyncExports", "addExports",
    "loop4colFields", "loop4rowFields", "stmFunc", "stmSQLPrepare",
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _new_guid() -> str:
    return str(uuid.uuid4()).upper()


def _split_go_batches(sql_text: str) -> List[str]:
    """Split a script on standalone GO lines (cs deploy template is 3-batch)."""
    parts = re.split(r"(?im)^\s*GO\s*$", sql_text)
    return [p.strip() for p in parts if p.strip()]


def _xml_response_to_text(value) -> Optional[str]:
    """csSys*JSONSave @response xml: NULL = success, <messages> = errors."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _exec_scalar(cur, sql: str, *params):
    cur.execute(sql, *params)
    row = cur.fetchone()
    return row[0] if row else None


def _jsonsave(cur, proc_name: str, rows: Sequence[dict]) -> Optional[str]:
    """Call dbo.<proc_name> with parametrized @data; return @response text (None = success)."""
    payload = json.dumps(list(rows), ensure_ascii=False)
    cur.execute(
        f"declare @response xml; "
        f"exec dbo.{proc_name} @data = ?, @response = @response out; "
        f"select convert(nvarchar(max), @response) [response];",
        payload,
    )
    row = cur.fetchone()
    return _xml_response_to_text(row[0] if row else None)


def _as_int(value) -> int:
    """Coerce bool/str to int for JSONSave payloads (bit serialized as true/false breaks int columns)."""
    if isinstance(value, bool):
        return 1 if value else 0
    return int(value)


# ---------------------------------------------------------------------------
# 3b. register_cs_table — full table registration in one call
# ---------------------------------------------------------------------------

def _stable_guid(cur, seed: str) -> str:
    """Deterministic GUID = convert(uniqueidentifier, hashbytes('MD5', seed)) —
    same convention as manual registrations, so re-runs and manual work align."""
    return str(_exec_scalar(
        cur, "select convert(uniqueidentifier, hashbytes('MD5', convert(varchar(500), ?)))", seed
    )).upper()


# ---------------------------------------------------------------------------
# MCP tool descriptors + dispatcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 23. Shared helper — ensure a csTranslate row for given texts (reuse/create)
# ---------------------------------------------------------------------------

def _ensure_translate(cur, texts: dict) -> tuple:
    """Return (csTranslateG, action). Reuse by Content_PL+Content_EN or create."""
    pl = texts.get("PL")
    en = texts.get("EN")
    if not pl:
        raise ValueError("PL text is required to create/reuse a csTranslate row.")
    tg = _exec_scalar(
        cur,
        "select top 1 csTranslateG from dbo.csTranslate with(nolock) "
        "where Content_PL = ? and isnull(Content_EN,N'') = isnull(?,N'')",
        pl, en,
    )
    if tg:
        return str(tg).upper(), "reused"
    tg = _new_guid()
    row = {"_opr": "I", "csTranslateG": tg}
    for lang, val in texts.items():
        if lang in NG_COLSGROUP_LANGS and val:
            row[f"Content_{lang}"] = val
    resp = _jsonsave(cur, "csTranslateJSONSave", [row])
    if resp:
        raise RuntimeError(f"csTranslateJSONSave: {resp}")
    return tg, "created"
