"""
cs_tools — write/automation tools for the cs* framework (MSSQL MCP Server).

These tools encapsulate the implicit rules of the cs* framework that otherwise
require an agent to remember 5-10 niche conventions per operation. Each tool
enforces those rules programmatically (parametrized @data, no `dbo.` prefix in
objectName, unique @v, latest @pv, full 12-language ColumnDesc, both rebuild
params, etc.).

Tools:
- deploy_sql_object   : deploy a procedure/function/view/trigger via csAddObjVer
                        (auto @pv=latest, fresh unique @v, 3-batch split, orphan cleanup).
- cs_jsonsave         : call any <T>JSONSave with parametrized @data (safe escaping),
                        parse @response xml into a readable result.
- add_cs_column       : add a column to a cs* table (csSysColumnsJSONSave + rebuild),
                        auto ColumnOrder, full 12x ColumnDesc, both rebuild params.
- add_ng_field        : add a field to an NG window (Fields + LayoutCols + optional
                        ins/upd viewHTML injection).
- get_cs_object_versions : list csSysObjVer history + inProgress state for an object.
- ng_get_window_config : read-only dump of a full NG window configuration
                        (datasets, fields, layout cols, groups, actions, lookups...).
- ng_set_field_labels : update field/whereField labels (lab/col/watermark) with the
                        formatType pitfall handled (silent reset to 'string' otherwise).
- ng_set_layout_col   : upsert grid layout col props (width/isVisible/ord/colsGroupIdent)
                        via minimal-U (no natural key -> no label re-validation trap).
- ng_upsert_cols_group : upsert a grid column group (csNGAppWindowColsGroups).
- ng_set_stmsql       : replace a dataset stmSQL with a MANDATORY sp_executesql test
                        BEFORE saving (with and without dates).
- ng_set_dataset_props : update whitelisted csNGAppWindowDataSets columns (pageSize,
                        pagingDisabled, getMetaInfo-like toggles) via minimal-U.
- rebuild_user_rights : rebuild per-user cache (appMainMenuJSON, appWindowIdentsWithRights,
                        warehousesRights) after menu/window/privileges changes.
- ai_tool_sync_params : sync csAIAgentsToolsParams with an AI tool procedure (upsert with
                        the U-required-fields gotcha, heuristic $.key diff, redeploy script).
- ng_add_lookup       : wire a lookup on an NG field (LookupDefs + Get/Set mappings with
                        all sourceKind conventions).

All tools are WRITE-CAPABLE. They run against the same connection_string as the
read tools. Destructive safety: deploy_sql_object never drops unless csAddObjVer
returns @drop=1; orphan cleanup only flips inProgress=0 (never DELETE).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import List, Optional, Sequence

from pyodbc import connect

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
# 1. deploy_sql_object
# ---------------------------------------------------------------------------

def deploy_sql_object(
    connection_string: str,
    object_name: str,
    body: str,
    description: str,
    object_type: str = "procedure",
) -> str:
    """
    Deploy a SQL object through csAddObjVer with all version-mechanism pitfalls handled:
      - objectName WITHOUT dbo. prefix (HARD RULE).
      - @pv = latest registered verG (or NULL for a brand-new object).
      - @v = freshly generated, collision-checked GUID.
      - 3-batch deploy (csAddObjVer + DDL body + csSysRestoreObject), no GO sent to driver.
      - orphan cleanup: clears a stuck inProgress=1 row from a prior failed attempt (UPDATE only).

    `body` must be the CREATE statement only (e.g. "CREATE procedure dbo.<Name> (...) as begin ... end;").
    Do NOT include csAddObjVer / GO / csSysRestoreObject — they are generated here.
    """
    name = object_name.strip()
    if name.lower().startswith("dbo."):
        name = name[4:]
    if "." in name:
        return f"Error: object_name must be without schema prefix (got '{object_name}')."
    if not body or not re.search(r"(?i)\bcreate\b", body):
        return "Error: body must contain a CREATE statement."
    if not description or len(description) < 3 or description == "ADD_VERSION_DESC_HERE":
        return "Error: description is required (>= 3 chars, not the placeholder)."

    full_name = f"dbo.{name}"
    log: List[str] = []

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            # a) latest verG -> @pv
            latest_ver = _exec_scalar(
                cur,
                "select top 1 v.verG from dbo.csSysObjVer v with(nolock) "
                "where v.objectName = ? order by v.verId desc",
                name,
            )
            pv = str(latest_ver).upper() if latest_ver else None
            log.append(f"@pv = {pv or 'NULL (new object)'}")

            # b) orphan cleanup: clear stuck inProgress=1 (failed prior attempt)
            cur.execute(
                "update dbo.csSysObjVer set inProgress = 0 "
                "where objectName in (?, ?) and inProgress = 1",
                name, full_name,
            )
            if cur.rowcount:
                log.append(f"cleared {cur.rowcount} orphan inProgress row(s)")

            # c) fresh unique @v (collision-checked against csSysObjVer.verG)
            for _ in range(10):
                v = _new_guid()
                exists = _exec_scalar(
                    cur, "select 1 from dbo.csSysObjVer where verG = ?", v
                )
                if not exists:
                    break
            else:
                return "Error: could not generate a unique @v after 10 tries."
            log.append(f"@v = {v}")

            # d) batch 1: csAddObjVer + optional drop
            pv_sql = f"'{pv}'" if pv else "NULL"
            batch1 = (
                "declare @do_drop bit = 0, @r int = 0;\n"
                f"exec @r = dbo.csAddObjVer @n = N'{name}', "
                f"@dsc = N'{description.replace(chr(39), chr(39)*2)}', "
                f"@pv = {pv_sql}, @v = '{v}', "
                f"@csScriptConvertersG = '{SCRIPT_CONVERTERS_G}', @drop = @do_drop out;\n"
                f"if(@do_drop = 1) exec dbo.csSysDropObjectForCreate N'{name}';\n"
                "select @r addobjver_stat, @do_drop do_drop;"
            )
            cur.execute(batch1)
            row = cur.fetchone()
            addobjver_stat = row[0] if row else None
            do_drop = row[1] if row else None
            log.append(f"csAddObjVer stat={addobjver_stat} drop={do_drop}")
            if addobjver_stat is not None and int(addobjver_stat) < 0:
                return "DEPLOY FAILED at csAddObjVer (stat<0). Log:\n  " + "\n  ".join(log)

            # e) batch 2: the CREATE body (must be first statement in its own batch)
            try:
                cur.execute(body)
            except Exception as e:  # noqa: BLE001
                return f"DEPLOY FAILED at CREATE: {e}\nLog:\n  " + "\n  ".join(log)

            # f) batch 3: restore (full name WITH schema here)
            cur.execute(f"exec dbo.csSysRestoreObject N'{name}';")

            # g) verify
            ok = _exec_scalar(
                cur,
                "select count(*) from sys.objects where name = ? and type in ('P','FN','IF','TF','V','TR')",
                name,
            )
            in_prog = _exec_scalar(
                cur,
                "select count(*) from dbo.csSysObjVer where objectName = ? and inProgress = 1",
                name,
            )

    log.append(f"object exists in sys.objects: {bool(ok)}")
    log.append(f"inProgress rows left: {in_prog}")
    status = "DEPLOYED OK" if ok and not in_prog else "DEPLOYED WITH WARNINGS"
    return f"{status}: {full_name}\n  " + "\n  ".join(log)


# ---------------------------------------------------------------------------
# 2. cs_jsonsave (generic parametrized JSONSave)
# ---------------------------------------------------------------------------

def cs_jsonsave(
    connection_string: str,
    proc_name: str,
    rows: Sequence[dict],
) -> str:
    """
    Call any <T>JSONSave with a parametrized @data payload (safe from multiline /
    diacritics escaping issues that break inline JSON), then parse @response xml.

    `rows` is a list of objects, each must include `_opr` ('I'|'U'|'D') and the
    table's keys/columns. The payload is sent through a pyodbc parameter (NVARCHAR(MAX)).
    """
    if not proc_name or not proc_name.endswith("JSONSave"):
        return "Error: proc_name must be a <T>JSONSave procedure."
    if not rows:
        return "Error: rows is empty."
    for r in rows:
        if "_opr" not in r:
            return "Error: each row must include '_opr' (I|U|D)."

    pname = proc_name[4:] if proc_name.lower().startswith("dbo.") else proc_name
    payload = json.dumps(list(rows), ensure_ascii=False)

    sql = (
        "declare @response xml;\n"
        f"exec dbo.{pname} @data = ?, @response = @response out;\n"
        "select convert(nvarchar(max), @response) [response];"
    )
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, payload)
            row = cur.fetchone()
            resp = _xml_response_to_text(row[0] if row else None)

    if resp is None:
        return f"OK: {pname} applied {len(rows)} row(s) (response NULL = success)."
    return f"WARNING from {pname} (response):\n{resp}"


# ---------------------------------------------------------------------------
# 3. add_cs_column
# ---------------------------------------------------------------------------

def add_cs_column(
    connection_string: str,
    table_name: str,
    column_name: str,
    base_type: str,
    descriptions: dict,
    nullable: bool = True,
    column_params: Optional[str] = None,
    default_def: Optional[str] = None,
) -> str:
    """
    Add a column to a cs* table via csSysColumnsJSONSave + csSysTablesRebuild.
    Enforces: ColumnOrder = max+1, full 12x ColumnDesc_XX, both rebuild params.

    `descriptions` must provide all 12 langs (EN, PL, DE, FR, ES, IT, NL, PT, RU, UK, SK, SE).
    `base_type` is the bare SQL type ('int', 'bigint', 'nvarchar', 'bit', 'datetime', ...).
    `column_params` like '(200)', '(max)', '(18,6)' for parametrized types.
    `default_def` like '((0))' for bit/int defaults (NOT 'DefaultValue').
    """
    missing = [l for l in COLUMN_DESC_LANGS if not descriptions.get(l)]
    if missing:
        return f"Error: descriptions missing languages: {', '.join(missing)} (all 12 required)."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            tbl_g = _exec_scalar(
                cur,
                "select csSysTablesG from dbo.csSysTables with(nolock) where TableName = ?",
                table_name,
            )
            if not tbl_g:
                return f"Error: table '{table_name}' not found in csSysTables."

            exists = _exec_scalar(
                cur,
                "select 1 from dbo.csSysColumns where csSysTablesG = ? and ColumnName = ?",
                tbl_g, column_name,
            )
            if exists:
                return f"Column '{column_name}' already exists in {table_name} (skipped)."

            max_ord = _exec_scalar(
                cur,
                "select isnull(max(ColumnOrder), 0) from dbo.csSysColumns where csSysTablesG = ?",
                tbl_g,
            )

            row = {
                "_opr": "I",
                "csSysTablesG": str(tbl_g),
                "ColumnName": column_name,
                "BaseType": base_type,
                "IsNullable": 1 if nullable else 0,
                "ColumnOrder": int(max_ord) + 1,
            }
            if column_params:
                row["ColumnParams"] = column_params
            if default_def:
                row["DefaultDef"] = default_def
            for lang in COLUMN_DESC_LANGS:
                row[f"ColumnDesc_{lang}"] = descriptions[lang]

            payload = json.dumps([row], ensure_ascii=False)
            cur.execute(
                "declare @response xml; "
                "exec dbo.csSysColumnsJSONSave @data = ?, @response = @response out; "
                "select convert(nvarchar(max), @response) [response];",
                payload,
            )
            r = cur.fetchone()
            resp = _xml_response_to_text(r[0] if r else None)
            if resp:
                return f"csSysColumnsJSONSave WARNING:\n{resp}"

            # rebuild (both params required)
            cur.execute(
                "declare @r xml; "
                "exec dbo.csSysTablesRebuild @csSysTablesG = ?, @TableName = ?, @response = @r out; "
                "select convert(nvarchar(max), @r) [response];",
                tbl_g, table_name,
            )
            r2 = cur.fetchone()
            resp2 = _xml_response_to_text(r2[0] if r2 else None)
            if resp2:
                return f"Column added but csSysTablesRebuild WARNING:\n{resp2}"

            # verify physical
            phys = _exec_scalar(
                cur,
                "select count(*) from sys.columns where object_id = object_id(?) and name = ?",
                f"dbo.{table_name}", column_name,
            )

    return (
        f"OK: column {table_name}.{column_name} ({base_type}{column_params or ''}) "
        f"added at order {int(max_ord)+1}, physical={bool(phys)}."
    )


# ---------------------------------------------------------------------------
# 4. add_ng_field
# ---------------------------------------------------------------------------

def add_ng_field(
    connection_string: str,
    app_window_ident: str,
    field_ident: str,
    format_type: str,
    sql_base_type: str,
    label_pl: str,
    label_en: str,
    data_set_ident: str = "main",
    alias: str = "s",
    sql_column_params: Optional[str] = None,
    add_to_form: bool = True,
    width: float = 180.0,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Add a field to an NG window: csNGAppWindowDataSetsFields + LayoutsCols,
    and (optionally) inject <c-edit> into the ins/upd action viewHTML.

    Enforces: idempotent UPSERT, layout col requires labelDataSetIdent/labelDataFieldIdent
    and a non-null width (else the column silently collapses).
    """
    log: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            # --- Field ---
            f_id = _exec_scalar(
                cur,
                "select csNGAppWindowDataSetsFieldsId from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            f_g = _exec_scalar(
                cur,
                "select csNGAppWindowDataSetsFieldsG from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            max_ord = _exec_scalar(
                cur,
                "select isnull(max(ord),0) from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ?",
                namespace_g, app_window_ident, data_set_ident,
            )
            field_row = {
                "_opr": "U" if f_id else "I",
                "csNGAppWindowDataSetsFieldsG": str(f_g) if f_g else _new_guid(),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "dataSetIdent": data_set_ident,
                "ord": int(max_ord) + (0 if f_id else 1),
                "dataFieldIdent": field_ident,
                "dataFieldLabDesc_PL": label_pl,
                "dataFieldLabDesc_EN": label_en,
                "dataFieldColDesc_PL": label_pl,
                "dataFieldColDesc_EN": label_en,
                "formatType": format_type,
                "SQLBaseType": sql_base_type,
                "alias": alias,
                "isTranslate": 0,
                "addToSelect": 1,
            }
            if f_id:
                field_row["csNGAppWindowDataSetsFieldsId"] = int(f_id)
            if sql_column_params:
                field_row["SQLColumnParams"] = sql_column_params
            cur.execute(
                "declare @response xml; "
                "exec dbo.csNGAppWindowDataSetsFieldsJSONSave @data = ?, @response = @response out; "
                "select convert(nvarchar(max), @response) [response];",
                json.dumps([field_row], ensure_ascii=False),
            )
            r = cur.fetchone()
            resp = _xml_response_to_text(r[0] if r else None)
            if resp:
                return f"Field JSONSave WARNING:\n{resp}"
            log.append(f"field {field_ident} ({'U' if f_id else 'I'})")

            # --- Layout col (default layout) ---
            lc_id = _exec_scalar(
                cur,
                "select csNGAppWindowDataSetsLayoutsColsId from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and layoutIdent = 'default' "
                "and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            lc_g = _exec_scalar(
                cur,
                "select csNGAppWindowDataSetsLayoutsColsG from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and layoutIdent = 'default' "
                "and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            lc_max = _exec_scalar(
                cur,
                "select isnull(max(ord),0) from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and layoutIdent = 'default'",
                namespace_g, app_window_ident, data_set_ident,
            )
            lc_row = {
                "_opr": "U" if lc_id else "I",
                "csNGAppWindowDataSetsLayoutsColsG": str(lc_g) if lc_g else _new_guid(),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "dataSetIdent": data_set_ident,
                "layoutIdent": "default",
                "ord": int(lc_max) + (0 if lc_id else 1),
                "dataFieldIdent": field_ident,
                "labelDataSetIdent": data_set_ident,
                "labelDataFieldIdent": field_ident,
                "width": width,
                "isVisible": 1,
            }
            if lc_id:
                lc_row["csNGAppWindowDataSetsLayoutsColsId"] = int(lc_id)
            cur.execute(
                "declare @response xml; "
                "exec dbo.csNGAppWindowDataSetsLayoutsColsJSONSave @data = ?, @response = @response out; "
                "select convert(nvarchar(max), @response) [response];",
                json.dumps([lc_row], ensure_ascii=False),
            )
            r = cur.fetchone()
            resp = _xml_response_to_text(r[0] if r else None)
            if resp:
                return f"Field added but LayoutCol JSONSave WARNING:\n{resp}"
            log.append(f"layoutcol {field_ident} ({'U' if lc_id else 'I'})")

            # --- optional: inject <c-edit> into ins/upd viewHTML ---
            if add_to_form:
                for action_ident in ("ins", "upd"):
                    act = cur.execute(
                        "select csNGAppWindowDataSetsActionsId, csNGAppWindowDataSetsActionsG, viewHTML "
                        "from dbo.csNGAppWindowDataSetsActions with(nolock) "
                        "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and actionIdent = ?",
                        namespace_g, app_window_ident, data_set_ident, action_ident,
                    ).fetchone()
                    if not act:
                        continue
                    act_id, act_g, html = act
                    html = html or "<div class=\"c-l-v\">\n</div>"
                    if field_ident in html:
                        log.append(f"viewHTML[{action_ident}] already has {field_ident}")
                        continue
                    edit = (
                        f"<c-edit\nclass=\"c-w-2\"\ndataFieldIdent=\"{field_ident}\"\n/>"
                    )
                    # insert before the last closing </div>
                    idx = html.rfind("</div>")
                    new_html = (html[:idx] + edit + "\n" + html[idx:]) if idx >= 0 else html + "\n" + edit
                    act_row = {
                        "_opr": "U",
                        "csNGAppWindowDataSetsActionsId": int(act_id),
                        "csNGAppWindowDataSetsActionsG": str(act_g),
                        "csAppNameSpacesG": namespace_g,
                        "appWindowIdent": app_window_ident,
                        "dataSetIdent": data_set_ident,
                        "actionIdent": action_ident,
                        "viewHTML": new_html,
                    }
                    cur.execute(
                        "declare @response xml; "
                        "exec dbo.csNGAppWindowDataSetsActionsJSONSave @data = ?, @response = @response out; "
                        "select convert(nvarchar(max), @response) [response];",
                        json.dumps([act_row], ensure_ascii=False),
                    )
                    r = cur.fetchone()
                    resp = _xml_response_to_text(r[0] if r else None)
                    if resp:
                        log.append(f"viewHTML[{action_ident}] WARNING: {resp[:120]}")
                    else:
                        log.append(f"viewHTML[{action_ident}] injected")

    return f"OK: NG field {app_window_ident}.{data_set_ident}.{field_ident}\n  " + "\n  ".join(log)


# ---------------------------------------------------------------------------
# 5. get_cs_object_versions
# ---------------------------------------------------------------------------

def get_cs_object_versions(
    connection_string: str,
    object_name: str,
    top: int = 10,
) -> str:
    """List recent csSysObjVer rows + inProgress state for an object (no dbo. prefix)."""
    name = object_name.strip()
    if name.lower().startswith("dbo."):
        name = name[4:]
    with connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select top (?) v.verId, v.verG, v.parentVerG, v.inProgress, v.isRegOnly, v.verDescription "
                "from dbo.csSysObjVer v with(nolock) where v.objectName = ? order by v.verId desc",
                top, name,
            )
            rows = cur.fetchall()
            in_prog = _exec_scalar(
                cur,
                "select count(*) from dbo.csSysObjVer where objectName = ? and inProgress = 1",
                name,
            )
            exists = _exec_scalar(
                cur,
                "select count(*) from sys.objects where name = ?",
                name,
            )
    if not rows:
        return f"(no csSysObjVer rows for '{name}'; object in sys.objects: {bool(exists)})"
    lines = [
        f"object: {name} | in sys.objects: {bool(exists)} | inProgress rows: {in_prog}",
        "verId | verG | parentVerG | inProgress | isRegOnly | desc",
    ]
    for verId, verG, pverG, inP, reg, desc in rows:
        lines.append(
            f"{verId} | {verG} | {pverG} | {int(inP)} | {int(reg)} | {(desc or '')[:60]}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. update_view_html  (sync .vue <template> -> DB viewHTML, like husky)
# ---------------------------------------------------------------------------

def _extract_template(vue_source: str) -> Optional[str]:
    """
    Extract the inner content of the ROOT <template>...</template> block, replicating
    the husky/csRestAPIcsNGAppWindowsViewHTMLSave logic: take the lines strictly
    between a line that is exactly '<template>' and the LAST line that is exactly
    '</template>', trim each line, drop blanks, join with CRLF, then ##asterix## -> *.
    Nested slot templates (<template #slot>) are preserved (they are not exact
    '<template>' so they don't move the boundaries).
    """
    lines = vue_source.splitlines()
    start = None
    end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "<template>" and start is None:
            start = i
        if s == "</template>":
            end = i
    if start is None or end is None or end <= start:
        return None
    inner = [line.strip() for line in lines[start + 1:end]]
    inner = [line for line in inner if line != ""]
    template = "\r\n".join(inner).replace("##asterix##", "*")
    return template or None


def update_view_html(
    connection_string: str,
    app_window_ident: str,
    file_path: Optional[str] = None,
    content: Optional[str] = None,
    component: Optional[str] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Sync a window/action .vue <template> into the DB viewHTML on demand — same effect
    as the husky pre-commit hook, but without commit+push. Saves through the proper
    JSONSave (so csNGAppWindows.dataSets cache rebuilds itself):
      - window/grid file (component == app_window_ident, e.g. csMicroOrders.vue)
        -> csNGAppWindows.viewHTML via csNGAppWindowsJSONSave.
      - action form file (component == '<dataSet>_<action>', e.g. main_ins.vue)
        -> csNGAppWindowDataSetsActions.viewHTML via csNGAppWindowDataSetsActionsJSONSave.

    Provide `file_path` (preferred) or raw `content`. `component` defaults to the file
    base name without '.vue'.
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."

    if content is None:
        if not file_path:
            return "Error: provide file_path or content."
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return f"Error: cannot read file_path: {exc}"

    if component is None:
        import os
        base = os.path.basename(file_path) if file_path else aw
        component = re.sub(r"\.vue$", "", base, flags=re.IGNORECASE)
    component = component.strip()

    template = _extract_template(content)
    if not template:
        return ("Error: no <template>...</template> found "
                "(need a line that is exactly '<template>' and one that is exactly '</template>').")

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            if component == aw:
                target = f"csNGAppWindows.viewHTML [{aw}]"
                sql = (
                    "set nocount on;\n"
                    "declare @vh nvarchar(max)=?, @ns uniqueidentifier=?, @aw nvarchar(200)=?;\n"
                    "declare @data nvarchar(max), @response xml, @cnt int;\n"
                    "select @cnt = count(*) from dbo.csNGAppWindows where csAppNameSpacesG=@ns and appWindowIdent=@aw;\n"
                    "set @data = (select N'U' [_opr], w.csNGAppWindowsId, w.csNGAppWindowsG, w.csAppNameSpacesG, "
                    "w.appWindowIdent, @vh viewHTML from dbo.csNGAppWindows w "
                    "where w.csAppNameSpacesG=@ns and w.appWindowIdent=@aw for json path, include_null_values);\n"
                    "if @data is not null exec dbo.csNGAppWindowsJSONSave @data=@data, @response=@response out;\n"
                    "select @cnt [matched], convert(nvarchar(max), @response) [response];"
                )
                cur.execute(sql, (template, namespace_g, aw))
            else:
                parts = component.split("_")
                if len(parts) < 2:
                    return (f"Error: action file '{component}' must be '<dataSet>_<action>' "
                            f"(e.g. main_ins). A window file name must equal app_window_ident.")
                data_set_ident, action_ident = parts[0], parts[1]
                target = f"csNGAppWindowDataSetsActions.viewHTML [{aw}/{data_set_ident}/{action_ident}]"
                sql = (
                    "set nocount on;\n"
                    "declare @vh nvarchar(max)=?, @ns uniqueidentifier=?, @aw nvarchar(200)=?, "
                    "@ds nvarchar(200)=?, @act nvarchar(200)=?;\n"
                    "declare @data nvarchar(max), @response xml, @cnt int;\n"
                    "select @cnt = count(*) from dbo.csNGAppWindowDataSetsActions "
                    "where csAppNameSpacesG=@ns and appWindowIdent=@aw and dataSetIdent=@ds and actionIdent=@act;\n"
                    "set @data = (select N'U' [_opr], a.csNGAppWindowDataSetsActionsId, a.csNGAppWindowDataSetsActionsG, "
                    "a.csAppNameSpacesG, a.appWindowIdent, a.dataSetIdent, a.actionIdent, @vh viewHTML "
                    "from dbo.csNGAppWindowDataSetsActions a "
                    "where a.csAppNameSpacesG=@ns and a.appWindowIdent=@aw and a.dataSetIdent=@ds and a.actionIdent=@act "
                    "for json path, include_null_values);\n"
                    "if @data is not null exec dbo.csNGAppWindowDataSetsActionsJSONSave @data=@data, @response=@response out;\n"
                    "select @cnt [matched], convert(nvarchar(max), @response) [response];"
                )
                cur.execute(sql, (template, namespace_g, aw, data_set_ident, action_ident))

            row = cur.fetchone()
            matched = (row[0] if row else 0) or 0
            resp = _xml_response_to_text(row[1] if row else None)

    if not matched:
        return (f"Error: target not found in DB ({target}). "
                f"Check app_window_ident / component / namespace_g.")
    if resp is None:
        return f"OK: {target} updated ({len(template)} chars; response NULL = success)."
    return f"WARNING updating {target} (response):\n{resp}"


# ---------------------------------------------------------------------------
# 7. ng_get_window_config  (read-only dump)
# ---------------------------------------------------------------------------

def ng_get_window_config(
    connection_string: str,
    app_window_ident: str,
    data_set_ident: Optional[str] = None,
    include_stmsql: bool = False,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Compact, read-only dump of a full NG window configuration. Replaces the ad-hoc
    SELECT scripts (_q_fields/_q_cols/_q_wf/_q_sort/_dump_stmsql...) with one call.
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    out: List[str] = []
    ds_f = " and dataSetIdent = ?" if data_set_ident else ""

    def _p(*extra):
        base = [namespace_g, aw]
        if data_set_ident:
            base.append(data_set_ident)
        return base + list(extra)

    with connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select useAutoWindow, onlyAsLookup, getMetaInfo, outOfPrivileges, "
                "appWindowDesc_PL, appWindowDesc_EN from dbo.csNGAppWindows with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?",
                namespace_g, aw,
            )
            w = cur.fetchone()
            if not w:
                return f"Error: window '{aw}' not found (namespace {namespace_g})."
            out.append(
                f"WINDOW {aw} | useAutoWindow={w[0]} onlyAsLookup={w[1]} "
                f"getMetaInfo={w[2]} outOfPrivileges={w[3]} | PL={w[4]!r} EN={w[5]!r}"
            )

            cur.execute(
                "select dataSetIdent, ord, pageSize, pagingDisabled, notUseSort, "
                "dataFieldIdent4ReadOnly, len(isnull(stmSQL, N'')), stmSQL "
                "from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by ord",
                *_p(),
            )
            out.append("\nDATASETS (ident | ord pageSize pagingDisabled notUseSort ro4 stmLen):")
            for r in cur.fetchall():
                out.append(f"  {r[0]} | {r[1]} {r[2]} {r[3]} {r[4]} {r[5]!r} {r[6]}")
                if include_stmsql and r[7]:
                    out.append("  --- stmSQL ---\n" + r[7] + "\n  --- /stmSQL ---")

            cur.execute(
                "select dataSetIdent, ord, dataFieldIdent, formatType, SQLBaseType, "
                "isnull(SQLColumnParams, N''), isnull(alias, N''), isnull(columnAlias, N''), "
                "isTranslate, addToSelect, isnull(onCFActionIdent, N''), "
                "dataFieldLabDesc_PL, dataFieldColDesc_PL "
                "from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, ord",
                *_p(),
            )
            out.append("\nFIELDS (ds ord ident | fmt type params alias colAlias isTr addSel onCF | labPL colPL):")
            for r in cur.fetchall():
                out.append(
                    f"  {r[0]} {r[1]} {r[2]} | {r[3]} {r[4]}{r[5]} a={r[6]!r} ca={r[7]!r} "
                    f"tr={r[8]} sel={r[9]} onCF={r[10]!r} | {r[11]!r} {r[12]!r}"
                )

            cur.execute(
                "select layoutIdent, ord, dataFieldIdent, width, isVisible, "
                "isnull(colsGroupIdent, N''), isnull(labelDataFieldIdent, N'') "
                "from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by layoutIdent, ord",
                *_p(),
            )
            out.append("\nLAYOUT COLS (layout ord ident | width visible group labelIdent):")
            for r in cur.fetchall():
                out.append(f"  {r[0]} {r[1]} {r[2]} | w={r[3]} vis={r[4]} grp={r[5]!r} lbl={r[6]!r}")

            cur.execute(
                "select colsGroupIdent, dataFieldColsGroupDesc_PL, dataFieldColsGroupDesc_EN "
                "from dbo.csNGAppWindowColsGroups with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? order by colsGroupIdent",
                namespace_g, aw,
            )
            rows = cur.fetchall()
            if rows:
                out.append("\nCOLS GROUPS (ident | PL EN):")
                for r in rows:
                    out.append(f"  {r[0]} | {r[1]!r} {r[2]!r}")

            cur.execute(
                "select dataSetIdent, actionIdent, kind, isnull(SQLName, N''), showView, "
                "hideWhenEmpty, ord from dbo.csNGAppWindowDataSetsActions with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, ord",
                *_p(),
            )
            out.append("\nACTIONS (ds ident | kind SQLName showView hideWhenEmpty ord):")
            for r in cur.fetchall():
                out.append(f"  {r[0]} {r[1]} | {r[2]} {r[3]!r} {r[4]} {r[5]} {r[6]}")

            cur.execute(
                "select dataSetIdent, ord, dataFieldIdent, formatType, isnull(dataFieldValueDef, N''), "
                "isnull(linkedParamName, N''), initNewRow, dataFieldLabDesc_PL "
                "from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, ord",
                *_p(),
            )
            rows = cur.fetchall()
            if rows:
                out.append("\nWHERE FIELDS (ds ord ident | fmt valueDef linkedParam initNewRow labPL):")
                for r in rows:
                    out.append(f"  {r[0]} {r[1]} {r[2]} | {r[3]} {r[4]!r} {r[5]!r} {r[6]} {r[7]!r}")

            cur.execute(
                "select dataSetIdent, ord, sortIdent, isDef, isnull(layoutIdent, N''), sortDesc_PL "
                "from dbo.csNGAppWindowDataSetsSortIdents with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, ord",
                *_p(),
            )
            rows = cur.fetchall()
            out.append("\nSORT IDENTS (ds ord ident | isDef layout PL):" if rows else "\nSORT IDENTS: NONE (required for every NG window!)")
            for r in rows:
                out.append(f"  {r[0]} {r[1]} {r[2]} | {r[3]} {r[4]!r} {r[5]!r}")

            cur.execute(
                "select dataSetIdent, ord, dataFieldIdent from dbo.csNGAppWindowDataSetsKeyFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, ord",
                *_p(),
            )
            rows = cur.fetchall()
            if rows:
                out.append("\nKEY FIELDS: " + ", ".join(f"{r[0]}.{r[2]}" for r in rows))

            cur.execute(
                "select dataSetIdent, dataFieldIdent, appWindowIdentLookup, sourceKind, isMultiSelect "
                "from dbo.csNGAppWindowDataSetsLookupDefs with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?" + ds_f + " order by dataSetIdent, dataFieldIdent",
                *_p(),
            )
            rows = cur.fetchall()
            if rows:
                out.append("\nLOOKUP DEFS (ds field -> lookupWindow | sourceKind multi):")
                for r in rows:
                    out.append(f"  {r[0]} {r[1]} -> {r[2]} | {r[3]} {r[4]}")

            cur.execute(
                "select appWindowIdentTo, placement, ord, appWindowLinkDesc_PL "
                "from dbo.csNGAppWindowsLinks with(nolock) "
                "where csAppNameSpacesGFrom = ? and appWindowIdentFrom = ? order by ord",
                namespace_g, aw,
            )
            rows = cur.fetchall()
            if rows:
                out.append("\nLINKS (to | placement ord PL):")
                for r in rows:
                    out.append(f"  {r[0]} | {r[1]} {r[2]} {r[3]!r}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 8. ng_set_field_labels
# ---------------------------------------------------------------------------

def ng_set_field_labels(
    connection_string: str,
    app_window_ident: str,
    field_ident: str,
    labels: dict,
    data_set_ident: str = "main",
    target: str = "field",
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Update labels of an NG field (or whereField): dataFieldLabDesc_XX / dataFieldColDesc_XX /
    dataFieldWatermarkDesc_XX. Pitfalls handled:
      - minimal-U payload (Id+G+changed cols, NO natural key) -> skips SQLBaseType re-validation;
      - current formatType is ALWAYS re-sent (otherwise JSONSave silently resets it to 'string');
      - only the provided languages are written — never auto-copied from PL (HARD RULE 13).

    `labels` = {"PL": {"lab": "...", "col": "...", "watermark": "..."}, "EN": {...}}
    (each of lab/col/watermark optional; language keys must be one of NG_LABEL_LANGS).
    """
    if target not in ("field", "whereField"):
        return "Error: target must be 'field' or 'whereField'."
    if not labels or not isinstance(labels, dict):
        return "Error: labels dict is required, e.g. {'PL': {'lab': 'Nazwa'}, 'EN': {'lab': 'Name'}}."

    bad_langs = [l for l in labels if l not in NG_LABEL_LANGS]
    if bad_langs:
        return f"Error: unsupported language(s): {', '.join(bad_langs)} (allowed: {', '.join(NG_LABEL_LANGS)})."

    tbl = "csNGAppWindowDataSetsFields" if target == "field" else "csNGAppWindowDataSetsWhereFields"
    proc = tbl + "JSONSave"

    changes: dict = {}
    for lang, parts in labels.items():
        if not isinstance(parts, dict):
            return f"Error: labels['{lang}'] must be an object with lab/col/watermark keys."
        bad = [k for k in parts if k not in ("lab", "col", "watermark")]
        if bad:
            return f"Error: labels['{lang}'] has unknown key(s): {', '.join(bad)} (allowed: lab, col, watermark)."
        if "lab" in parts:
            changes[f"dataFieldLabDesc_{lang}"] = parts["lab"]
        if "col" in parts:
            changes[f"dataFieldColDesc_{lang}"] = parts["col"]
        if "watermark" in parts:
            changes[f"dataFieldWatermarkDesc_{lang}"] = parts["watermark"]
    if not changes:
        return "Error: labels contain no lab/col/watermark values."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"select {tbl}Id, {tbl}G, formatType from dbo.{tbl} with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            row = cur.fetchone()
            if not row:
                return (f"Error: {target} '{field_ident}' not found in "
                        f"{app_window_ident}/{data_set_ident}.")
            rec = {
                "_opr": "U",
                f"{tbl}Id": int(row[0]),
                f"{tbl}G": str(row[1]).upper(),
                "formatType": row[2],  # pitfall: without it labels-update resets formatType to 'string'
            }
            rec.update(changes)
            resp = _jsonsave(cur, proc, [rec])

    if resp:
        return f"{proc} WARNING:\n{resp}"
    return (f"OK: {target} {app_window_ident}/{data_set_ident}/{field_ident} — "
            f"updated {len(changes)} label column(s) ({', '.join(sorted(changes))}); "
            f"formatType '{row[2]}' preserved.")


# ---------------------------------------------------------------------------
# 9. ng_set_layout_col
# ---------------------------------------------------------------------------

def ng_set_layout_col(
    connection_string: str,
    app_window_ident: str,
    field_ident: str,
    data_set_ident: str = "main",
    layout_ident: str = "default",
    width: Optional[float] = None,
    is_visible: Optional[bool] = None,
    ord: Optional[int] = None,
    cols_group_ident: Optional[str] = None,
    clear_cols_group: bool = False,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Upsert a grid layout column (csNGAppWindowDataSetsLayoutsCols). Covers: width change,
    show/hide, reorder, attach/detach column group. Pitfalls handled:
      - UPDATE = minimal-U (Id+G+changed props only) — natural key in a U row would force
        labelDataSetIdent/labelDataFieldIdent re-validation which fails for joined fields;
      - isVisible serialized as int 1/0 (bit -> true/false breaks the proc, msg 245);
      - INSERT always includes labelDataSetIdent/labelDataFieldIdent and a non-null width
        (NULL width silently collapses the column);
      - cols_group_ident is verified against csNGAppWindowColsGroups (warn if missing).
    """
    warnings: List[str] = []
    if width is not None and width % 60 != 0:
        warnings.append(f"width={width} is not a multiple of 60 (convention: 120 code / 220 desc).")
    if cols_group_ident and clear_cols_group:
        return "Error: pass either cols_group_ident or clear_cols_group, not both."

    changes: dict = {}
    if width is not None:
        changes["width"] = float(width)
    if is_visible is not None:
        changes["isVisible"] = _as_int(is_visible)
    if ord is not None:
        changes["ord"] = int(ord)
    if cols_group_ident:
        changes["colsGroupIdent"] = cols_group_ident
    if clear_cols_group:
        changes["colsGroupIdent"] = None

    if not changes:
        return "Error: nothing to change (pass width / is_visible / ord / cols_group_ident / clear_cols_group)."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            if cols_group_ident:
                grp = _exec_scalar(
                    cur,
                    "select 1 from dbo.csNGAppWindowColsGroups with(nolock) "
                    "where csAppNameSpacesG = ? and appWindowIdent = ? and colsGroupIdent = ?",
                    namespace_g, app_window_ident, cols_group_ident,
                )
                if not grp:
                    warnings.append(
                        f"cols group '{cols_group_ident}' does NOT exist in csNGAppWindowColsGroups "
                        "— create it first (ng_upsert_cols_group) or the grid header will not group."
                    )

            cur.execute(
                "select csNGAppWindowDataSetsLayoutsColsId, csNGAppWindowDataSetsLayoutsColsG "
                "from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? "
                "and layoutIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, layout_ident, field_ident,
            )
            lc = cur.fetchone()

            if lc:
                rec = {
                    "_opr": "U",
                    "csNGAppWindowDataSetsLayoutsColsId": int(lc[0]),
                    "csNGAppWindowDataSetsLayoutsColsG": str(lc[1]).upper(),
                }
                rec.update(changes)
                mode = "updated"
            else:
                fld = _exec_scalar(
                    cur,
                    "select 1 from dbo.csNGAppWindowDataSetsFields with(nolock) "
                    "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                    namespace_g, app_window_ident, data_set_ident, field_ident,
                )
                if not fld:
                    return (f"Error: field '{field_ident}' not found in "
                            f"{app_window_ident}/{data_set_ident} — add the field first (add_ng_field).")
                max_ord = _exec_scalar(
                    cur,
                    "select isnull(max(ord),0) from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                    "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and layoutIdent = ?",
                    namespace_g, app_window_ident, data_set_ident, layout_ident,
                )
                rec = {
                    "_opr": "I",
                    "csNGAppWindowDataSetsLayoutsColsG": _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": app_window_ident,
                    "dataSetIdent": data_set_ident,
                    "layoutIdent": layout_ident,
                    "dataFieldIdent": field_ident,
                    "labelDataSetIdent": data_set_ident,
                    "labelDataFieldIdent": field_ident,
                    "ord": int(ord) if ord is not None else int(max_ord) + 1,
                    "width": float(width) if width is not None else 120.0,
                    "isVisible": _as_int(is_visible) if is_visible is not None else 1,
                }
                if cols_group_ident:
                    rec["colsGroupIdent"] = cols_group_ident
                mode = "inserted"

            resp = _jsonsave(cur, "csNGAppWindowDataSetsLayoutsColsJSONSave", [rec])

    if resp:
        return f"LayoutsCols JSONSave WARNING:\n{resp}"
    msg = (f"OK: layout col {app_window_ident}/{data_set_ident}/{layout_ident}/{field_ident} {mode} "
           f"({', '.join(k for k in rec if not k.startswith('csNGAppWindow') and k != '_opr')}).")
    if warnings:
        msg += "\nWARNINGS:\n  - " + "\n  - ".join(warnings)
    return msg


# ---------------------------------------------------------------------------
# 10. ng_upsert_cols_group
# ---------------------------------------------------------------------------

def ng_upsert_cols_group(
    connection_string: str,
    app_window_ident: str,
    cols_group_ident: str,
    descriptions: dict,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Upsert a grid column group (csNGAppWindowColsGroups — note: table is per-WINDOW, no
    dataSetIdent). Column names are dataFieldColsGroupDesc_XX (NOT colsGroupDesc_XX).
    Only the provided languages are written — never auto-copied (HARD RULE 13).
    The group takes effect only when layout cols get colsGroupIdent (ng_set_layout_col)
    and grouped columns are ADJACENT by ord.
    """
    if not descriptions or not isinstance(descriptions, dict):
        return "Error: descriptions dict required, e.g. {'PL': 'Obowiązuje', 'EN': 'Valid'}."
    bad = [l for l in descriptions if l not in NG_COLSGROUP_LANGS]
    if bad:
        return f"Error: unsupported language(s): {', '.join(bad)} (allowed: {', '.join(NG_COLSGROUP_LANGS)})."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select csNGAppWindowColsGroupsId, csNGAppWindowColsGroupsG "
                "from dbo.csNGAppWindowColsGroups with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and colsGroupIdent = ?",
                namespace_g, app_window_ident, cols_group_ident,
            )
            g = cur.fetchone()
            rec = {
                "_opr": "U" if g else "I",
                "csNGAppWindowColsGroupsG": str(g[1]).upper() if g else _new_guid(),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "colsGroupIdent": cols_group_ident,
            }
            if g:
                rec["csNGAppWindowColsGroupsId"] = int(g[0])
            for lang, text in descriptions.items():
                rec[f"dataFieldColsGroupDesc_{lang}"] = text
            resp = _jsonsave(cur, "csNGAppWindowColsGroupsJSONSave", [rec])

    if resp:
        return f"ColsGroups JSONSave WARNING:\n{resp}"
    return (f"OK: cols group '{cols_group_ident}' {'updated' if g else 'created'} in {app_window_ident} "
            f"(langs: {', '.join(sorted(descriptions))}). Attach columns via ng_set_layout_col "
            f"(grouped columns must be adjacent by ord).")


# ---------------------------------------------------------------------------
# 11. ng_set_stmsql
# ---------------------------------------------------------------------------

STMSQL_TEST_PARAMS_DECL = (
    "@stmSQLOut nvarchar(max) out, @ch nvarchar(2), @csCompaniesIdStr nvarchar(30), "
    "@isRefreshOneRecord int, @LanguageSuffix nvarchar(2), @where nvarchar(max)"
)


def _test_stmsql(cur, stm: str, where_json: str) -> Optional[str]:
    """Run the stmSQL template through sp_executesql; return error text or None on success."""
    try:
        cur.execute(
            "declare @stmSQLOut nvarchar(max) = N''; "
            "exec sp_executesql @stmt = ?, @params = N'" + STMSQL_TEST_PARAMS_DECL + "', "
            "@stmSQLOut = @stmSQLOut out, @ch = ?, @csCompaniesIdStr = N'1435126', "
            "@isRefreshOneRecord = 0, @LanguageSuffix = N'PL', @where = ?; "
            "select @stmSQLOut;",
            stm, "\r\n", where_json,
        )
        row = cur.fetchone()
        generated = row[0] if row else None
        if not generated:
            return "template executed but @stmSQLOut is empty"
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def ng_set_stmsql(
    connection_string: str,
    app_window_ident: str,
    stm_sql: str,
    data_set_ident: str = "main",
    test: bool = True,
    test_where: str = '{"searchText":"test","dateFrom":"2026-01-01","dateTo":"2026-12-31"}',
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Replace csNGAppWindowDataSets.stmSQL with a MANDATORY sp_executesql test BEFORE saving
    (per adding-columns instruction): the template is executed twice — with the provided
    test_where (dates included: apostrophe bugs only show up with non-null dates) and with
    an empty where {}. Only if both pass is the new stmSQL saved (minimal-U via JSONSave).
    Set test=False ONLY when the template needs extra params (e.g. @csItemsIdStr) —
    then verify manually.
    """
    if not stm_sql or not stm_sql.strip():
        return "Error: stm_sql is required."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select csNGAppWindowDataSetsId, csNGAppWindowDataSetsG, len(isnull(stmSQL, N'')) "
                "from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ?",
                namespace_g, app_window_ident, data_set_ident,
            )
            ds = cur.fetchone()
            if not ds:
                return f"Error: dataset {app_window_ident}/{data_set_ident} not found."
            old_len = int(ds[2])

            test_log: List[str] = []
            if test:
                for label, wj in (("with dates", test_where), ("empty where", "{}")):
                    err = _test_stmsql(cur, stm_sql, wj)
                    if err:
                        return (
                            f"TEST FAILED ({label}) — stmSQL NOT saved.\n{err}\n"
                            "Hints: apostrophes (open >= ''' = 3, close N'''' = 4), iif() 3 args, "
                            "extra params (@csItemsIdStr...) -> test=False + manual sp_executesql."
                        )
                    test_log.append(f"test {label}: OK")

            rec = {
                "_opr": "U",
                "csNGAppWindowDataSetsId": int(ds[0]),
                "csNGAppWindowDataSetsG": str(ds[1]).upper(),
                "stmSQL": stm_sql,
            }
            resp = _jsonsave(cur, "csNGAppWindowDataSetsJSONSave", [rec])

    if resp:
        return f"csNGAppWindowDataSetsJSONSave WARNING:\n{resp}"
    lines = [f"OK: stmSQL {app_window_ident}/{data_set_ident} saved "
             f"({old_len} -> {len(stm_sql)} chars)."]
    lines += test_log if test else ["WARNING: saved WITHOUT sp_executesql test (test=False) — verify manually!"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 12. ng_set_dataset_props
# ---------------------------------------------------------------------------

def ng_set_dataset_props(
    connection_string: str,
    app_window_ident: str,
    props: dict,
    data_set_ident: str = "main",
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Update whitelisted csNGAppWindowDataSets columns (pageSize, pagingDisabled, notUseSort,
    dataFieldIdent4ReadOnly, loadDataImmediate...) via minimal-U JSONSave. stmSQL is
    deliberately NOT allowed here — use ng_set_stmsql (it enforces the sp_executesql test).
    Booleans are coerced to int 1/0 (bit -> true/false breaks int columns in JSONSave).
    """
    if not props or not isinstance(props, dict):
        return "Error: props dict is required, e.g. {'pagingDisabled': 1, 'pageSize': 100}."
    bad = [k for k in props if k not in NG_DATASET_PROPS_WHITELIST]
    if bad:
        return (f"Error: column(s) not allowed: {', '.join(bad)}. "
                f"Allowed: {', '.join(sorted(NG_DATASET_PROPS_WHITELIST))}. "
                "For stmSQL use ng_set_stmsql.")

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select csNGAppWindowDataSetsId, csNGAppWindowDataSetsG "
                "from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ?",
                namespace_g, app_window_ident, data_set_ident,
            )
            ds = cur.fetchone()
            if not ds:
                return f"Error: dataset {app_window_ident}/{data_set_ident} not found."
            rec = {
                "_opr": "U",
                "csNGAppWindowDataSetsId": int(ds[0]),
                "csNGAppWindowDataSetsG": str(ds[1]).upper(),
            }
            for k, v in props.items():
                rec[k] = _as_int(v) if isinstance(v, bool) else v
            resp = _jsonsave(cur, "csNGAppWindowDataSetsJSONSave", [rec])

    if resp:
        return f"csNGAppWindowDataSetsJSONSave WARNING:\n{resp}"
    return (f"OK: dataset {app_window_ident}/{data_set_ident} — "
            f"updated {', '.join(sorted(props))}.")


# ---------------------------------------------------------------------------
# 13. rebuild_user_rights
# ---------------------------------------------------------------------------

def rebuild_user_rights(
    connection_string: str,
    cs_companies_id: Optional[int] = None,
    cs_usr_id: Optional[int] = None,
    confirm_all: bool = False,
) -> str:
    """
    Rebuild the per-user cache in csCompaniesUsrs (appMainMenuJSON, appWindowIdentsWithRights,
    warehousesRights) — MANDATORY after changing menu items, NG windows or privileges,
    otherwise: missing menu entries / missing action buttons / eternal spinner.

    Direct UPDATE is correct here: cache columns are exception (b) of HARD RULE 1 —
    csCompaniesUsrsJSONSave SKIPS these columns (silent no-op).

    Scope: on DEV always narrow to the working company/user. A full run over all users
    requires confirm_all=True (long / can time out on big databases).
    """
    if not cs_companies_id and not confirm_all:
        return ("Error: pass cs_companies_id (and optionally cs_usr_id) to narrow the scope, "
                "or confirm_all=True for a full rebuild of all internal users (slow).")

    sql = (
        "update cu set "
        "cu.appMainMenuJSON = dbo.csFnGetAppMainMenuJSONForCompaniesUsr(cu.csCompaniesId, cu.csUsrId, null), "
        "cu.appWindowIdentsWithRights = dbo.csNGFnGetRightsJSON(cu.csCompaniesId, cu.csUsrId), "
        "cu.warehousesRights = dbo.csNGFnGetWarehousesRights(cu.csCompaniesId, cu.csUsrId) "
        "from dbo.csCompaniesUsrs cu "  # hard-rules-allow: kolumny cache, wyjątek HARD RULE 1b
        "where cu.IsExternal = 0"
    )
    params: List = []
    if cs_companies_id:
        sql += " and cu.csCompaniesId = ?"
        params.append(int(cs_companies_id))
    if cs_usr_id:
        sql += " and cu.csUsrId = ?"
        params.append(int(cs_usr_id))

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, *params) if params else cur.execute(sql)
            count = cur.rowcount

    scope = (f"company {cs_companies_id}" + (f", user {cs_usr_id}" if cs_usr_id else "")) \
        if cs_companies_id else "ALL internal users"
    return (f"OK: rights cache rebuilt for {count} user(s) ({scope}). "
            "Re-login / session refresh required to pick up changes.")


# ---------------------------------------------------------------------------
# 14. ai_tool_sync_params
# ---------------------------------------------------------------------------

def ai_tool_sync_params(
    connection_string: str,
    tool_name: str,
    params: Optional[Sequence[dict]] = None,
    generate_sync_script: bool = True,
) -> str:
    """
    Sync the AI tool parameter registry (csAIAgentsToolsParams) with the tool procedure.
    Pitfalls handled:
      - U rows MUST also carry csAIAgentsToolsG+name+type+isRequired (otherwise
        'Proszę uzupełnić pole...' and the WHOLE batch rolls back);
      - typeJSON is stored as a plain STRING (a dict is dumped; never json_query);
      - isRequired coerced to int.
    Always reports a heuristic diff: registry names vs $.keys referenced in the
    procedure body (AI tools take @dataInput json — sys.parameters is useless here).
    With generate_sync_script=True returns a csSysGenManagedSync redeploy script
    for the touched params (save it to data/_sync_<Tool>_params_....sql).
    """
    name = (tool_name or "").strip()
    if not name:
        return "Error: tool_name is required."
    short = name[4:] if name.lower().startswith("dbo.") else name

    out: List[str] = []
    touched: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select csAIAgentsToolsG, name, SQLProcedure from dbo.csAIAgentsTools with(nolock) "
                "where name = ? or SQLProcedure in (?, ?)",
                short, short, f"dbo.{short}",
            )
            t = cur.fetchone()
            if not t:
                return f"Error: AI tool '{short}' not found in csAIAgentsTools (name/SQLProcedure)."
            tool_g = str(t[0]).upper()
            proc = (t[2] or "").strip()
            out.append(f"TOOL {t[1]} | G={tool_g} | proc={proc or '(none)'}")

            # --- registry state ---
            cur.execute(
                "select csAIAgentsToolsParamsId, csAIAgentsToolsParamsG, name, type, isRequired, "
                "description, iif(typeJSON is null, 0, 1) "
                "from dbo.csAIAgentsToolsParams with(nolock) where csAIAgentsToolsG = ? order by name",
                tool_g,
            )
            reg = {r[2]: r for r in cur.fetchall()}

            # --- heuristic diff vs proc body ($.key references in @dataInput json) ---
            body_keys: set = set()
            if proc:
                body = _exec_scalar(
                    cur,
                    "select m.definition from sys.sql_modules m where m.object_id = object_id(?)",
                    proc if proc.lower().startswith("dbo.") else f"dbo.{proc}",
                )
                if body:
                    body_keys = set(re.findall(r"\$\.([A-Za-z_][A-Za-z0-9_]*)", body))

            # --- upserts ---
            rows: List[dict] = []
            for p in (params or []):
                pname = (p.get("name") or "").strip()
                if not pname:
                    return "Error: each param needs 'name'."
                existing = reg.get(pname)
                ptype = p.get("type") or (existing[3] if existing else None)
                if not ptype:
                    return f"Error: param '{pname}' is new — 'type' is required."
                is_req = p.get("isRequired", existing[4] if existing else 0)
                rec = {
                    "_opr": "U" if existing else "I",
                    "csAIAgentsToolsParamsG": str(existing[1]).upper() if existing else _new_guid(),
                    # U-gotcha: required fields must ALWAYS be present, not only changed ones
                    "csAIAgentsToolsG": tool_g,
                    "name": pname,
                    "type": ptype,
                    "isRequired": _as_int(is_req),
                }
                if existing:
                    rec["csAIAgentsToolsParamsId"] = int(existing[0])
                if "description" in p:
                    rec["description"] = p["description"]
                tj = p.get("typeJSON")
                if tj is not None:
                    rec["typeJSON"] = json.dumps(tj, ensure_ascii=False) if isinstance(tj, (dict, list)) else str(tj)
                rows.append(rec)
                touched.append(pname)

            if rows:
                resp = _jsonsave(cur, "csAIAgentsToolsParamsJSONSave", rows)
                if resp:
                    return f"csAIAgentsToolsParamsJSONSave WARNING (whole batch rolled back):\n{resp}"
                out.append(f"UPSERTED {len(rows)} param(s): "
                           + ", ".join(f"{r['name']}({r['_opr']})" for r in rows))
                if any("typeJSON" in r for r in rows):
                    cur.execute(
                        "select name, iif(typeJSON is null, 0, 1) from dbo.csAIAgentsToolsParams "
                        "with(nolock) where csAIAgentsToolsG = ? and name in ({})".format(
                            ",".join("?" * len(rows))),
                        tool_g, *[r["name"] for r in rows],
                    )
                    for n, has in cur.fetchall():
                        if not has and any(r["name"] == n and "typeJSON" in r for r in rows):
                            out.append(f"WARNING: typeJSON for '{n}' is NULL after save (silent schema loss)!")

                # refresh registry for the diff below
                cur.execute(
                    "select name from dbo.csAIAgentsToolsParams with(nolock) where csAIAgentsToolsG = ?",
                    tool_g,
                )
                reg_names = {r[0] for r in cur.fetchall()}
            else:
                reg_names = set(reg)

            out.append(f"\nREGISTRY ({len(reg_names)}): " + (", ".join(sorted(reg_names)) or "(empty)"))
            if body_keys:
                missing_in_reg = sorted(body_keys - reg_names)
                unused_in_proc = sorted(reg_names - body_keys)
                if missing_in_reg:
                    out.append("DIFF referenced in proc body but NOT in registry (heuristic $.key scan): "
                               + ", ".join(missing_in_reg))
                if unused_in_proc:
                    out.append("DIFF in registry but not referenced in proc body (heuristic): "
                               + ", ".join(unused_in_proc))
                if not missing_in_reg and not unused_in_proc:
                    out.append("DIFF: registry matches proc body references.")

            # --- redeploy script ---
            if generate_sync_script and touched:
                names_in = ",".join("''" + n.replace("'", "''''") + "''" for n in touched)
                where_exp = f"csAIAgentsToolsG = ''{tool_g}'' and name in ({names_in})"
                cur.execute(
                    "declare @x xml; exec dbo.csSysGenManagedSync "
                    "@object_name = N'csAIAgentsToolsParams', @where_exp = N'" + where_exp + "', "
                    "@select_results = 0, @results_xml = @x out; "
                    "select convert(nvarchar(max), @x);"
                )
                r = cur.fetchone()
                script_xml = r[0] if r else None
                if script_xml:
                    # <ScriptLines><row><line>...</line><id>n</id></row>... -> plain text
                    lines = re.findall(r"<line>(.*?)</line>", script_xml, flags=re.S)
                    script = "\n".join(
                        l.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                        for l in lines
                    )
                    out.append("\nSYNC SCRIPT (save to data/_sync_" + short + "_params_....sql):\n" + script)
                else:
                    out.append("\nWARNING: csSysGenManagedSync returned empty script.")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 15. ng_add_lookup
# ---------------------------------------------------------------------------

def ng_add_lookup(
    connection_string: str,
    app_window_ident: str,
    field_ident: str,
    lookup_window_ident: str,
    data_set_ident: str = "main",
    source_kind: str = "rows",
    is_multi_select: bool = False,
    close_kind: Optional[str] = None,
    search_get: bool = True,
    gets: Optional[Sequence[dict]] = None,
    sets: Optional[Sequence[dict]] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Wire a lookup on an NG field: csNGAppWindowDataSetsLookupDefs + Get + Set mappings.
    Conventions/pitfalls handled (verified on csSalesHeaders):
      - DEF always gets csAppNameSpacesGLookup (INSERT without it silently breaks);
      - every Get/Set row carries sourceKind = DEF.sourceKind (rows w/o it are ignored);
      - source_kind='rows' (form field) vs 'where' (filter panel host; default
        closeKind='onLostFocus');
      - search_get auto-adds Get: host field -> searchText[where] of the lookup;
      - Set rows default: lookup rows -> host (sourceKindTo = DEF.sourceKind);
        dataSetIdentFrom defaults to the lookup window's FIRST dataset (may be != main);
      - Get with 'value' -> sourceKindFrom='value' + dataFieldValueFrom (constant filter);
      - idempotent: existing identical mappings are skipped;
      - warns when the lookup window is missing onlyAsLookup=1 or has no sort idents.

    gets: [{"from_field": .., "to_field": .., "value": .., "source_kind_from": ..,
            "source_kind_to": ..}]  (from_field XOR value)
    sets: [{"from_field": .., "to_field": .., "data_set_ident_from": ..,
            "source_kind_to": ..}]
    """
    if source_kind not in ("rows", "where"):
        return "Error: source_kind must be 'rows' (form field) or 'where' (filter panel)."
    if not sets and not search_get and not gets:
        return "Error: nothing to wire — provide sets/gets or leave search_get=True."

    warnings: List[str] = []
    log: List[str] = []
    if close_kind is None and source_kind == "where":
        close_kind = "onLostFocus"

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            # --- host field must exist ---
            fld = _exec_scalar(
                cur,
                "select 1 from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            ) or _exec_scalar(
                cur,
                "select 1 from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            if not fld:
                return (f"Error: host field '{field_ident}' not found in "
                        f"{app_window_ident}/{data_set_ident} (Fields nor WhereFields).")

            # --- lookup window checks ---
            cur.execute(
                "select onlyAsLookup, getMetaInfo from dbo.csNGAppWindows with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?",
                namespace_g, lookup_window_ident,
            )
            lw = cur.fetchone()
            if not lw:
                return f"Error: lookup window '{lookup_window_ident}' not found."
            if not lw[0]:
                warnings.append(f"lookup window {lookup_window_ident} has onlyAsLookup=0 "
                                "(convention: dedicated lookup windows set it to 1 + getMetaInfo=0).")
            lookup_ds = _exec_scalar(
                cur,
                "select top 1 dataSetIdent from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? order by ord",
                namespace_g, lookup_window_ident,
            ) or "main"
            has_sort = _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindowDataSetsSortIdents with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?",
                namespace_g, lookup_window_ident,
            )
            if not has_sort:
                warnings.append(f"lookup window {lookup_window_ident} has NO sort idents "
                                "(REQUIRED for lookup windows — paging is unstable without it).")

            # --- DEF upsert ---
            cur.execute(
                "select csNGAppWindowDataSetsLookupDefsId, csNGAppWindowDataSetsLookupDefsG "
                "from dbo.csNGAppWindowDataSetsLookupDefs with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            d = cur.fetchone()
            def_row = {
                "_opr": "U" if d else "I",
                "csNGAppWindowDataSetsLookupDefsG": str(d[1]).upper() if d else _new_guid(),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "dataSetIdent": data_set_ident,
                "dataFieldIdent": field_ident,
                "appWindowIdentLookup": lookup_window_ident,
                "csAppNameSpacesGLookup": namespace_g,  # pitfall: INSERT without it silently breaks
                "sourceKind": source_kind,
                "isMultiSelect": _as_int(is_multi_select),
            }
            if close_kind:
                def_row["closeKind"] = close_kind
            if d:
                def_row["csNGAppWindowDataSetsLookupDefsId"] = int(d[0])
            resp = _jsonsave(cur, "csNGAppWindowDataSetsLookupDefsJSONSave", [def_row])
            if resp:
                return f"LookupDefs JSONSave WARNING:\n{resp}"
            log.append(f"DEF {field_ident} -> {lookup_window_ident} ({'U' if d else 'I'}, "
                       f"sourceKind={source_kind})")

            # --- GET rows ---
            get_rows: List[dict] = []
            if search_get:
                get_rows.append({
                    "from_field": field_ident, "to_field": "searchText",
                    "source_kind_from": source_kind, "source_kind_to": "where",
                })
            for g in (gets or []):
                get_rows.append(dict(g))

            n_get = 0
            for g in get_rows:
                from_field = g.get("from_field")
                value = g.get("value")
                if (from_field is None) == (value is None):
                    return f"Error: Get mapping needs exactly one of from_field/value: {g}"
                sk_from = g.get("source_kind_from") or ("value" if value is not None else source_kind)
                sk_to = g.get("source_kind_to") or "where"
                to_field = g.get("to_field")
                if not to_field:
                    return f"Error: Get mapping needs to_field: {g}"
                ds_from = g.get("data_set_ident_from") or data_set_ident
                ds_to = g.get("data_set_ident_to") or lookup_ds
                exists = _exec_scalar(
                    cur,
                    "select 1 from dbo.csNGAppWindowDataSetsLookupDefsGet with(nolock) "
                    "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? "
                    "and dataFieldIdent = ? and isnull(dataFieldIdentFrom, N'') = ? "
                    "and dataFieldIdentTo = ? and isnull(dataFieldValueFrom, N'') = ? "
                    "and sourceKindFrom = ? and sourceKindTo = ?",
                    namespace_g, app_window_ident, data_set_ident, field_ident,
                    from_field or "", to_field, "" if value is None else str(value), sk_from, sk_to,
                )
                if exists:
                    log.append(f"GET {from_field or value!r} -> {to_field} already exists (skip)")
                    continue
                rec = {
                    "_opr": "I",
                    "csNGAppWindowDataSetsLookupDefsGetG": _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": app_window_ident,
                    "dataSetIdent": data_set_ident,
                    "dataFieldIdent": field_ident,
                    "dataSetIdentFrom": ds_from,
                    "dataSetIdentTo": ds_to,
                    "dataFieldIdentTo": to_field,
                    "sourceKindFrom": sk_from,
                    "sourceKindTo": sk_to,
                    "sourceKind": source_kind,  # pitfall: rows without sourceKind are ignored
                }
                if from_field is not None:
                    rec["dataFieldIdentFrom"] = from_field
                if value is not None:
                    rec["dataFieldValueFrom"] = str(value)
                resp = _jsonsave(cur, "csNGAppWindowDataSetsLookupDefsGetJSONSave", [rec])
                if resp:
                    return f"LookupDefsGet JSONSave WARNING (after {n_get} gets):\n{resp}"
                n_get += 1
                log.append(f"GET {from_field or ('value ' + str(value))} [{sk_from}] -> {to_field} [{sk_to}]")

            # --- SET rows ---
            n_set = 0
            for s in (sets or []):
                from_field = s.get("from_field")
                to_field = s.get("to_field") or from_field
                if not from_field:
                    return f"Error: Set mapping needs from_field: {s}"
                ds_from = s.get("data_set_ident_from") or lookup_ds
                sk_to = s.get("source_kind_to") or source_kind
                exists = _exec_scalar(
                    cur,
                    "select 1 from dbo.csNGAppWindowDataSetsLookupDefsSet with(nolock) "
                    "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? "
                    "and dataFieldIdent = ? and dataFieldIdentFrom = ? and dataFieldIdentTo = ? "
                    "and sourceKindTo = ?",
                    namespace_g, app_window_ident, data_set_ident, field_ident,
                    from_field, to_field, sk_to,
                )
                if exists:
                    log.append(f"SET {from_field} -> {to_field} already exists (skip)")
                    continue
                rec = {
                    "_opr": "I",
                    "csNGAppWindowDataSetsLookupDefsSetG": _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": app_window_ident,
                    "dataSetIdent": data_set_ident,
                    "dataFieldIdent": field_ident,
                    "dataSetIdentFrom": ds_from,
                    "dataFieldIdentFrom": from_field,
                    "dataSetIdentTo": s.get("data_set_ident_to") or data_set_ident,
                    "dataFieldIdentTo": to_field,
                    "sourceKindFrom": s.get("source_kind_from") or "rows",
                    "sourceKindTo": sk_to,
                    "sourceKind": source_kind,
                }
                resp = _jsonsave(cur, "csNGAppWindowDataSetsLookupDefsSetJSONSave", [rec])
                if resp:
                    return f"LookupDefsSet JSONSave WARNING (after {n_set} sets):\n{resp}"
                n_set += 1
                log.append(f"SET {from_field} [{ds_from}] -> {to_field} [{sk_to}]")

    msg = (f"OK: lookup {app_window_ident}/{data_set_ident}/{field_ident} -> {lookup_window_ident} "
           f"(+{n_get} get, +{n_set} set).\n  " + "\n  ".join(log))
    if not sets:
        warnings.append("no Set mappings — the lookup will open but select nothing back; "
                        "add sets like [{'from_field': 'csXId'}, {'from_field': 'XDesc'}].")
    if warnings:
        msg += "\nWARNINGS:\n  - " + "\n  - ".join(warnings)
    return msg


# ---------------------------------------------------------------------------
# MCP tool descriptors + dispatcher
# ---------------------------------------------------------------------------

CS_TOOL_NAMES = {
    "deploy_sql_object",
    "cs_jsonsave",
    "add_cs_column",
    "add_ng_field",
    "get_cs_object_versions",
    "update_view_html",
    "ng_get_window_config",
    "ng_set_field_labels",
    "ng_set_layout_col",
    "ng_upsert_cols_group",
    "ng_set_stmsql",
    "ng_set_dataset_props",
    "rebuild_user_rights",
    "ai_tool_sync_params",
    "ng_add_lookup",
}


def tool_descriptors():
    from mcp.types import Tool

    return [
        Tool(
            name="deploy_sql_object",
            description=(
                "Deploy a procedure/function/view/trigger through the cs* versioning "
                "framework (csAddObjVer). Handles all pitfalls automatically: objectName "
                "WITHOUT dbo. prefix, @pv=latest version, fresh unique @v, 3-batch split "
                "(no GO), and orphan inProgress cleanup. `body` is the CREATE statement only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Object name WITHOUT schema, e.g. 'csFooBar'."},
                    "body": {"type": "string", "description": "The CREATE statement only (CREATE procedure dbo.<Name> ... )."},
                    "description": {"type": "string", "description": "Version description (>=3 chars)."},
                    "object_type": {"type": "string", "description": "procedure|function|view|trigger (informational)."},
                },
                "required": ["object_name", "body", "description"],
            },
        ),
        Tool(
            name="cs_jsonsave",
            description=(
                "Call any <T>JSONSave with a PARAMETRIZED @data payload (safe from "
                "multiline/diacritics JSON escaping bugs). Parses @response xml: returns "
                "OK on NULL, or the <messages> error text. rows = list of {_opr, ...columns}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "proc_name": {"type": "string", "description": "e.g. 'csNGAppWindowDataSetsFieldsJSONSave'."},
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Rows to upsert/delete; each must include _opr (I|U|D).",
                    },
                },
                "required": ["proc_name", "rows"],
            },
        ),
        Tool(
            name="add_cs_column",
            description=(
                "Add a column to a cs* table via csSysColumnsJSONSave + csSysTablesRebuild. "
                "Auto ColumnOrder (max+1), enforces all 12 ColumnDesc_XX, both rebuild params. "
                "Idempotent (skips if column exists)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "column_name": {"type": "string"},
                    "base_type": {"type": "string", "description": "int|bigint|nvarchar|bit|datetime|uniqueidentifier|numeric"},
                    "nullable": {"type": "boolean", "description": "Default true."},
                    "column_params": {"type": "string", "description": "e.g. '(200)', '(max)', '(18,6)'."},
                    "default_def": {"type": "string", "description": "e.g. '((0))' — NOTE: DefaultDef, not DefaultValue."},
                    "descriptions": {
                        "type": "object",
                        "description": "All 12 languages: EN,PL,DE,FR,ES,IT,NL,PT,RU,UK,SK,SE (real translations, never copy PL).",
                    },
                },
                "required": ["table_name", "column_name", "base_type", "descriptions"],
            },
        ),
        Tool(
            name="add_ng_field",
            description=(
                "Add a field to an NG window: csNGAppWindowDataSetsFields + LayoutsCols "
                "(+ optional <c-edit> injection into ins/upd viewHTML). Idempotent UPSERT; "
                "sets labelDataFieldIdent and a non-null width to avoid silent collapse."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "format_type": {"type": "string", "description": "integer|string|decimal|date|bool|..."},
                    "sql_base_type": {"type": "string", "description": "bigint|nvarchar|int|bit|datetime|uniqueidentifier"},
                    "label_pl": {"type": "string"},
                    "label_en": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "alias": {"type": "string", "description": "Join alias for the column (default 's')."},
                    "sql_column_params": {"type": "string", "description": "e.g. '(200)', '(max)'."},
                    "add_to_form": {"type": "boolean", "description": "Inject <c-edit> into ins/upd viewHTML. Default true."},
                    "width": {"type": "number", "description": "Grid column width (default 180)."},
                },
                "required": ["app_window_ident", "field_ident", "format_type", "sql_base_type", "label_pl", "label_en"],
            },
        ),
        Tool(
            name="get_cs_object_versions",
            description=(
                "List recent csSysObjVer history + inProgress state for a SQL object "
                "(objectName without dbo.). Use to diagnose deploy failures."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string"},
                    "top": {"type": "integer", "description": "Max rows (default 10)."},
                },
                "required": ["object_name"],
            },
        ),
        Tool(
            name="update_view_html",
            description=(
                "Sync a window/action .vue <template> into the DB viewHTML on demand "
                "(same effect as the husky pre-commit, without commit+push). "
                "component == app_window_ident -> csNGAppWindows.viewHTML; "
                "component '<dataSet>_<action>' (e.g. main_ins) -> csNGAppWindowDataSetsActions.viewHTML. "
                "Saves via the proper JSONSave so dataSets cache rebuilds. Provide file_path (preferred) or content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string", "description": "Window ident (= folder name), e.g. 'csMicroOrders'."},
                    "file_path": {"type": "string", "description": "Path to the .vue file (preferred)."},
                    "content": {"type": "string", "description": "Raw .vue source (alternative to file_path)."},
                    "component": {"type": "string", "description": "File base name w/o .vue. Default = file_path basename. == app_window_ident -> window; '<dataSet>_<action>' -> action."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard E4B58826-...)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_get_window_config",
            description=(
                "READ-ONLY compact dump of a full NG window configuration: window props, "
                "datasets, fields, layout cols, cols groups, actions, where fields, sort "
                "idents, key fields, lookup defs, links. Use INSTEAD of ad-hoc SELECTs. "
                "include_stmsql=true returns full stmSQL text (also useful as a snapshot "
                "before ng_set_stmsql)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Optional filter (default: all datasets)."},
                    "include_stmsql": {"type": "boolean", "description": "Include full stmSQL text (default false)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_set_field_labels",
            description=(
                "Update NG field (or whereField) labels: lab (edit label), col (grid header), "
                "watermark — per language. Handles the formatType pitfall (re-sends current "
                "formatType, otherwise JSONSave silently resets date/decimal/bool to 'string'). "
                "Writes ONLY provided languages — never copies PL to others (HARD RULE)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "labels": {
                        "type": "object",
                        "description": "{'PL': {'lab': '...', 'col': '...', 'watermark': '...'}, 'EN': {...}} — each key optional; langs: PL,EN,DE,FR,ES,NL,PT,RU,UK,IT.",
                    },
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "target": {"type": "string", "description": "'field' (default) or 'whereField' (filter panel)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "labels"],
            },
        ),
        Tool(
            name="ng_set_layout_col",
            description=(
                "Upsert a grid layout column: width, isVisible, ord, attach/detach column "
                "group. UPDATE uses minimal-U (avoids label re-validation trap for joined "
                "fields); INSERT auto-fills labelDataSetIdent/labelDataFieldIdent + non-null "
                "width (NULL width silently collapses the column). isVisible as int 1/0. "
                "Verifies the cols group exists. Grouped columns must be adjacent by ord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "width": {"type": "number", "description": "Multiple of 60 (120 code / 220 desc)."},
                    "is_visible": {"type": "boolean"},
                    "ord": {"type": "integer"},
                    "cols_group_ident": {"type": "string", "description": "Attach to this column group."},
                    "clear_cols_group": {"type": "boolean", "description": "Detach from its column group."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident"],
            },
        ),
        Tool(
            name="ng_upsert_cols_group",
            description=(
                "Upsert a grid column group (csNGAppWindowColsGroups; per-window, column "
                "names dataFieldColsGroupDesc_XX). Only provided languages are written "
                "(never copies PL). Then attach columns via ng_set_layout_col — grouped "
                "columns must be adjacent by ord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "cols_group_ident": {"type": "string"},
                    "descriptions": {
                        "type": "object",
                        "description": "{'PL': 'Obowiązuje', 'EN': 'Valid', ...} — langs: PL,EN,DE,FR,ES,NL,PT,RU,UK,IT,SE,SK.",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "cols_group_ident", "descriptions"],
            },
        ),
        Tool(
            name="ng_set_stmsql",
            description=(
                "Replace a dataset stmSQL with a MANDATORY sp_executesql test BEFORE saving: "
                "template runs twice (with dates — apostrophe bugs only show with non-null "
                "dates — and with empty where). Saved only if both pass. Snapshot the old "
                "stmSQL first via ng_get_window_config(include_stmsql=true). test=false only "
                "for templates needing extra params (@csItemsIdStr...) — verify manually then."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "stm_sql": {"type": "string", "description": "Full new stmSQL template text."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "test": {"type": "boolean", "description": "Run sp_executesql test before save (default true)."},
                    "test_where": {"type": "string", "description": "JSON for @where in the test (default has searchText+dates)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "stm_sql"],
            },
        ),
        Tool(
            name="ng_set_dataset_props",
            description=(
                "Update whitelisted csNGAppWindowDataSets columns (pageSize, pagingDisabled, "
                "notUseSort, dataFieldIdent4ReadOnly, loadDataImmediate, calcAggregates...) "
                "via minimal-U JSONSave. stmSQL NOT allowed here — use ng_set_stmsql "
                "(enforces the sp_executesql test). Booleans coerced to int 1/0."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "props": {"type": "object", "description": "{column: value} — whitelisted csNGAppWindowDataSets columns only."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "props"],
            },
        ),
        Tool(
            name="rebuild_user_rights",
            description=(
                "Rebuild per-user cache in csCompaniesUsrs (appMainMenuJSON, "
                "appWindowIdentsWithRights, warehousesRights). MANDATORY after changing menu "
                "items, NG windows or privileges — otherwise missing menu/buttons/spinner. "
                "On DEV narrow to the working company/user; full run needs confirm_all=true "
                "(slow). Direct UPDATE is correct (cache columns = HARD RULE 1 exception b)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cs_companies_id": {"type": "integer", "description": "Narrow to this company (recommended on DEV)."},
                    "cs_usr_id": {"type": "integer", "description": "Narrow to this user."},
                    "confirm_all": {"type": "boolean", "description": "Required for a full rebuild of ALL internal users."},
                },
            },
        ),
        Tool(
            name="ai_tool_sync_params",
            description=(
                "Sync the AI tool parameter registry (csAIAgentsToolsParams): upsert params "
                "(handles the U-batch-rollback gotcha — required fields always re-sent; "
                "typeJSON stored as plain string), heuristic diff registry vs $.keys in the "
                "procedure body, and a csSysGenManagedSync redeploy script for data/."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "csAIAgentsTools.name or SQLProcedure (with/without dbo.)."},
                    "params": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{name, type, description, isRequired, typeJSON}] — upserted by name. Omit to only get the diff report. typeJSON may be an object (auto-dumped to string).",
                    },
                    "generate_sync_script": {"type": "boolean", "description": "Return csSysGenManagedSync script for touched params (default true)."},
                },
                "required": ["tool_name"],
            },
        ),
        Tool(
            name="ng_add_lookup",
            description=(
                "Wire a lookup on an NG field: LookupDefs + Get/Set mappings. Handles all "
                "conventions: csAppNameSpacesGLookup on INSERT, sourceKind on every Get/Set "
                "row (silently ignored otherwise), source_kind='rows' (form) vs 'where' "
                "(filter panel host, auto closeKind=onLostFocus), auto Get host->searchText, "
                "Set dataSetIdentFrom = lookup's first dataset. Idempotent. Warns about "
                "missing onlyAsLookup/sort idents on the lookup window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string", "description": "Host field (Fields or WhereFields), usually *Desc."},
                    "lookup_window_ident": {"type": "string", "description": "e.g. csCustomersLookup."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "source_kind": {"type": "string", "description": "'rows' (form field, default) or 'where' (filter panel host)."},
                    "is_multi_select": {"type": "boolean"},
                    "close_kind": {"type": "string", "description": "e.g. 'onLostFocus' (auto for source_kind='where')."},
                    "search_get": {"type": "boolean", "description": "Auto-add Get: host field -> searchText (default true)."},
                    "gets": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Extra Get mappings: [{from_field XOR value, to_field, source_kind_from?, source_kind_to?}]. value -> constant filter (e.g. isSupplier=1).",
                    },
                    "sets": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Set mappings (lookup row -> host): [{from_field, to_field? (default =from_field), data_set_ident_from?, source_kind_to?}]. Typically Id + Desc pair.",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "lookup_window_ident"],
            },
        ),
    ]


def handle_tool(name: str, arguments: dict, connection_string: str) -> str:
    arguments = arguments or {}

    if name == "deploy_sql_object":
        return deploy_sql_object(
            connection_string,
            object_name=arguments.get("object_name", ""),
            body=arguments.get("body", ""),
            description=arguments.get("description", ""),
            object_type=arguments.get("object_type", "procedure"),
        )

    if name == "cs_jsonsave":
        return cs_jsonsave(
            connection_string,
            proc_name=arguments.get("proc_name", ""),
            rows=arguments.get("rows") or [],
        )

    if name == "add_cs_column":
        return add_cs_column(
            connection_string,
            table_name=arguments.get("table_name", ""),
            column_name=arguments.get("column_name", ""),
            base_type=arguments.get("base_type", ""),
            descriptions=arguments.get("descriptions") or {},
            nullable=arguments.get("nullable", True),
            column_params=arguments.get("column_params"),
            default_def=arguments.get("default_def"),
        )

    if name == "add_ng_field":
        return add_ng_field(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            format_type=arguments.get("format_type", ""),
            sql_base_type=arguments.get("sql_base_type", ""),
            label_pl=arguments.get("label_pl", ""),
            label_en=arguments.get("label_en", ""),
            data_set_ident=arguments.get("data_set_ident", "main"),
            alias=arguments.get("alias", "s"),
            sql_column_params=arguments.get("sql_column_params"),
            add_to_form=arguments.get("add_to_form", True),
            width=float(arguments.get("width") or 180.0),
        )

    if name == "get_cs_object_versions":
        return get_cs_object_versions(
            connection_string,
            object_name=arguments.get("object_name", ""),
            top=int(arguments.get("top") or 10),
        )

    if name == "update_view_html":
        return update_view_html(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            file_path=arguments.get("file_path"),
            content=arguments.get("content"),
            component=arguments.get("component"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_get_window_config":
        return ng_get_window_config(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident"),
            include_stmsql=bool(arguments.get("include_stmsql", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_field_labels":
        return ng_set_field_labels(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            labels=arguments.get("labels") or {},
            data_set_ident=arguments.get("data_set_ident") or "main",
            target=arguments.get("target") or "field",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_layout_col":
        width = arguments.get("width")
        ord_v = arguments.get("ord")
        return ng_set_layout_col(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            width=float(width) if width is not None else None,
            is_visible=arguments.get("is_visible"),
            ord=int(ord_v) if ord_v is not None else None,
            cols_group_ident=arguments.get("cols_group_ident"),
            clear_cols_group=bool(arguments.get("clear_cols_group", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_upsert_cols_group":
        return ng_upsert_cols_group(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            cols_group_ident=arguments.get("cols_group_ident", ""),
            descriptions=arguments.get("descriptions") or {},
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_stmsql":
        return ng_set_stmsql(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            stm_sql=arguments.get("stm_sql", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            test=bool(arguments.get("test", True)),
            test_where=arguments.get("test_where")
            or '{"searchText":"test","dateFrom":"2026-01-01","dateTo":"2026-12-31"}',
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_dataset_props":
        return ng_set_dataset_props(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            props=arguments.get("props") or {},
            data_set_ident=arguments.get("data_set_ident") or "main",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "rebuild_user_rights":
        cid = arguments.get("cs_companies_id")
        uid = arguments.get("cs_usr_id")
        return rebuild_user_rights(
            connection_string,
            cs_companies_id=int(cid) if cid is not None else None,
            cs_usr_id=int(uid) if uid is not None else None,
            confirm_all=bool(arguments.get("confirm_all", False)),
        )

    if name == "ai_tool_sync_params":
        return ai_tool_sync_params(
            connection_string,
            tool_name=arguments.get("tool_name", ""),
            params=arguments.get("params"),
            generate_sync_script=bool(arguments.get("generate_sync_script", True)),
        )

    if name == "ng_add_lookup":
        return ng_add_lookup(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            lookup_window_ident=arguments.get("lookup_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            source_kind=arguments.get("source_kind") or "rows",
            is_multi_select=bool(arguments.get("is_multi_select", False)),
            close_kind=arguments.get("close_kind"),
            search_get=bool(arguments.get("search_get", True)),
            gets=arguments.get("gets"),
            sets=arguments.get("sets"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    raise ValueError(f"Unknown cs tool: {name}")
