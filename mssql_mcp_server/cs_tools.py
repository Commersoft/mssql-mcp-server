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
- describe            : compact schema of a DB object — table/view columns (type/null/PK/FK)
                        or procedure/function parameters (avoids 'Invalid column name' guessing).
- sql_grep           : case-insensitive substring search over SQL object bodies
                        (object:line:content, like Grep over files).
- ng_preview_dataset  : dry-run an NG dataset — expand /*FIELDS*/ + stmSQL into the real data
                        SELECT and execute it (top N), catching reserved-word/invalid-column/
                        @var runtime errors the config-only validator misses.
- ng_bulk_layout      : bulk upsert grid layout columns (visible/ord/width/group) in one call.
- ng_register_translates : register gT() idents (reuse csTranslate by content/GUID or create).
- ng_add_linked_window : master->detail link (csNGAppWindowsLinks + LinksFields + optional
                        tabIdent where-field) in one call.
- ng_add_filter       : filter-panel where-field + optional lookup + watermark in one call.

All tools are WRITE-CAPABLE (except describe/sql_grep/ng_preview_dataset — read-only). They run against the same connection_string as the
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
    is_translate: bool = False,
    before_translate: Optional[str] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Add a field to an NG window: csNGAppWindowDataSetsFields + LayoutsCols,
    and (optionally) inject <c-edit> into the ins/upd action viewHTML.

    Enforces: idempotent UPSERT, layout col requires labelDataSetIdent/labelDataFieldIdent
    and a non-null width (else the column silently collapses).

    is_translate=True -> sets isTranslate=1 + dataFieldIdentBeforeTranslate (defaults to
    '<field_ident>_'; a missing trailing '_' is auto-appended — the /*FIELDS*/ builder
    concatenates it with the language suffix, so 'WarehouseDesc' would expand to the
    non-existent column 'WarehouseDescPL').
    """
    log: List[str] = []
    if before_translate and not is_translate:
        return ("ERROR: before_translate given but is_translate is false — the /*FIELDS*/ builder "
                "only uses dataFieldIdentBeforeTranslate when isTranslate=1 (would emit alias.field_ident).")
    bt_prefix: Optional[str] = None
    if is_translate:
        bt_prefix = before_translate or field_ident
        if not bt_prefix.endswith("_"):
            bt_prefix += "_"
            log.append(f"before_translate auto-suffixed to '{bt_prefix}'")
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
                "isTranslate": 1 if is_translate else 0,
                "addToSelect": 1,
            }
            if bt_prefix:
                field_row["dataFieldIdentBeforeTranslate"] = bt_prefix
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
# 16. describe — table columns (type/null/PK/FK) or proc/function parameters
# ---------------------------------------------------------------------------

def describe(connection_string: str, object_name: str) -> str:
    """Compact schema of a DB object: columns (type/null/PK/FK) for a table/view,
    or the parameter list for a procedure/function. Eliminates the round-trips
    of guessing column names (repeated 'Invalid column name')."""
    name = object_name.split(".")[-1].strip("[]")
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                "select o.type, o.type_desc from sys.objects o where o.object_id = object_id(?)",
                name,
            ).fetchone()
            if not row:
                return f"Error: object '{name}' not found."
            otype = (row[0] or "").strip()

            if otype in ("U", "V"):
                cur.execute(
                    "select c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, "
                    "  isnull(pk.is_pk,0), fk.ref "
                    "from sys.columns c "
                    "  join sys.types t on t.user_type_id = c.user_type_id "
                    "  outer apply (select max(cast(i.is_primary_key as int)) is_pk "
                    "    from sys.index_columns xc join sys.indexes i on i.object_id=xc.object_id and i.index_id=xc.index_id "
                    "    where xc.object_id=c.object_id and xc.column_id=c.column_id and i.is_primary_key=1) pk "
                    "  outer apply (select top 1 rt.name + N'.' + rc.name ref "
                    "    from sys.foreign_key_columns f "
                    "      join sys.objects rt on rt.object_id=f.referenced_object_id "
                    "      join sys.columns rc on rc.object_id=f.referenced_object_id and rc.column_id=f.referenced_column_id "
                    "    where f.parent_object_id=c.object_id and f.parent_column_id=c.column_id) fk "
                    "where c.object_id = object_id(?) order by c.column_id",
                    name,
                )
                lines = []
                for r in cur.fetchall():
                    cname, ctype, mlen, prec, scale, nullable, ident, is_pk, ref = r
                    typ = ctype
                    if ctype in ("nvarchar", "nchar", "varchar", "char", "varbinary", "binary"):
                        typ += "(max)" if mlen == -1 else f"({mlen // 2 if ctype.startswith('n') and mlen != -1 else mlen})"
                    elif ctype in ("decimal", "numeric"):
                        typ += f"({prec},{scale})"
                    flags = []
                    if is_pk:
                        flags.append("PK")
                    if ident:
                        flags.append("identity")
                    flags.append("NULL" if nullable else "NOT NULL")
                    if ref:
                        flags.append(f"-> {ref}")
                    lines.append(f"  {cname} | {typ} {' '.join(flags)}")
                if not lines:
                    return f"{name} ({row[1]}): no columns."
                return f"TABLE {name} ({len(lines)} cols):\n" + "\n".join(lines)

            # procedure / function
            cur.execute(
                "select p.name, t.name, p.max_length, p.precision, p.scale, p.is_output "
                "from sys.parameters p join sys.types t on t.user_type_id = p.user_type_id "
                "where p.object_id = object_id(?) order by p.parameter_id",
                name,
            )
            lines = []
            for r in cur.fetchall():
                pname, ptype, mlen, prec, scale, is_out = r
                typ = ptype
                if ptype in ("nvarchar", "nchar", "varchar", "char"):
                    typ += "(max)" if mlen == -1 else f"({mlen // 2 if ptype.startswith('n') and mlen != -1 else mlen})"
                elif ptype in ("decimal", "numeric"):
                    typ += f"({prec},{scale})"
                lines.append(f"  {pname or '(returns)'} {typ}{' out' if is_out else ''}")
            head = f"{row[1]} {name} ({len(lines)} params):"
            return head + ("\n" + "\n".join(lines) if lines else " (no parameters)")


# ---------------------------------------------------------------------------
# 17. sql_grep — grep over SQL object bodies (object:line:content)
# ---------------------------------------------------------------------------

def sql_grep(connection_string: str, pattern: str, name_like: Optional[str] = None,
             top: int = 100) -> str:
    """Case-insensitive substring search over sys.sql_modules bodies. Returns
    object:line:trimmed-line hits (like Grep over files). Optional name_like
    narrows candidate objects. Replaces ad-hoc LIKE-on-sql_modules + substring."""
    if not pattern:
        return "Error: pattern is required."
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            sql = ("select o.name, o.type, m.definition from sys.sql_modules m "
                   "join sys.objects o on o.object_id = m.object_id "
                   "where m.definition like ? escape '\\'")
            like = "%" + pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params = [like]
            if name_like:
                sql += " and o.name like ?"
                params.append("%" + name_like + "%")
            sql += " order by o.name"
            cur.execute(sql, *params)
            needle = pattern.lower()
            hits: List[str] = []
            truncated = False
            for oname, otype, definition in cur.fetchall():
                for i, line in enumerate((definition or "").splitlines(), start=1):
                    if needle in line.lower():
                        hits.append(f"{oname}:{i}: {line.strip()[:200]}")
                        if len(hits) >= top:
                            truncated = True
                            break
                if truncated:
                    break
    if not hits:
        return f"No matches for '{pattern}'" + (f" in objects like '{name_like}'." if name_like else ".")
    out = "\n".join(hits)
    if truncated:
        out += f"\n... (truncated at {top} hits — narrow with name_like or a longer pattern)"
    return out


# ---------------------------------------------------------------------------
# 18. ng_preview_dataset — run the REAL NG data SELECT (dry-run), catch runtime SQL errors
# ---------------------------------------------------------------------------

def ng_preview_dataset(connection_string: str, app_window_ident: str,
                       data_set_ident: str = "main", where: Optional[str] = None,
                       top: int = 5, cs_companies_id: Optional[int] = None,
                       cs_usr_id: Optional[int] = None,
                       namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Dry-run an NG dataset the way the runtime does: expand /*FIELDS*/ + stmSQL
    template into the real data SELECT and EXECUTE it (top N, for json). Catches
    reserved-word/invalid-column/@var errors that csNGValidateWindowForAI (config-only)
    misses. Returns the generated SQL + first rows, or the SQL + the exact error."""
    top = max(1, min(int(top or 5), 50))
    cid = str(int(cs_companies_id)) if cs_companies_id is not None else "(select min(csCompaniesId) from dbo.csCompanies)"
    uid = str(int(cs_usr_id)) if cs_usr_id is not None else "(select min(csUsrId) from dbo.csUsr)"
    ns = namespace_g.replace("'", "''")
    aw = app_window_ident.replace("'", "''")
    ds = data_set_ident.replace("'", "''")

    build = f"""set nocount on;
declare @stmSQL nvarchar(max), @stmSQLPrepare nvarchar(max), @fields nvarchar(max),
 @stmSQLOut nvarchar(max)=N'', @prepOut nvarchar(max)=N'', @paramsPrepare nvarchar(max), @useManual bit,
 @ch nvarchar(2)=char(13)+char(10), @suffix nvarchar(2)=N'PL',
 @cid bigint, @uid bigint, @cidStr nvarchar(30), @uidStr nvarchar(30),
 @where nvarchar(max)=?, @pd nvarchar(max)=dbo.csFnNGAPIGetDataStmParamDef();
set @cid = {cid};
set @uid = {uid};
set @cidStr = cast(@cid as nvarchar(30));
set @uidStr = cast(@uid as nvarchar(30));
select @stmSQL=stmSQL,@stmSQLPrepare=stmSQLPrepare,@fields=fields from dbo.csNGAppWindowDataSets with(nolock)
 where csAppNameSpacesG=N'{ns}' and appWindowIdent=N'{aw}' and dataSetIdent=N'{ds}';
if @stmSQL is null begin select cast(null as nvarchar(max)) g, cast(null as nvarchar(max)) p, N'DATASET_NOT_FOUND' e; return; end;
set @fields=replace(isnull(@fields,N''),N'/*LANGUAGE_SUFFIX*/',@suffix);
set @stmSQL=replace(replace(@stmSQL,N'/*FIELDS*/',@fields),N'/*LANGUAGE_SUFFIX*/',@suffix);
if @stmSQLPrepare is not null and @stmSQLPrepare<>N'' begin
 set @stmSQLPrepare=replace(@stmSQLPrepare,N'/*LANGUAGE_SUFFIX*/',@suffix);
 exec sp_executesql @stmSQLPrepare,@pd,@sessionId=null,@route=null,@kind=null,@ch=@ch,@pageNo=1,@pageSize={top},
  @where=@where,@whereLists=null,@layout=null,@order=null,@params=null,@csCompaniesId=@cid,@csUsrId=@uid,
  @csB2BPortalsId=null,@csNGMasterMenuDefId=null,@csAppMainMenusId=null,@languageSuffix=@suffix,@paramsJSON=null,
  @paramsJSONAdd=null,@paramsJSONSession=null,@csCompaniesIdStr=@cidStr,@csUsrIdStr=@uidStr,
  @csB2BPortalsIdStr=null,@csNGMasterMenuDefIdStr=null,@csAppMainMenusIdStr=null,@stmSQLOut=@prepOut out,
  @selectedRows=null,@filter=null,@advancedFilters=null,@isRefreshOneRecord=0,@csAppNameSpacesG=N'{ns}',
  @appWindowIdent=N'{aw}',@dataSetIdent=N'{ds}',@paramsPrepare=@paramsPrepare out,@useManualAggregateFields=@useManual out;
end;
exec sp_executesql @stmSQL,@pd,@sessionId=null,@route=null,@kind=2,@ch=@ch,@pageNo=1,@pageSize={top},
 @where=@where,@whereLists=null,@layout=null,@order=null,@params=null,@csCompaniesId=@cid,@csUsrId=@uid,
 @csB2BPortalsId=null,@csNGMasterMenuDefId=null,@csAppMainMenusId=null,@languageSuffix=@suffix,@paramsJSON=null,
 @paramsJSONAdd=null,@paramsJSONSession=null,@csCompaniesIdStr=@cidStr,@csUsrIdStr=@uidStr,
 @csB2BPortalsIdStr=null,@csNGMasterMenuDefIdStr=null,@csAppMainMenusIdStr=null,@stmSQLOut=@stmSQLOut out,
 @selectedRows=null,@filter=null,@advancedFilters=null,@isRefreshOneRecord=0,@csAppNameSpacesG=N'{ns}',
 @appWindowIdent=N'{aw}',@dataSetIdent=N'{ds}',@paramsPrepare=@paramsPrepare out,@useManualAggregateFields=@useManual out;
select @stmSQLOut g, @prepOut p, cast(null as nvarchar(max)) e;"""

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(build, where)
                row = cur.fetchone()
            except Exception as e:  # noqa: BLE001
                return f"BUILD ERROR (stmSQL template failed to compose):\n{e}"
            if not row or (row[2] == "DATASET_NOT_FOUND"):
                return f"Error: dataset {app_window_ident}/{data_set_ident} not found in namespace."
            generated = row[0] or ""
            prep = row[1] or ""
            if not generated.strip():
                return "Error: stmSQL produced empty SQL (dataset has no stmSQL?)."

            run = (f"set nocount on;\n{prep}\nselect top {top} * from (\n{generated}\n) __dry "
                   "for json path, include_null_values")
            try:
                cur.execute(run)
                res = cur.fetchone()
                rows_json = res[0] if res and res[0] else "[]"
            except Exception as e:  # noqa: BLE001
                return (f"RUNTIME SQL ERROR — the window would fail with this on open:\n{e}\n\n"
                        f"--- generated data SQL ---\n{generated}")

    preview = rows_json if len(rows_json) <= 4000 else rows_json[:4000] + " …(truncated)"
    return (f"OK: dataset {app_window_ident}/{data_set_ident} executes cleanly (top {top}).\n"
            f"--- rows (json) ---\n{preview}\n\n--- generated data SQL ---\n{generated}")


# ---------------------------------------------------------------------------
# 19. ng_bulk_layout — set visibility/order/width/group for many columns in one call
# ---------------------------------------------------------------------------

def ng_bulk_layout(connection_string: str, app_window_ident: str, columns: Sequence[dict],
                   data_set_ident: str = "main", layout_ident: str = "default",
                   namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Bulk upsert of grid layout columns. Each item: {field, visible?, ord?, width?, group?}.
    Same pitfalls as ng_set_layout_col (minimal-U, int isVisible, non-null width on INSERT,
    group existence check) but one connection + one MCP call for the whole grid."""
    if not columns:
        return "Error: columns list is required."
    log: List[str] = []
    warnings: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            known_groups: dict = {}
            for item in columns:
                field = item.get("field")
                if not field:
                    return f"Error: each column needs 'field': {item}"
                changes: dict = {}
                if "width" in item and item["width"] is not None:
                    changes["width"] = float(item["width"])
                if "visible" in item and item["visible"] is not None:
                    changes["isVisible"] = _as_int(item["visible"])
                if "ord" in item and item["ord"] is not None:
                    changes["ord"] = int(item["ord"])
                if "group" in item:
                    grp = item["group"]
                    changes["colsGroupIdent"] = grp if grp else None
                    if grp and grp not in known_groups:
                        known_groups[grp] = _exec_scalar(
                            cur,
                            "select 1 from dbo.csNGAppWindowColsGroups with(nolock) "
                            "where csAppNameSpacesG=? and appWindowIdent=? and colsGroupIdent=?",
                            namespace_g, app_window_ident, grp,
                        )
                        if not known_groups[grp]:
                            warnings.append(f"cols group '{grp}' does not exist (create via ng_upsert_cols_group).")
                if not changes:
                    continue
                cur.execute(
                    "select csNGAppWindowDataSetsLayoutsColsId, csNGAppWindowDataSetsLayoutsColsG "
                    "from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and layoutIdent=? and dataFieldIdent=?",
                    namespace_g, app_window_ident, data_set_ident, layout_ident, field,
                )
                lc = cur.fetchone()
                if lc:
                    rec = {"_opr": "U",
                           "csNGAppWindowDataSetsLayoutsColsId": int(lc[0]),
                           "csNGAppWindowDataSetsLayoutsColsG": str(lc[1]).upper()}
                    rec.update(changes)
                    mode = "U"
                else:
                    fld = _exec_scalar(
                        cur,
                        "select 1 from dbo.csNGAppWindowDataSetsFields with(nolock) "
                        "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                        namespace_g, app_window_ident, data_set_ident, field,
                    )
                    if not fld:
                        warnings.append(f"field '{field}' not found — skipped (add via add_ng_field).")
                        continue
                    rec = {"_opr": "I",
                           "csNGAppWindowDataSetsLayoutsColsG": _new_guid(),
                           "csAppNameSpacesG": namespace_g,
                           "appWindowIdent": app_window_ident,
                           "dataSetIdent": data_set_ident,
                           "layoutIdent": layout_ident,
                           "dataFieldIdent": field,
                           "labelDataSetIdent": data_set_ident,
                           "labelDataFieldIdent": field}
                    rec.update(changes)
                    if "width" not in changes:
                        rec["width"] = 120.0
                    mode = "I"
                resp = _jsonsave(cur, "csNGAppWindowDataSetsLayoutsColsJSONSave", [rec])
                if resp:
                    return f"JSONSave WARNING at field '{field}' (after {len(log)} ok):\n{resp}"
                log.append(f"{mode} {field} {changes}")
    msg = f"OK: ng_bulk_layout {app_window_ident}/{data_set_ident} — {len(log)} column(s) applied."
    if log:
        msg += "\n  " + "\n  ".join(log)
    if warnings:
        msg += "\nWARNINGS:\n  - " + "\n  - ".join(warnings)
    return msg


# ---------------------------------------------------------------------------
# 20. ng_register_translates — register gT idents (reuse csTranslate by content)
# ---------------------------------------------------------------------------

def ng_register_translates(connection_string: str, app_window_ident: str,
                           translates: Sequence[dict],
                           namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Register gT() idents on a window (csNGAppWindowTranslates). Each item:
    {ident, cs_translate_g?} to reuse an existing translation, OR {ident, PL, EN, ...}
    to reuse-by-content (match Content_PL+Content_EN) or create a new csTranslate.
    Idempotent on (appWindowIdent, translateIdent)."""
    if not translates:
        return "Error: translates list is required."
    log: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            for item in translates:
                ident = item.get("ident")
                if not ident:
                    return f"Error: each item needs 'ident': {item}"
                tg = item.get("cs_translate_g")
                texts = {k: v for k, v in item.items() if k in NG_COLSGROUP_LANGS}
                if not tg:
                    pl = texts.get("PL")
                    en = texts.get("EN")
                    if pl:
                        tg = _exec_scalar(
                            cur,
                            "select top 1 csTranslateG from dbo.csTranslate with(nolock) "
                            "where Content_PL = ? and isnull(Content_EN,N'') = isnull(?,N'')",
                            pl, en,
                        )
                    if not tg:
                        if not pl:
                            return f"Error: '{ident}' has no cs_translate_g and no PL text to create/reuse."
                        tg = _new_guid()
                        trow = {"_opr": "I", "csTranslateG": tg}
                        for lang, val in texts.items():
                            trow[f"Content_{lang}"] = val
                        resp = _jsonsave(cur, "csTranslateJSONSave", [trow])
                        if resp:
                            return f"csTranslateJSONSave WARNING for '{ident}':\n{resp}"
                        action = "created csTranslate"
                    else:
                        action = "reused by content"
                else:
                    tg = str(tg).upper()
                    action = "reused by GUID"
                existing = _exec_scalar(
                    cur,
                    "select csNGAppWindowTranslatesG from dbo.csNGAppWindowTranslates with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and translateIdent=?",
                    namespace_g, app_window_ident, ident,
                )
                link = {
                    "_opr": "U" if existing else "I",
                    "csNGAppWindowTranslatesG": str(existing).upper() if existing else _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": app_window_ident,
                    "translateIdent": ident,
                    "csTranslateG": str(tg).upper(),
                }
                resp = _jsonsave(cur, "csNGAppWindowTranslatesJSONSave", [link])
                if resp:
                    return f"csNGAppWindowTranslatesJSONSave WARNING for '{ident}':\n{resp}"
                log.append(f"{ident} -> {tg} ({action}, {'U' if existing else 'I'})")
    return f"OK: registered {len(log)} translate ident(s) on {app_window_ident}.\n  " + "\n  ".join(log)


# ---------------------------------------------------------------------------
# 21. ng_add_linked_window — master->detail link (bottom/side panel) in one call
# ---------------------------------------------------------------------------

def ng_add_linked_window(connection_string: str, app_window_ident_from: str,
                         app_window_ident_to: str, placement: str,
                         map_fields: Sequence[dict], ord: int = 1,
                         labels: Optional[dict] = None, tab_default: Optional[str] = None,
                         namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Link a detail window to a master (csNGAppWindowsLinks + LinksFields). map_fields:
    [{from, to?}] (from = master main field, to = detail where-field; default to=from).
    placement: 'bottom-panel'|'outer-side-panel'|'side-panel'|'inner-side-panel'.
    Optional tab_default sets the master where-field 'tabIdent-<placement>' (needed when a
    placement holds several tabs). labels: {'PL':..,'EN':..} for the tab caption. The
    linkedWindows cache rebuilds automatically via csNGAppWindowsLinksJSONSave."""
    if not map_fields:
        return "Error: map_fields is required (at least the master key mapping)."
    labels = labels or {}
    log: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select csNGAppWindowsLinksG from dbo.csNGAppWindowsLinks with(nolock) "
                "where csAppNameSpacesGFrom=? and appWindowIdentFrom=? and csAppNameSpacesGTo=? and appWindowIdentTo=?",
                namespace_g, app_window_ident_from, namespace_g, app_window_ident_to,
            )
            ex = cur.fetchone()
            link = {
                "_opr": "U" if ex else "I",
                "csNGAppWindowsLinksG": str(ex[0]).upper() if ex else _new_guid(),
                "csAppNameSpacesGFrom": namespace_g,
                "appWindowIdentFrom": app_window_ident_from,
                "csAppNameSpacesGTo": namespace_g,
                "appWindowIdentTo": app_window_ident_to,
                "placement": placement,
                "ord": int(ord),
            }
            for lang, val in labels.items():
                if lang in NG_COLSGROUP_LANGS:
                    link[f"appWindowLinkDesc_{lang}"] = val
            resp = _jsonsave(cur, "csNGAppWindowsLinksJSONSave", [link])
            if resp:
                return f"csNGAppWindowsLinksJSONSave WARNING:\n{resp}"
            log.append(f"LINK {app_window_ident_from} -> {app_window_ident_to} [{placement}] ord={ord}")

            for m in map_fields:
                ff = m.get("from")
                if not ff:
                    return f"Error: map_fields item needs 'from': {m}"
                ft = m.get("to") or ff
                exf = _exec_scalar(
                    cur,
                    "select csNGAppWindowsLinksFieldsG from dbo.csNGAppWindowsLinksFields with(nolock) "
                    "where csAppNameSpacesGFrom=? and appWindowIdentFrom=? and csAppNameSpacesGTo=? and appWindowIdentTo=? "
                    "and dataFieldIdentFrom=? and dataFieldIdentTo=?",
                    namespace_g, app_window_ident_from, namespace_g, app_window_ident_to, ff, ft,
                )
                if exf:
                    log.append(f"  MAP {ff} -> {ft} exists (skip)")
                    continue
                rec = {
                    "_opr": "I",
                    "csNGAppWindowsLinksFieldsG": _new_guid(),
                    "csAppNameSpacesGFrom": namespace_g,
                    "appWindowIdentFrom": app_window_ident_from,
                    "csAppNameSpacesGTo": namespace_g,
                    "appWindowIdentTo": app_window_ident_to,
                    "dataSetIdentFrom": m.get("data_set_ident_from") or "main",
                    "sourceKindFrom": m.get("source_kind_from") or "rows",
                    "dataFieldIdentFrom": ff,
                    "dataSetIdentTo": m.get("data_set_ident_to") or "main",
                    "dataFieldIdentTo": ft,
                }
                resp = _jsonsave(cur, "csNGAppWindowsLinksFieldsJSONSave", [rec])
                if resp:
                    return f"csNGAppWindowsLinksFieldsJSONSave WARNING (map {ff}):\n{resp}"
                log.append(f"  MAP {ff} -> {ft}")

            if tab_default:
                tab_field = f"tabIdent-{placement}"
                exw = _exec_scalar(
                    cur,
                    "select csNGAppWindowDataSetsWhereFieldsG from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=N'main' and dataFieldIdent=?",
                    namespace_g, app_window_ident_from, tab_field,
                )
                wrec = {
                    "_opr": "U" if exw else "I",
                    "csNGAppWindowDataSetsWhereFieldsG": str(exw).upper() if exw else _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": app_window_ident_from,
                    "dataSetIdent": "main",
                    "dataFieldIdent": tab_field,
                    "formatType": "string",
                    "SQLBaseType": "nvarchar",
                    "SQLColumnParams": "(max)",
                    "dataFieldValueDef": tab_default,
                    "isActive": 1,
                    "notUseForGetData": 1,
                }
                resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [wrec])
                if resp:
                    return f"tabIdent whereField WARNING:\n{resp}"
                log.append(f"  TAB where-field {tab_field} = {tab_default} ({'U' if exw else 'I'})")

    return f"OK: linked window.\n  " + "\n  ".join(log)


# ---------------------------------------------------------------------------
# 22. ng_add_filter — where-field (+ optional lookup + watermark) in one call
# ---------------------------------------------------------------------------

def ng_add_filter(connection_string: str, app_window_ident: str, field_ident: str,
                  format_type: str, sql_base_type: str, data_set_ident: str = "main",
                  sql_column_params: Optional[str] = None, value_def: Optional[str] = None,
                  not_use_for_get_data: Optional[bool] = None, ord: Optional[int] = None,
                  label_pl: Optional[str] = None, label_en: Optional[str] = None,
                  watermark: Optional[dict] = None, lookup_window_ident: Optional[str] = None,
                  lookup_sets: Optional[Sequence[dict]] = None,
                  lookup_gets: Optional[Sequence[dict]] = None,
                  namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Create a filter-panel where-field (+ optional lookup wiring + watermark) in one call.
    - host string fields (lookups) default notUseForGetData=1 (they only drive searchText/set);
    - watermark: {'PL':..,'EN':..,..} — needed for :showLabel=false fields (empty-field hint);
    - lookup_window_ident + lookup_sets wires ng_add_lookup with source_kind='where'."""
    log: List[str] = []
    is_lookup_host = lookup_window_ident is not None
    if not_use_for_get_data is None:
        not_use_for_get_data = is_lookup_host and format_type == "string"

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            ex = _exec_scalar(
                cur,
                "select csNGAppWindowDataSetsWhereFieldsG from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )
            if ord is None:
                ord = int(_exec_scalar(
                    cur,
                    "select isnull(max(ord),0)+1 from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                    namespace_g, app_window_ident, data_set_ident,
                ) or 1)
            rec = {
                "_opr": "U" if ex else "I",
                "csNGAppWindowDataSetsWhereFieldsG": str(ex).upper() if ex else _new_guid(),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "dataSetIdent": data_set_ident,
                "dataFieldIdent": field_ident,
                "formatType": format_type,
                "SQLBaseType": sql_base_type,
                "isActive": 1,
                "ord": int(ord),
            }
            if sql_column_params:
                rec["SQLColumnParams"] = sql_column_params
            if value_def is not None:
                rec["dataFieldValueDef"] = value_def
            if not_use_for_get_data:
                rec["notUseForGetData"] = 1
            if label_pl:
                rec["dataFieldLabDesc_PL"] = label_pl
            if label_en:
                rec["dataFieldLabDesc_EN"] = label_en
            resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [rec])
            if resp:
                return f"whereField JSONSave WARNING:\n{resp}"
            log.append(f"WHERE-FIELD {field_ident} ({format_type}/{sql_base_type}, {'U' if ex else 'I'})")

            if watermark:
                bad = [l for l in watermark if l not in NG_LABEL_LANGS]
                if bad:
                    return f"Error: watermark has unsupported lang(s): {', '.join(bad)}."
                wrec = {
                    "_opr": "U",
                    "csNGAppWindowDataSetsWhereFieldsId": None,
                    "csNGAppWindowDataSetsWhereFieldsG": None,
                    "formatType": format_type,
                }
                cur.execute(
                    "select csNGAppWindowDataSetsWhereFieldsId, csNGAppWindowDataSetsWhereFieldsG "
                    "from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                    namespace_g, app_window_ident, data_set_ident, field_ident,
                )
                wr = cur.fetchone()
                wrec["csNGAppWindowDataSetsWhereFieldsId"] = int(wr[0])
                wrec["csNGAppWindowDataSetsWhereFieldsG"] = str(wr[1]).upper()
                for lang, val in watermark.items():
                    wrec[f"dataFieldWatermarkDesc_{lang}"] = val
                resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [wrec])
                if resp:
                    return f"watermark JSONSave WARNING:\n{resp}"
                log.append(f"  watermark ({', '.join(sorted(watermark))})")

    if is_lookup_host:
        lk = ng_add_lookup(
            connection_string,
            app_window_ident=app_window_ident,
            field_ident=field_ident,
            lookup_window_ident=lookup_window_ident,
            data_set_ident=data_set_ident,
            source_kind="where",
            gets=lookup_gets,
            sets=lookup_sets,
            namespace_g=namespace_g,
        )
        log.append("  " + lk.replace("\n", "\n  "))

    return "OK: ng_add_filter.\n  " + "\n  ".join(log)


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


# ---------------------------------------------------------------------------
# 24. ng_add_action — dataset action + fields + privileges + optional rebuild
# ---------------------------------------------------------------------------

def ng_add_action(
    connection_string: str,
    app_window_ident: str,
    action_ident: str,
    data_set_ident: str = "main",
    labels: Optional[dict] = None,
    sql_name: Optional[str] = None,
    kind: Optional[str] = None,
    crud: Optional[str] = None,
    show_view: Optional[bool] = None,
    view_html: Optional[str] = None,
    ord: Optional[int] = None,
    hide_when_empty: Optional[bool] = None,
    show_confirmation: Optional[bool] = None,
    add_current_row: Optional[bool] = None,
    add_where: Optional[bool] = None,
    ref_kind: Optional[int] = None,
    close_after_exec: Optional[bool] = None,
    is_auto: Optional[bool] = None,
    fields: Optional[Sequence[dict]] = None,
    extra: Optional[dict] = None,
    wire_privileges: bool = True,
    rebuild: bool = False,
    cs_companies_id: Optional[int] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Register/update an NG dataset action (csNGAppWindowDataSetsActions) with the
    framework conventions handled:
      - crud='ins'|'upd'|'del' presets the standard auto-action flags
        (ins: isRowsInsert=1/ord=1; upd: isRowsUpdate=1/hideWhenEmpty=1;
         del: isRowsDelete=1/hideWhenEmpty=1/showConfirmation=1); refKind=1, isAuto=1.
      - custom action (no crud): isAuto=0, ord=max+1, position='default' (NOT NULL).
      - labels {PL,EN,...} -> actionDesc_* (10 languages).
      - fields: [{dataFieldIdent, dataFieldValueDef?, dataFieldIdentForNewRowValue?}]
        -> csNGAppWindowDataSetsActionsFields.
      - wire_privileges: for every granular window privilege (hasRightsAllDataSets=0
        with a dataset row hasRightsAllActions=0) inserts the ActionsPrivileges row —
        WITHOUT this the new action stays invisible for those users.
      - view_html: stores the action form template (showView=1 implied).
    After adding an action REMEMBER: rights cache rebuild (rebuild=True +
    cs_companies_id, or rebuild_user_rights tool) or the button will not appear.
    """
    aw = (app_window_ident or "").strip()
    act = (action_ident or "").strip()
    if not aw or not act:
        return "Error: app_window_ident and action_ident are required."
    if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", act):
        return (f"Error: actionIdent '{act}' — framework allows only letters+digits "
                "(no underscore: 'Pole [Symbol] nie może zawierać znaku _').")
    if crud and crud not in ("ins", "upd", "del"):
        return "Error: crud must be one of ins|upd|del (or omitted for a custom action)."

    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            ds = _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                namespace_g, aw, data_set_ident,
            )
            if not ds:
                return f"Error: dataset {aw}.{data_set_ident} not found (namespace {namespace_g})."

            cur.execute(
                "select csNGAppWindowDataSetsActionsId, csNGAppWindowDataSetsActionsG "
                "from dbo.csNGAppWindowDataSetsActions with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and actionIdent=?",
                namespace_g, aw, data_set_ident, act,
            )
            existing = cur.fetchone()

            row: dict = {
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": aw,
                "dataSetIdent": data_set_ident,
                "actionIdent": act,
            }
            if existing:
                row["_opr"] = "U"
                row["csNGAppWindowDataSetsActionsId"] = int(existing[0])
                row["csNGAppWindowDataSetsActionsG"] = str(existing[1]).upper()
            else:
                row["_opr"] = "I"
                row["csNGAppWindowDataSetsActionsG"] = _new_guid()
                # CRUD presets (pattern: csStatuses ins/upd/del)
                presets = {
                    "ins": {"ord": 1, "isAuto": 1, "isRowsInsert": 1, "hideWhenEmpty": 0, "refKind": 1},
                    "upd": {"ord": 2, "isAuto": 1, "isRowsUpdate": 1, "hideWhenEmpty": 1, "refKind": 1},
                    "del": {"ord": 3, "isAuto": 1, "isRowsDelete": 1, "hideWhenEmpty": 1,
                            "refKind": 1, "showConfirmation": 1},
                }
                if crud:
                    row.update(presets[crud])
                else:
                    max_ord = _exec_scalar(
                        cur,
                        "select isnull(max(ord),0) from dbo.csNGAppWindowDataSetsActions with(nolock) "
                        "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                        namespace_g, aw, data_set_ident,
                    )
                    row.setdefault("ord", int(max_ord) + 1)
                    row.setdefault("isAuto", 0)
                    row.setdefault("hideWhenEmpty", 1)
                    row.setdefault("refKind", 1)
                row.setdefault("position", "default")  # NOT NULL

            # explicit overrides
            overrides = {
                "ord": ord, "kind": kind, "SQLName": sql_name,
                "hideWhenEmpty": hide_when_empty, "showConfirmation": show_confirmation,
                "addCurrentRow": add_current_row, "addWhere": add_where,
                "refKind": ref_kind, "closeAfterExec": close_after_exec,
                "isAuto": is_auto, "showView": show_view,
            }
            for col, val in overrides.items():
                if val is not None:
                    row[col] = _as_int(val) if isinstance(val, bool) else val
            if view_html is not None:
                row["viewHTML"] = view_html
                row.setdefault("showView", 1)
            for lang, val in (labels or {}).items():
                if lang in NG_LABEL_LANGS and val:
                    row[f"actionDesc_{lang}"] = val
            for col, val in (extra or {}).items():
                row[col] = _as_int(val) if isinstance(val, bool) else val

            resp = _jsonsave(cur, "csNGAppWindowDataSetsActionsJSONSave", [row])
            if resp:
                return f"csNGAppWindowDataSetsActionsJSONSave ERROR:\n{resp}"
            out.append(f"ACTION {aw}.{data_set_ident}.{act}: {'updated' if existing else 'inserted'}"
                       + (f" (crud preset '{crud}')" if crud and not existing else ""))

            # --- action fields ---
            for f in (fields or []):
                fi = (f.get("dataFieldIdent") or "").strip()
                if not fi:
                    return f"Error: fields item without dataFieldIdent: {f}"
                fg = _exec_scalar(
                    cur,
                    "select csNGAppWindowDataSetsActionsFieldsG from dbo.csNGAppWindowDataSetsActionsFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and actionIdent=? and dataFieldIdent=?",
                    namespace_g, aw, data_set_ident, act, fi,
                )
                frow = {
                    "_opr": "U" if fg else "I",
                    "csNGAppWindowDataSetsActionsFieldsG": str(fg).upper() if fg else _new_guid(),
                    "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": aw,
                    "dataSetIdent": data_set_ident,
                    "actionIdent": act,
                    "dataFieldIdent": fi,
                }
                if fg:
                    fid = _exec_scalar(
                        cur,
                        "select csNGAppWindowDataSetsActionsFieldsId from dbo.csNGAppWindowDataSetsActionsFields with(nolock) "
                        "where csNGAppWindowDataSetsActionsFieldsG=?", fg,
                    )
                    frow["csNGAppWindowDataSetsActionsFieldsId"] = int(fid)
                for k in ("dataFieldValueDef", "dataFieldIdentForNewRowValue"):
                    if f.get(k) is not None:
                        frow[k] = f[k]
                resp = _jsonsave(cur, "csNGAppWindowDataSetsActionsFieldsJSONSave", [frow])
                if resp:
                    return f"ActionsFieldsJSONSave ERROR ({fi}):\n{resp}"
                out.append(f"  field {fi}: {'U' if fg else 'I'}")

            # --- privileges wiring (granular privileges only) ---
            if wire_privileges:
                cur.execute(
                    "select p.csPrivilegesG, p.hasRightsAllDataSets from dbo.csNGAppWindowsPrivileges p with(nolock) "
                    "where p.csAppNameSpacesG=? and p.appWindowIdent=?",
                    namespace_g, aw,
                )
                privs = cur.fetchall()
                wired = 0
                for pg, all_ds in privs:
                    if all_ds:
                        continue
                    dsp = cur.execute(
                        "select hasRightsAllActions from dbo.csNGAppWindowsDataSetsPrivileges with(nolock) "
                        "where csPrivilegesG=? and csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                        pg, namespace_g, aw, data_set_ident,
                    ).fetchone()
                    if not dsp or dsp[0]:
                        continue
                    exists_ap = _exec_scalar(
                        cur,
                        "select count(*) from dbo.csNGAppWindowsDataSetsActionsPrivileges with(nolock) "
                        "where csPrivilegesG=? and csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and actionIdent=?",
                        pg, namespace_g, aw, data_set_ident, act,
                    )
                    if exists_ap:
                        continue
                    resp = _jsonsave(cur, "csNGAppWindowsDataSetsActionsPrivilegesJSONSave", [{
                        "_opr": "I",
                        "csNGAppWindowsDataSetsActionsPrivilegesG": _new_guid(),
                        "csPrivilegesG": str(pg).upper(),
                        "csAppNameSpacesG": namespace_g,
                        "appWindowIdent": aw,
                        "dataSetIdent": data_set_ident,
                        "actionIdent": act,
                    }])
                    if resp:
                        return f"ActionsPrivilegesJSONSave ERROR:\n{resp}"
                    wired += 1
                out.append(f"  privileges: {wired} granular grant(s) added"
                           + ("" if privs else " (window has NO privileges — run ng_ensure_privileges!)"))

    if rebuild:
        out.append(rebuild_user_rights(connection_string, cs_companies_id=cs_companies_id))
    else:
        out.append("REMINDER: rebuild_user_rights after action/menu/privilege changes (button invisible otherwise).")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 25. ng_ensure_privileges — audit/create/repair window privileges + grant
# ---------------------------------------------------------------------------

def ng_ensure_privileges(
    connection_string: str,
    app_window_ident: str,
    create_if_missing: bool = True,
    privilege_desc_pl: Optional[str] = None,
    privilege_desc_en: Optional[str] = None,
    privilege_group_pl: str = "DSM",
    fix_gaps: bool = False,
    grant_cs_usr_id: Optional[int] = None,
    grant_cs_companies_id: Optional[int] = None,
    grant_privilege_g: Optional[str] = None,
    rebuild: bool = False,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Audit and repair the privilege wiring of an NG window. Model: csPrivileges (GUID
    identity, no text ident) -> csNGAppWindowsPrivileges (hasRightsAllDataSets=1 = full)
    -> optional granular DataSets/Actions privilege rows. A window WITHOUT any
    csNGAppWindowsPrivileges row = eternal spinner for non-admin users.
      - create_if_missing: creates a full-rights privilege (desc default = window PL desc).
      - fix_gaps: for granular privileges inserts missing DataSets rows
        (hasRightsAllActions=1) / Actions rows so the privilege covers the whole window.
      - grant_cs_usr_id + grant_cs_companies_id: grants via csCompaniesUsrsPrivileges
        (grant_privilege_g required when the window has >1 privilege).
      - rebuild: rebuild_user_rights afterwards (scoped to grant company/user if given).
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            wdesc = _exec_scalar(
                cur,
                "select appWindowDesc_PL from dbo.csNGAppWindows with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=?",
                namespace_g, aw,
            )
            if wdesc is None:
                return f"Error: window '{aw}' not found (namespace {namespace_g})."

            cur.execute(
                "select wp.csPrivilegesG, wp.hasRightsAllDataSets, p.PrivilegeDesc_PL, p.PrivilegeGroupDesc_PL "
                "from dbo.csNGAppWindowsPrivileges wp with(nolock) "
                "join dbo.csPrivileges p with(nolock) on p.csPrivilegesG = wp.csPrivilegesG "
                "where wp.csAppNameSpacesG=? and wp.appWindowIdent=?",
                namespace_g, aw,
            )
            privs = cur.fetchall()

            if not privs and create_if_missing:
                pg = _new_guid()
                resp = _jsonsave(cur, "csPrivilegesJSONSave", [{
                    "_opr": "I", "csPrivilegesG": pg,
                    "PrivilegeDesc_PL": privilege_desc_pl or wdesc or aw,
                    "PrivilegeDesc_EN": privilege_desc_en,
                    "PrivilegeGroupDesc_PL": privilege_group_pl,
                }])
                if resp:
                    return f"csPrivilegesJSONSave ERROR:\n{resp}"
                resp = _jsonsave(cur, "csNGAppWindowsPrivilegesJSONSave", [{
                    "_opr": "I", "csNGAppWindowsPrivilegesG": _new_guid(),
                    "csPrivilegesG": pg, "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": aw, "hasRightsAllDataSets": 1,
                }])
                if resp:
                    return f"csNGAppWindowsPrivilegesJSONSave ERROR:\n{resp}"
                out.append(f"CREATED full-rights privilege '{privilege_desc_pl or wdesc or aw}' ({pg}) "
                           f"group '{privilege_group_pl}', hasRightsAllDataSets=1.")
                privs = [(pg, 1, privilege_desc_pl or wdesc or aw, privilege_group_pl)]
            elif not privs:
                return (f"WINDOW {aw}: NO privileges (users see an eternal spinner). "
                        f"Re-run with create_if_missing=True to fix.")

            out.append(f"WINDOW {aw}: {len(privs)} privilege(s):")
            cur.execute(
                "select dataSetIdent from dbo.csNGAppWindowDataSets with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, aw)
            all_ds = [r[0] for r in cur.fetchall()]

            for pg, all_flag, dpl, grp in privs:
                pg = str(pg).upper()
                usr_cnt = _exec_scalar(
                    cur, "select count(*) from dbo.csCompaniesUsrsPrivileges with(nolock) where csPrivilegesG=?", pg)
                out.append(f"  {pg} | '{dpl}' (grupa '{grp}') | hasRightsAllDataSets={all_flag} "
                           f"| nadany {usr_cnt} userom")
                if all_flag:
                    continue
                # granular audit
                cur.execute(
                    "select dataSetIdent, hasRightsAllActions from dbo.csNGAppWindowsDataSetsPrivileges with(nolock) "
                    "where csPrivilegesG=? and csAppNameSpacesG=? and appWindowIdent=?",
                    pg, namespace_g, aw,
                )
                dsp = {r[0]: r[1] for r in cur.fetchall()}
                missing_ds = [d for d in all_ds if d not in dsp]
                if missing_ds:
                    if fix_gaps:
                        rows = [{
                            "_opr": "I", "csNGAppWindowsDataSetsPrivilegesG": _new_guid(),
                            "csPrivilegesG": pg, "csAppNameSpacesG": namespace_g,
                            "appWindowIdent": aw, "dataSetIdent": d,
                            "hasRightsAllActions": 1, "hasRightsAllLayoutsCols": 1,
                            "hasRightsAllColsGroups": 1,
                        } for d in missing_ds]
                        resp = _jsonsave(cur, "csNGAppWindowsDataSetsPrivilegesJSONSave", rows)
                        if resp:
                            return f"DataSetsPrivilegesJSONSave ERROR:\n{resp}"
                        out.append(f"    FIXED: added dataset grants: {', '.join(missing_ds)}")
                    else:
                        out.append(f"    GAP: datasets not covered: {', '.join(missing_ds)} (fix_gaps=True to add)")
                for d, all_act in dsp.items():
                    if all_act:
                        continue
                    cur.execute(
                        "select a.actionIdent from dbo.csNGAppWindowDataSetsActions a with(nolock) "
                        "where a.csAppNameSpacesG=? and a.appWindowIdent=? and a.dataSetIdent=? "
                        "and not exists (select 1 from dbo.csNGAppWindowsDataSetsActionsPrivileges ap with(nolock) "
                        " where ap.csPrivilegesG=? and ap.csAppNameSpacesG=a.csAppNameSpacesG "
                        " and ap.appWindowIdent=a.appWindowIdent and ap.dataSetIdent=a.dataSetIdent "
                        " and ap.actionIdent=a.actionIdent)",
                        namespace_g, aw, d, pg,
                    )
                    missing_act = [r[0] for r in cur.fetchall()]
                    if missing_act:
                        if fix_gaps:
                            rows = [{
                                "_opr": "I", "csNGAppWindowsDataSetsActionsPrivilegesG": _new_guid(),
                                "csPrivilegesG": pg, "csAppNameSpacesG": namespace_g,
                                "appWindowIdent": aw, "dataSetIdent": d, "actionIdent": a,
                            } for a in missing_act]
                            resp = _jsonsave(cur, "csNGAppWindowsDataSetsActionsPrivilegesJSONSave", rows)
                            if resp:
                                return f"ActionsPrivilegesJSONSave ERROR:\n{resp}"
                            out.append(f"    FIXED: {d}: added action grants: {', '.join(missing_act)}")
                        else:
                            out.append(f"    GAP: {d}: actions not covered: {', '.join(missing_act)}")

            # --- grant to user ---
            if grant_cs_usr_id:
                if not grant_cs_companies_id:
                    return "\n".join(out) + "\nError: grant requires grant_cs_companies_id."
                pg = (grant_privilege_g or "").upper()
                if not pg:
                    if len(privs) == 1:
                        pg = str(privs[0][0]).upper()
                    else:
                        return "\n".join(out) + "\nError: window has multiple privileges — pass grant_privilege_g."
                already = _exec_scalar(
                    cur,
                    "select count(*) from dbo.csCompaniesUsrsPrivileges with(nolock) "
                    "where csCompaniesId=? and csUsrId=? and csPrivilegesG=?",
                    int(grant_cs_companies_id), int(grant_cs_usr_id), pg,
                )
                if already:
                    out.append(f"GRANT: user {grant_cs_usr_id} already has {pg}.")
                else:
                    resp = _jsonsave(cur, "csCompaniesUsrsPrivilegesJSONSave", [{
                        "_opr": "I", "csCompaniesUsrsPrivilegesG": _new_guid(),
                        "csCompaniesId": int(grant_cs_companies_id),
                        "csUsrId": int(grant_cs_usr_id), "csPrivilegesG": pg,
                    }])
                    if resp:
                        return f"csCompaniesUsrsPrivilegesJSONSave ERROR:\n{resp}"
                    out.append(f"GRANT: privilege {pg} -> user {grant_cs_usr_id} (company {grant_cs_companies_id}).")

    if rebuild:
        out.append(rebuild_user_rights(connection_string,
                                       cs_companies_id=grant_cs_companies_id,
                                       cs_usr_id=grant_cs_usr_id,
                                       confirm_all=not grant_cs_companies_id))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 26. ng_add_menu_entry — NGDict menu entry NEXT TO the Dict one (never replace)
# ---------------------------------------------------------------------------

def ng_add_menu_entry(
    connection_string: str,
    app_window_ident: str,
    dict_app_window: Optional[str] = None,
    parent_menu_path: Optional[str] = None,
    labels: Optional[dict] = None,
    menu_path: Optional[str] = None,
    ord: Optional[int] = None,
    usable: bool = True,
    rebuild: bool = False,
    cs_companies_id: Optional[int] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Add the NG menu entry (Kind='NGDict') for a window, enforcing the project rule:
    the Dict entry is NEVER replaced — both entries coexist.
      - If the Dict predecessor has a menu entry (dict_app_window, default =
        app_window_ident): the NGDict entry is CLONED from it (same parent/Id/labels/
        ContentGuid/Icon, generator formula for menuPath slug from appWindowDesc_PL).
      - Otherwise a fresh entry is created under parent_menu_path (menuPath of the
        parent node, e.g. '/rozrachunki'); labels {PL,...} required (ContentGuid is
        reused/created in csTranslate by content).
    Idempotent: existing NGDict entry -> reports it (and can flip usable).
    REMEMBER: menu changes need the user cache rebuild (rebuild=True + cs_companies_id).
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            wdesc = _exec_scalar(
                cur,
                "select appWindowDesc_PL from dbo.csNGAppWindows with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, aw)
            if wdesc is None:
                return f"Error: NG window '{aw}' not found (namespace {namespace_g})."

            cur.execute(
                "select csAppMainMenusItemsId, csAppMainMenusItemsG, menuPath, usable "
                "from dbo.csAppMainMenusItems with(nolock) "
                "where appWindowIdent=? and csAppNameSpacesG=? and Kind=N'NGDict'",
                aw, namespace_g,
            )
            existing = cur.fetchall()
            if existing:
                for r in existing:
                    out.append(f"EXISTS: NGDict entry Id={r[0]} menuPath={r[2]!r} usable={r[3]}")
                    if usable and not r[3]:
                        resp = _jsonsave(cur, "csAppMainMenusItemsJSONSave", [{
                            "_opr": "U", "csAppMainMenusItemsId": int(r[0]),
                            "csAppMainMenusItemsG": str(r[1]).upper(), "usable": 1,
                        }])
                        out.append("  usable -> 1" + (f" (WARN: {resp})" if resp else ""))
                return "\n".join(out)

            slug = _exec_scalar(
                cur, "select replace(lower(dbo.csFnNonPLOnlyCharV01(?)), N' ', N'-')",
                (labels or {}).get("PL") or wdesc or aw)

            # Case A: clone from the Dict predecessor's entry
            dict_name = (dict_app_window or aw).strip()
            cur.execute(
                "select top 1 p.csAppMainMenusParentItemsG, p.csAppMainMenusG, p.Id, p.Icon, p.IsQuick, "
                "p.StripColorBrush, p.ContentGuid, p.notShowInAppMenu, p.commands, p.params, "
                "isnull(nullif(pr.parentMenuPath, N'/'), N'') parentPath, "
                "p.Content_PL, p.Content_EN, p.Content_DE, p.Content_FR, p.Content_ES, p.Content_IT, "
                "p.Content_NL, p.Content_PT, p.Content_RU, p.Content_UK, p.Content_SK, p.Content_SE "
                "from dbo.csAppMainMenusItems p with(nolock) "
                "join dbo.csAppWindows a with(nolock) on a.csAppWindowsG = p.csAppWindowsG "
                "outer apply (select max(parent.menuPath) parentMenuPath from dbo.csAppMainMenusItems parent with(nolock) "
                "  where parent.csAppMainMenusItemsG = p.csAppMainMenusParentItemsG) pr "
                "where a.AppWindow = ? and p.Kind = N'Dict' "
                "order by p.usable desc, p.csAppMainMenusItemsId",
                dict_name,
            )
            dict_row = cur.fetchone()

            if dict_row:
                cols = [d[0] for d in cur.description]
                d = dict(zip(cols, dict_row))
                new_path = menu_path or (d["parentPath"] + "/" + slug)
                row = {
                    "_opr": "I", "csAppMainMenusItemsG": _new_guid(),
                    "csAppMainMenusParentItemsG": str(d["csAppMainMenusParentItemsG"]).upper()
                        if d["csAppMainMenusParentItemsG"] else None,
                    "csAppMainMenusG": str(d["csAppMainMenusG"]).upper(),
                    "Id": int(ord if ord is not None else d["Id"]),
                    "csAppWindowsG": None, "Kind": "NGDict",
                    "Icon": d["Icon"], "IsQuick": _as_int(d["IsQuick"] or 0),
                    "StripColorBrush": d["StripColorBrush"],
                    "appWindowIdent": aw, "csAppNameSpacesG": namespace_g,
                    "ContentGuid": str(d["ContentGuid"]).upper(),
                    "childMenuPath": slug, "menuPath": new_path,
                    "notShowInAppMenu": _as_int(d["notShowInAppMenu"] or 0),
                    "commands": d["commands"], "params": d["params"],
                    "deprecated": 0, "usable": _as_int(usable),
                }
                for lang in NG_COLSGROUP_LANGS:
                    row[f"Content_{lang}"] = (labels or {}).get(lang) or d.get(f"Content_{lang}")
                mode = f"cloned from Dict entry of '{dict_name}'"
            else:
                # Case B: fresh entry under parent_menu_path
                if not parent_menu_path:
                    return (f"Error: no Dict menu entry found for '{dict_name}' — pass parent_menu_path "
                            f"(menuPath of the parent node) and labels for a fresh entry.")
                cur.execute(
                    "select csAppMainMenusItemsG, csAppMainMenusG, menuPath from dbo.csAppMainMenusItems with(nolock) "
                    "where menuPath = ?", parent_menu_path)
                parent = cur.fetchone()
                if not parent:
                    return f"Error: parent menu item with menuPath='{parent_menu_path}' not found."
                texts = dict(labels or {})
                texts.setdefault("PL", wdesc or aw)
                try:
                    content_g, tg_action = _ensure_translate(cur, texts)
                except (ValueError, RuntimeError) as e:
                    return f"Error (csTranslate): {e}"
                max_id = _exec_scalar(
                    cur,
                    "select isnull(max(Id),0) from dbo.csAppMainMenusItems with(nolock) "
                    "where csAppMainMenusG=? and csAppMainMenusParentItemsG=?",
                    parent[1], parent[0],
                )
                parent_path = "" if parent[2] == "/" else (parent[2] or "")
                row = {
                    "_opr": "I", "csAppMainMenusItemsG": _new_guid(),
                    "csAppMainMenusParentItemsG": str(parent[0]).upper(),
                    "csAppMainMenusG": str(parent[1]).upper(),
                    "Id": int(ord if ord is not None else int(max_id) + 1),
                    "csAppWindowsG": None, "Kind": "NGDict", "IsQuick": 0,
                    "appWindowIdent": aw, "csAppNameSpacesG": namespace_g,
                    "ContentGuid": content_g,
                    "childMenuPath": slug,
                    "menuPath": menu_path or (parent_path + "/" + slug),
                    "notShowInAppMenu": 0, "deprecated": 0, "usable": _as_int(usable),
                }
                for lang in NG_COLSGROUP_LANGS:
                    if texts.get(lang):
                        row[f"Content_{lang}"] = texts[lang]
                mode = f"fresh entry under '{parent_menu_path}' (csTranslate {tg_action})"

            collision = _exec_scalar(
                cur,
                "select count(*) from dbo.csAppMainMenusItems with(nolock) "
                "where csAppMainMenusG=? and menuPath=?",
                row["csAppMainMenusG"], row["menuPath"],
            )
            if collision:
                return (f"Error: menuPath '{row['menuPath']}' already exists in this menu "
                        f"(UQ csAppMainMenusG+menuPath). Pass menu_path explicitly.")
            resp = _jsonsave(cur, "csAppMainMenusItemsJSONSave", [row])
            if resp:
                return f"csAppMainMenusItemsJSONSave ERROR:\n{resp}"
            out.append(f"MENU: added NGDict entry for {aw} ({mode}) menuPath={row['menuPath']!r} "
                       f"Id={row['Id']} usable={row['usable']}.")
            if dict_row:
                out.append("Dict entry left untouched (project rule: both entries coexist).")

    if rebuild:
        out.append(rebuild_user_rights(connection_string, cs_companies_id=cs_companies_id))
    else:
        out.append("REMINDER: rebuild_user_rights (menu cache) or the entry stays invisible.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 27. ng_set_sort — SortIdents + LayoutsColsSortOrder in one call
# ---------------------------------------------------------------------------

def ng_set_sort(
    connection_string: str,
    app_window_ident: str,
    columns: Sequence[dict],
    data_set_ident: str = "main",
    layout_ident: str = "default",
    sort_ident: str = "default",
    is_def: bool = True,
    labels: Optional[dict] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Define/replace a sort for an NG dataset layout: upserts the SortIdents header and
    REPLACES its LayoutsColsSortOrder column list. columns: [{field, desc?}] in order.
    Rules handled: sort on a BUSINESS column (never <T>G), only one isDef=1 per layout
    (others get isDef=0 first — filtered unique index), fields validated against
    csNGAppWindowDataSetsFields. labels {PL,...} -> sortDesc_*.
    """
    aw = (app_window_ident or "").strip()
    if not aw or not columns:
        return "Error: app_window_ident and columns are required."
    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            for c in columns:
                fi = (c.get("field") or "").strip()
                if not fi:
                    return f"Error: columns item without field: {c}"
                if fi.lower().endswith("g") and fi.lower() == (aw.lower() + "g"):
                    out.append(f"WARN: sorting on {fi} looks like the <T>G GUID — use a business column.")
                known = _exec_scalar(
                    cur,
                    "select count(*) from dbo.csNGAppWindowDataSetsFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                    namespace_g, aw, data_set_ident, fi,
                )
                if not known:
                    return f"Error: field '{fi}' not found in {aw}.{data_set_ident} fields."

            # unset other defaults (filtered UQ on isDef=1 per layout)
            if is_def:
                cur.execute(
                    "select csNGAppWindowDataSetsSortIdentsId, csNGAppWindowDataSetsSortIdentsG, sortIdent "
                    "from dbo.csNGAppWindowDataSetsSortIdents with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and layoutIdent=? "
                    "and isDef=1 and sortIdent<>?",
                    namespace_g, aw, data_set_ident, layout_ident, sort_ident,
                )
                for r in cur.fetchall():
                    resp = _jsonsave(cur, "csNGAppWindowDataSetsSortIdentsJSONSave", [{
                        "_opr": "U", "csNGAppWindowDataSetsSortIdentsId": int(r[0]),
                        "csNGAppWindowDataSetsSortIdentsG": str(r[1]).upper(), "isDef": 0,
                    }])
                    if resp:
                        return f"SortIdentsJSONSave ERROR (unset default {r[2]}):\n{resp}"
                    out.append(f"  isDef=0 on previous default '{r[2]}'")

            cur.execute(
                "select csNGAppWindowDataSetsSortIdentsId, csNGAppWindowDataSetsSortIdentsG "
                "from dbo.csNGAppWindowDataSetsSortIdents with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and layoutIdent=? and sortIdent=?",
                namespace_g, aw, data_set_ident, layout_ident, sort_ident,
            )
            hdr = cur.fetchone()
            row = {
                "_opr": "U" if hdr else "I",
                "csNGAppWindowDataSetsSortIdentsG": str(hdr[1]).upper() if hdr else _new_guid(),
                "csAppNameSpacesG": namespace_g, "appWindowIdent": aw,
                "dataSetIdent": data_set_ident, "layoutIdent": layout_ident,
                "sortIdent": sort_ident, "isDef": _as_int(is_def),
            }
            if hdr:
                row["csNGAppWindowDataSetsSortIdentsId"] = int(hdr[0])
            else:
                max_ord = _exec_scalar(
                    cur,
                    "select isnull(max(ord),0) from dbo.csNGAppWindowDataSetsSortIdents with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and layoutIdent=?",
                    namespace_g, aw, data_set_ident, layout_ident,
                )
                row["ord"] = int(max_ord) + 1
            for lang, val in (labels or {}).items():
                if lang in NG_LABEL_LANGS and val:
                    row[f"sortDesc_{lang}"] = val
            resp = _jsonsave(cur, "csNGAppWindowDataSetsSortIdentsJSONSave", [row])
            if resp:
                return f"SortIdentsJSONSave ERROR:\n{resp}"
            out.append(f"SORT {aw}.{data_set_ident}.{layout_ident}.{sort_ident}: "
                       f"{'updated' if hdr else 'created'} (isDef={row['isDef']}).")

            # replace column list
            cur.execute(
                "select csNGAppWindowDataSetsLayoutsColsSortOrderId, csNGAppWindowDataSetsLayoutsColsSortOrderG "
                "from dbo.csNGAppWindowDataSetsLayoutsColsSortOrder with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and layoutIdent=? and sortIdent=?",
                namespace_g, aw, data_set_ident, layout_ident, sort_ident,
            )
            old = cur.fetchall()
            if old:
                resp = _jsonsave(cur, "csNGAppWindowDataSetsLayoutsColsSortOrderJSONSave", [{
                    "_opr": "D",
                    "csNGAppWindowDataSetsLayoutsColsSortOrderId": int(r[0]),
                    "csNGAppWindowDataSetsLayoutsColsSortOrderG": str(r[1]).upper(),
                } for r in old])
                if resp:
                    return f"LayoutsColsSortOrderJSONSave DELETE ERROR:\n{resp}"
            new_rows = [{
                "_opr": "I",
                "csNGAppWindowDataSetsLayoutsColsSortOrderG": _new_guid(),
                "csAppNameSpacesG": namespace_g, "appWindowIdent": aw,
                "dataSetIdent": data_set_ident, "layoutIdent": layout_ident,
                "sortIdent": sort_ident, "dataFieldIdent": c["field"].strip(),
                "ord": i + 1, "isDesc": _as_int(bool(c.get("desc"))), "sortType": 0,
            } for i, c in enumerate(columns)]
            resp = _jsonsave(cur, "csNGAppWindowDataSetsLayoutsColsSortOrderJSONSave", new_rows)
            if resp:
                return f"LayoutsColsSortOrderJSONSave INSERT ERROR:\n{resp}"
            out.append("  columns: " + ", ".join(
                f"{c['field'].strip()}{' DESC' if c.get('desc') else ''}" for c in columns)
                + f" (replaced {len(old)} old row(s))")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 28. ng_diff_with_dict — migration gap report NG window vs Dict/table context
# ---------------------------------------------------------------------------

def ng_diff_with_dict(
    connection_string: str,
    app_window_ident: str,
    dict_app_window: Optional[str] = None,
    table_name: Optional[str] = None,
    data_set_ident: str = "main",
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Read-only migration gap report: compares the NG window against the Dict/table
    context (csNGDictWindowContextForAI) + the Dict window anatomy (AppWindowXML):
      - suggested-visible table fields missing from the NG default layout,
      - FK lookups proposed by the context but not wired in LookupDefs,
      - default sort presence (and <T>G-sort warning),
      - where-fields / filter presence,
      - Dict datasets (from AppWindowXML) vs NG datasets.
    Complements csNGValidateWindowForAI (config sanity) with FIDELITY-to-Dict checks.
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    dict_name = (dict_app_window or aw).strip()
    out: List[str] = [f"DIFF {aw} (NG) vs '{dict_name}' (Dict/table context)"]

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            # --- context proc (result sets tagged by 'section' column) ---
            meta, ctx_fields, fk_lookups = {}, [], []
            cur.execute("exec dbo.csNGDictWindowContextForAI @appWindow=?, @tableName=?",
                        dict_name, table_name)
            while True:
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    for r in cur.fetchall():
                        rec = dict(zip(cols, r))
                        sec = rec.get("section")
                        if sec == "meta":
                            meta = rec
                        elif sec == "field":
                            ctx_fields.append(rec)
                        elif sec == "fkLookup":
                            fk_lookups.append(rec)
                if not cur.nextset():
                    break
            if meta:
                out.append(f"META: existsInDict={meta.get('existsInDict')} existsInNG={meta.get('existsInNG')} "
                           f"table={meta.get('tableName')} pk={meta.get('pkColumns')}")

            # --- NG side ---
            cur.execute(
                "select lc.dataFieldIdent from dbo.csNGAppWindowDataSetsLayoutsCols lc with(nolock) "
                "where lc.csAppNameSpacesG=? and lc.appWindowIdent=? and lc.dataSetIdent=? and lc.isVisible=1",
                namespace_g, aw, data_set_ident,
            )
            ng_visible = {r[0] for r in cur.fetchall()}
            cur.execute(
                "select dataFieldIdent from dbo.csNGAppWindowDataSetsFields with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                namespace_g, aw, data_set_ident,
            )
            ng_fields = {r[0] for r in cur.fetchall()}
            if not ng_fields:
                out.append(f"NG: dataset {aw}.{data_set_ident} has NO fields (window missing or empty).")

            # 1. suggested-visible fields not visible in NG
            missing_vis = [f["dataFieldIdent"] for f in ctx_fields
                           if f.get("suggestedIsVisible") and f["dataFieldIdent"] not in ng_visible]
            extra_vis = sorted(ng_visible - {f["dataFieldIdent"] for f in ctx_fields}) if ctx_fields else []
            if missing_vis:
                out.append(f"FIELDS missing from NG visible layout ({len(missing_vis)}): " + ", ".join(missing_vis))
            elif ctx_fields:
                out.append("FIELDS: all suggested-visible table columns are visible in NG.")
            if extra_vis:
                out.append(f"FIELDS visible in NG beyond table columns (computed/joins — OK if intended): "
                           + ", ".join(extra_vis))

            # 2. FK lookups not wired
            cur.execute(
                "select dataFieldIdent from dbo.csNGAppWindowDataSetsLookupDefs with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, aw)
            wired = {r[0].lower() for r in cur.fetchall()}
            for fk in fk_lookups:
                col = (fk.get("lookupColumn") or "")
                host_variants = {col.lower(), col.lower().replace("id", "desc"),
                                 (col[:-2] + "Desc").lower() if col.lower().endswith("id") else col.lower()}
                if col and not (host_variants & wired) and col in ng_fields:
                    mark = "" if fk.get("hasLookupNGWindow") else " (lookup window MISSING too)"
                    out.append(f"LOOKUP not wired: {col} -> {fk.get('proposedLookupNGIdent')}{mark}")

            # 3. sort
            cur.execute(
                "select si.sortIdent, so.dataFieldIdent from dbo.csNGAppWindowDataSetsSortIdents si with(nolock) "
                "left join dbo.csNGAppWindowDataSetsLayoutsColsSortOrder so with(nolock) "
                "  on so.csAppNameSpacesG=si.csAppNameSpacesG and so.appWindowIdent=si.appWindowIdent "
                "  and so.dataSetIdent=si.dataSetIdent and so.layoutIdent=si.layoutIdent and so.sortIdent=si.sortIdent "
                "where si.csAppNameSpacesG=? and si.appWindowIdent=? and si.dataSetIdent=? and si.isDef=1",
                namespace_g, aw, data_set_ident,
            )
            sort_rows = cur.fetchall()
            if not sort_rows:
                out.append("SORT: NO default sort (add ng_set_sort — required for every NG window).")
            else:
                sort_cols = [r[1] for r in sort_rows if r[1]]
                out.append(f"SORT: default '{sort_rows[0][0]}' on: {', '.join(sort_cols) or '(no columns!)'}")
                if any((c or "").lower() == (aw.lower() + "g") for c in sort_cols):
                    out.append("SORT WARN: sorted by <T>G — use a business column.")

            # 4. filters
            wf_cnt = _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                namespace_g, aw, data_set_ident)
            out.append(f"FILTERS: {wf_cnt} where-field(s) on {data_set_ident}"
                       + (" — none; Dict windows almost always filter (check the original)." if not wf_cnt else ""))

            # 5. Dict datasets vs NG datasets
            xml = _exec_scalar(
                cur,
                "select top 1 cast(AppWindowXML as nvarchar(max)) from dbo.csAppWindows with(nolock) "
                "where AppWindow=? order by len(cast(AppWindowViewHTML as nvarchar(max))) desc",
                dict_name)
            if xml:
                dict_ds = list(dict.fromkeys(
                    re.findall(r"<DataSetSQLIdent>([^<]+)</DataSetSQLIdent>", xml)))
                cur.execute(
                    "select dataSetIdent from dbo.csNGAppWindowDataSets with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, aw)
                ng_ds = [r[0] for r in cur.fetchall()]
                shown = ", ".join(dict_ds[:40]) + (f", ... (+{len(dict_ds) - 40})" if len(dict_ds) > 40 else "")
                out.append(f"DATASETS Dict({len(dict_ds)} unikalnych): {shown or '-'}")
                out.append(f"DATASETS NG({len(ng_ds)}): {', '.join(ng_ds) or '-'}")
                out.append("HINT: pełna anatomia formatki Dict (kolumny/filtry/zakładki): "
                           f"rag_get_dict_window_html('{dict_name}').")
            else:
                out.append(f"DICT: no csAppWindows row named '{dict_name}' (pure-table window / different name).")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 29. help_upsert_topic — csHelpContents + window links in one call
# ---------------------------------------------------------------------------

def _help_content_replace(conn, cur, row_id: int, row_g: str, payload: dict, cont: dict):
    """csHelpContentsJSONSave U-path silently skips Content_*/TransformedContent_* (reports
    success, persists nothing) — the only working edit is DELETE+re-INSERT with the same G.
    Both link tables (csHelpContentsNGAppWindows, legacy csHelpContentsAppWindows) carry an
    FK to csHelpContents, so links are detached first and re-inserted afterwards (same link
    G), all inside one transaction. Returns (error_or_None, notes)."""
    notes: List[str] = []
    cur.execute("select * from dbo.csHelpContents with(nolock) where csHelpContentsId=?", row_id)
    row = cur.fetchone()
    if not row:
        return (f"Error: csHelpContents row Id={row_id} not found.", notes)
    cols = [d[0] for d in cur.description]
    existing = {}
    for c, v in zip(cols, row):
        if c == "csHelpContentsId" or v is None:
            continue
        existing[c] = v if isinstance(v, (str, int, float, bool)) else str(v)
    merged = dict(existing)
    merged.update({k: v for k, v in payload.items() if k != "csHelpContentsId" and not k.startswith("_")})
    # keep the legacy H5 renderer in sync: refresh TransformedContent only where it existed
    for lang, v in cont.items():
        if existing.get(f"TransformedContent_{lang}") is not None:
            merged[f"TransformedContent_{lang}"] = v
    merged["csHelpContentsG"] = row_g
    merged.setdefault("IsExternalEditor", 0)

    cur.execute(
        "select csHelpContentsNGAppWindowsId, csHelpContentsNGAppWindowsG, csAppNameSpacesG, appWindowIdent "
        "from dbo.csHelpContentsNGAppWindows with(nolock) where csHelpContentsG=?", row_g)
    ng_links = [(int(r[0]), str(r[1]).upper(), str(r[2]).upper(), r[3]) for r in cur.fetchall()]
    cur.execute(
        "select csHelpContentsAppWindowsId, csHelpContentsAppWindowsG, csAppWindowsG "
        "from dbo.csHelpContentsAppWindows with(nolock) where csHelpContentsG=?", row_g)
    legacy_links = [(int(r[0]), str(r[1]).upper(), str(r[2]).upper()) for r in cur.fetchall()]

    conn.autocommit = False
    try:
        # D of the auto-gen link tables needs the NATURAL key (Id+G alone = silent no-op)
        for lid, lg, ns, wid in ng_links:
            resp = _jsonsave(cur, "csHelpContentsNGAppWindowsJSONSave", [{
                "_opr": "D", "csHelpContentsNGAppWindowsId": lid, "csHelpContentsNGAppWindowsG": lg,
                "csHelpContentsG": row_g, "csAppNameSpacesG": ns, "appWindowIdent": wid}])
            if resp:
                raise RuntimeError(f"D NG link '{wid}': {resp}")
        for lid, lg, awg in legacy_links:
            resp = _jsonsave(cur, "csHelpContentsAppWindowsJSONSave", [{
                "_opr": "D", "csHelpContentsAppWindowsId": lid, "csHelpContentsAppWindowsG": lg,
                "csHelpContentsG": row_g, "csAppWindowsG": awg}])
            if resp:
                raise RuntimeError(f"D legacy link '{awg}': {resp}")
        resp = _jsonsave(cur, "csHelpContentsJSONSave", [{
            "_opr": "D", "csHelpContentsId": row_id, "csHelpContentsG": row_g}])
        if resp:
            raise RuntimeError(f"D content: {resp}")
        merged["_opr"] = "I"
        resp = _jsonsave(cur, "csHelpContentsJSONSave", [merged])
        if resp:
            raise RuntimeError(f"I content: {resp}")
        for _lid, lg, ns, wid in ng_links:
            resp = _jsonsave(cur, "csHelpContentsNGAppWindowsJSONSave", [{
                "_opr": "I", "csHelpContentsNGAppWindowsG": lg,
                "csHelpContentsG": row_g, "csAppNameSpacesG": ns, "appWindowIdent": wid}])
            if resp:
                raise RuntimeError(f"I NG link '{wid}': {resp}")
        for _lid, lg, awg in legacy_links:
            resp = _jsonsave(cur, "csHelpContentsAppWindowsJSONSave", [{
                "_opr": "I", "csHelpContentsAppWindowsG": lg,
                "csHelpContentsG": row_g, "csAppWindowsG": awg}])
            if resp:
                raise RuntimeError(f"I legacy link '{awg}': {resp}")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.autocommit = True
        return (f"CONTENT REPLACE ROLLED BACK (nothing changed): {exc}", notes)
    conn.autocommit = True

    # verify persisted lengths — the whole reason this workaround exists
    for lang, v in cont.items():
        expected = len(v.encode("utf-16-le")) // 2  # SQL len() counts UTF-16 units
        got = _exec_scalar(
            cur, f"select len(Content_{lang}) from dbo.csHelpContents with(nolock) where csHelpContentsG=?",
            row_g)
        if got != expected:
            notes.append(f"WARN: Content_{lang} persisted len={got}, expected {expected} — verify manually.")
    notes.append(
        f"TOPIC content replaced: {row_g} via transactional D+I with the same G "
        f"(U-path skips Content_*); links re-attached: NG={len(ng_links)}, legacy={len(legacy_links)}.")
    return (None, notes)


def help_upsert_topic(
    connection_string: str,
    subject,
    content=None,
    description=None,
    keywords=None,
    window_idents: Optional[Sequence[str]] = None,
    help_contents_g: Optional[str] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Upsert a help topic (csHelpContents) and link it to NG windows
    (csHelpContentsNGAppWindows). subject/content/description/keywords: either a
    plain string (=PL) or {PL, EN, ...}. Matching: help_contents_g, else Subject_PL;
    an UNKNOWN help_contents_g creates the topic WITH that GUID (stable series like
    0A5E18xx stay usable). Pitfalls handled: (1) content updates go through a
    transactional DELETE+re-INSERT with the same G (links detached/re-attached),
    because csHelpContentsJSONSave U-path silently skips Content_*/TransformedContent_*;
    persisted length is verified afterwards; (2) images must be INLINE base64 in
    Content_* (external URLs do not render) — external <img src="http..."> = WARN.
    """
    def _langs(val, allowed):
        if val is None:
            return {}
        if isinstance(val, str):
            return {"PL": val}
        return {k: v for k, v in val.items() if k in allowed and v}

    subj = _langs(subject, NG_COLSGROUP_LANGS)
    cont = _langs(content, NG_COLSGROUP_LANGS)
    desc = _langs(description, NG_COLSGROUP_LANGS)
    keyw = _langs(keywords, NG_COLSGROUP_LANGS)
    if not subj.get("PL") and not help_contents_g:
        return "Error: subject PL (or help_contents_g) is required."

    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            row_id = row_g = None
            requested_g = None
            if help_contents_g:
                try:
                    requested_g = str(uuid.UUID(str(help_contents_g))).upper()
                except (ValueError, AttributeError, TypeError):
                    return f"Error: help_contents_g '{help_contents_g}' is not a valid GUID."
                cur.execute(
                    "select csHelpContentsId, csHelpContentsG from dbo.csHelpContents with(nolock) "
                    "where csHelpContentsG=?", requested_g)
                r = cur.fetchone()
                if r:
                    row_id, row_g = int(r[0]), str(r[1]).upper()
            elif subj.get("PL"):
                cur.execute(
                    "select csHelpContentsId, csHelpContentsG from dbo.csHelpContents with(nolock) "
                    "where Subject_PL=?", subj["PL"])
                r = cur.fetchone()
                if r:
                    row_id, row_g = int(r[0]), str(r[1]).upper()

            payload: dict = {}
            for lang, v in subj.items():
                payload[f"Subject_{lang}"] = v
            for lang, v in cont.items():
                payload[f"Content_{lang}"] = v
                if re.search(r"<img[^>]+src=[\"']https?://", v, re.I):
                    out.append(f"WARN: Content_{lang} contains EXTERNAL <img> URLs — inline base64 "
                               "(Content_*) is the only variant that renders in the help panel.")
            for lang, v in desc.items():
                payload[f"Description_{lang}"] = v
            for lang, v in keyw.items():
                payload[f"keyWords_{lang}"] = v

            if row_g:
                if cont:
                    # U-path skips Content_* — replace the row transactionally (same G)
                    err, notes = _help_content_replace(conn, cur, row_id, row_g, payload, cont)
                    if err:
                        return err
                    out.extend(notes)
                elif payload:
                    payload.update({"_opr": "U", "csHelpContentsId": row_id, "csHelpContentsG": row_g})
                    resp = _jsonsave(cur, "csHelpContentsJSONSave", [payload])
                    if resp:
                        return f"csHelpContentsJSONSave ERROR:\n{resp}"
                    out.append(f"TOPIC updated: {row_g} ({subj.get('PL', '(no subject change)')}).")
                else:
                    out.append(f"TOPIC exists: {row_g} (nothing to update).")
            else:
                if not subj.get("PL"):
                    return (f"Error: help_contents_g {requested_g} does not exist and no subject PL "
                            "was given — cannot create a topic without a subject.")
                row_g = requested_g or _new_guid()
                payload.update({"_opr": "I", "csHelpContentsG": row_g, "IsExternalEditor": 0})
                # INSERT requires Description_PL ('Proszę uzupełnić pole [Opis]')
                if not payload.get("Description_PL"):
                    payload["Description_PL"] = subj.get("PL")
                resp = _jsonsave(cur, "csHelpContentsJSONSave", [payload])
                if resp:
                    return f"csHelpContentsJSONSave ERROR:\n{resp}"
                out.append(f"TOPIC created: {row_g} '{subj['PL']}'"
                           + (" (with the requested GUID)." if requested_g else "."))

            for w in (window_idents or []):
                w = (w or "").strip()
                known = _exec_scalar(
                    cur, "select count(*) from dbo.csNGAppWindows with(nolock) "
                         "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, w)
                if not known:
                    out.append(f"  LINK SKIPPED: NG window '{w}' not found.")
                    continue
                dup = _exec_scalar(
                    cur, "select count(*) from dbo.csHelpContentsNGAppWindows with(nolock) "
                         "where csHelpContentsG=? and csAppNameSpacesG=? and appWindowIdent=?",
                    row_g, namespace_g, w)
                if dup:
                    out.append(f"  LINK exists: {w}")
                    continue
                resp = _jsonsave(cur, "csHelpContentsNGAppWindowsJSONSave", [{
                    "_opr": "I", "csHelpContentsNGAppWindowsG": _new_guid(),
                    "csHelpContentsG": row_g, "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": w,
                }])
                if resp:
                    return f"csHelpContentsNGAppWindowsJSONSave ERROR ({w}):\n{resp}"
                out.append(f"  LINK added: {w}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 30. ai_tool_register — AI tool + params + agent attachment in one call
# ---------------------------------------------------------------------------

def ai_tool_register(
    connection_string: str,
    name: str,
    description: Optional[str] = None,
    sql_procedure: Optional[str] = None,
    tool_type: str = "function",
    params: Optional[Sequence[dict]] = None,
    agents: Optional[Sequence] = None,
    use_permissions: Optional[bool] = None,
) -> str:
    """
    Register an AI tool end-to-end: csAIAgentsTools upsert (matched by SQLProcedure or
    name) + parameter sync (delegates to ai_tool_sync_params, all its pitfalls handled)
    + ATTACHMENT to agents via csAIAgentsToolsAgents — the chronically forgotten step
    (a registered but unattached tool is invisible to every agent).
    agents: list of agent names or csAIAgentsId (int); each attachment takes
    csCompaniesId from the agent row.
    """
    nm = (name or "").strip()
    if not nm:
        return "Error: name is required."
    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            proc = (sql_procedure or "").strip()
            if proc and not proc.lower().startswith("dbo."):
                proc = "dbo." + proc
            if proc:
                if not _exec_scalar(cur, "select object_id(?)", proc):
                    return f"Error: procedure '{proc}' does not exist — deploy it first (deploy_sql_object)."

            cur.execute(
                "select csAIAgentsToolsId, csAIAgentsToolsG, name, SQLProcedure, type, description "
                "from dbo.csAIAgentsTools with(nolock) where name=? or (SQLProcedure in (?, ?) and ?<>N'')",
                nm, proc, proc[4:] if proc else "", proc,
            )
            t = cur.fetchone()
            row: dict = {"name": nm, "type": tool_type}
            if description is not None:
                row["description"] = description
            if proc:
                row["SQLProcedure"] = proc
            if use_permissions is not None:
                row["usePermissions"] = _as_int(use_permissions)
            if t:
                row.update({"_opr": "U", "csAIAgentsToolsId": int(t[0]),
                            "csAIAgentsToolsG": str(t[1]).upper(),
                            "SQLProcedure": proc or t[3], "type": tool_type or t[4]})
                if description is None:
                    row["description"] = t[5]
                tool_g = str(t[1]).upper()
                mode = "updated"
            else:
                if not proc:
                    return "Error: sql_procedure is required to register a NEW tool."
                tool_g = _new_guid()
                row.update({"_opr": "I", "csAIAgentsToolsG": tool_g})
                mode = "created"
            resp = _jsonsave(cur, "csAIAgentsToolsJSONSave", [row])
            if resp:
                return f"csAIAgentsToolsJSONSave ERROR:\n{resp}"
            out.append(f"TOOL {nm}: {mode} (G={tool_g}, proc={proc or (t[3] if t else '?')}).")

            # --- attach to agents ---
            for a in (agents or []):
                if isinstance(a, dict):
                    a = a.get("agent") or a.get("name") or a.get("csAIAgentsId")
                cur.execute(
                    "select csAIAgentsId, csCompaniesId, name from dbo.csAIAgents with(nolock) "
                    "where name = ? or (csAIAgentsId = try_convert(bigint, ?))",
                    str(a), str(a),
                )
                found = cur.fetchall()
                if not found:
                    out.append(f"  ATTACH SKIPPED: agent '{a}' not found.")
                    continue
                if len(found) > 1:
                    out.append(f"  ATTACH AMBIGUOUS: '{a}' matches {len(found)} agents "
                               f"({', '.join(str(r[0]) for r in found)}) — pass csAIAgentsId.")
                    continue
                agent_id, comp_id, agent_name = int(found[0][0]), int(found[0][1]), found[0][2]
                dup = _exec_scalar(
                    cur, "select count(*) from dbo.csAIAgentsToolsAgents with(nolock) "
                         "where csAIAgentsId=? and csAIAgentsToolsG=?", agent_id, tool_g)
                if dup:
                    out.append(f"  ATTACHED already: {agent_name} ({agent_id}).")
                    continue
                resp = _jsonsave(cur, "csAIAgentsToolsAgentsJSONSave", [{
                    "_opr": "I", "csAIAgentsToolsAgentsG": _new_guid(),
                    "csCompaniesId": comp_id, "csAIAgentsId": agent_id,
                    "csAIAgentsToolsG": tool_g,
                }])
                if resp:
                    return f"csAIAgentsToolsAgentsJSONSave ERROR ({agent_name}):\n{resp}"
                out.append(f"  ATTACHED: {agent_name} ({agent_id}, company {comp_id}).")

    if params is not None:
        out.append(ai_tool_sync_params(connection_string, nm, params, generate_sync_script=False))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 31. ng_create_lookup_window — project-convention lookup generator + post-fixes
# ---------------------------------------------------------------------------

def ng_create_lookup_window(
    connection_string: str,
    table_name: str,
    visible_fields: Optional[Sequence[str]] = None,
    run_validator: bool = True,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Create a dedicated lookup window '<Table>Lookup' via csCreateNGDictFromTableDef
    (@DictType='lookup') and fix the classic auto-gen pitfalls:
      - viewHTML must be NULL (empty string -> 'window has no template'),
      - onlyAsLookup=1,
      - at least one VISIBLE layout column (c-list renders visible LayoutsCols;
        none visible = empty rows) — visible_fields sets them, otherwise the first
        string field is made visible.
    Then runs csNGValidateWindowForAI. Wire the lookup on the host window with
    ng_add_lookup afterwards (this tool only creates the lookup window itself).
    Idempotent: the generator skips every existing element (where not exists).
    """
    tbl = (table_name or "").strip()
    if not tbl:
        return "Error: table_name is required."
    ident = tbl + "Lookup"
    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            tg = _exec_scalar(cur, "select csSysTablesG from dbo.csSysTables with(nolock) where tableName=?", tbl)
            if not tg:
                return f"Error: table '{tbl}' not registered in csSysTables."
            pre_existing = _exec_scalar(
                cur, "select count(*) from dbo.csNGAppWindows with(nolock) "
                     "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, ident)

            cur.execute(
                "declare @response xml; "
                "exec dbo.csCreateNGDictFromTableDef @csSysTablesG=?, @DictType=N'lookup', @response=@response out; "
                "select convert(nvarchar(max), @response);", tg)
            resp = cur.fetchone()
            resp_txt = _xml_response_to_text(resp[0] if resp else None)
            if resp_txt and "message_type=\"1\"" in resp_txt.replace("'", '"'):
                return f"csCreateNGDictFromTableDef ERROR:\n{resp_txt}"
            out.append(f"GENERATOR: {'window existed — elements completed idempotently' if pre_existing else 'lookup window created'} ({ident}).")

            # --- post-fixes ---
            cur.execute(
                "select csNGAppWindowsId, csNGAppWindowsG, onlyAsLookup, viewHTML "
                "from dbo.csNGAppWindows with(nolock) where csAppNameSpacesG=? and appWindowIdent=?",
                namespace_g, ident)
            w = cur.fetchone()
            if not w:
                return "\n".join(out) + f"\nError: window '{ident}' still missing after generator run."
            fix: dict = {}
            if w[3] is not None and not (w[3] or "").strip():
                fix["viewHTML"] = None
            if not w[2]:
                fix["onlyAsLookup"] = 1
            if fix:
                fix.update({"_opr": "U", "csNGAppWindowsId": int(w[0]),
                            "csNGAppWindowsG": str(w[1]).upper(),
                            "csAppNameSpacesG": namespace_g, "appWindowIdent": ident})
                resp2 = _jsonsave(cur, "csNGAppWindowsJSONSave", [fix])
                if resp2:
                    return "\n".join(out) + f"\ncsNGAppWindowsJSONSave ERROR:\n{resp2}"
                out.append(f"  fixed: {', '.join(k for k in fix if not k.startswith(('_', 'cs', 'app')))}")

            vis_cnt = _exec_scalar(
                cur, "select count(*) from dbo.csNGAppWindowDataSetsLayoutsCols with(nolock) "
                     "where csAppNameSpacesG=? and appWindowIdent=? and isVisible=1", namespace_g, ident)
            targets = list(visible_fields or [])
            if not targets and not vis_cnt:
                first_str = _exec_scalar(
                    cur,
                    "select top 1 dataFieldIdent from dbo.csNGAppWindowDataSetsFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and formatType=N'string' "
                    "and dataFieldIdent not like N'%G' order by ord", namespace_g, ident)
                if first_str:
                    targets = [first_str]
            if targets:
                cols = [{"field": f, "visible": 1, "ord": i + 1, "width": 240.0}
                        for i, f in enumerate(targets)]
                out.append("  layout: " + ng_bulk_layout(connection_string, ident, cols))
            else:
                out.append(f"  layout: {vis_cnt} visible column(s) — OK.")

            no_header = _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindowDataSetsLayoutsCols lc with(nolock) "
                "join dbo.csNGAppWindowDataSetsFields f with(nolock) on f.csAppNameSpacesG=lc.csAppNameSpacesG "
                " and f.appWindowIdent=lc.appWindowIdent and f.dataSetIdent=lc.dataSetIdent "
                " and f.dataFieldIdent=lc.dataFieldIdent "
                "where lc.csAppNameSpacesG=? and lc.appWindowIdent=? and lc.isVisible=1 "
                "and isnull(f.dataFieldColDesc_PL, N'') = N''", namespace_g, ident)
            if no_header:
                out.append(f"  WARN: {no_header} visible column(s) without col header (dataFieldColDesc_PL) "
                           "— empty header in c-list; fix with ng_set_field_labels.")

            if run_validator:
                cur.execute(
                    "declare @e int, @w int; "
                    "exec dbo.csNGValidateWindowForAI @appWindow=?, @errorCount=@e out, @warningCount=@w out, @quiet=1; "
                    "select @e, @w;", ident)
                r = cur.fetchone()
                out.append(f"VALIDATOR: errors={r[0]}, warnings={r[1]}"
                           + (" — run csNGValidateWindowForAI without @quiet for details." if (r[0] or r[1]) else ""))
    out.append(f"NEXT: wire on the host window: ng_add_lookup(..., lookup_window_ident='{ident}').")
    return "\n".join(out)


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
    "describe",
    "sql_grep",
    "ng_preview_dataset",
    "ng_bulk_layout",
    "ng_register_translates",
    "ng_add_linked_window",
    "ng_add_filter",
    "ng_add_action",
    "ng_ensure_privileges",
    "ng_add_menu_entry",
    "ng_set_sort",
    "ng_diff_with_dict",
    "help_upsert_topic",
    "ai_tool_register",
    "ng_create_lookup_window",
}


def tool_descriptors():
    from mcp.types import Tool

    return [
        Tool(
            name="describe",
            description=(
                "Compact schema of a DB object: for a table/view -> columns "
                "(name | type(len) NULL/NOT NULL [PK][identity][-> ref]); for a "
                "procedure/function -> parameter list. Use INSTEAD of guessing "
                "column names (avoids repeated 'Invalid column name')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Object name (dbo. prefix optional), e.g. 'csNGAppWindowDataSetsActionsFields'."},
                },
                "required": ["object_name"],
            },
        ),
        Tool(
            name="sql_grep",
            description=(
                "Case-insensitive substring search over SQL object bodies "
                "(sys.sql_modules). Returns object:line:content hits, like Grep over "
                "files. Optional name_like narrows candidate objects. Use to locate "
                "where a column/string is built instead of ad-hoc LIKE + substring."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Literal substring to find (case-insensitive)."},
                    "name_like": {"type": "string", "description": "Optional: only objects whose name contains this."},
                    "top": {"type": "integer", "description": "Max hits (default 100)."},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="ng_preview_dataset",
            description=(
                "Dry-run an NG dataset the way the runtime does: expand /*FIELDS*/ + "
                "stmSQL template into the real data SELECT and EXECUTE it (top N, for "
                "json). Catches reserved-word / invalid-column / missing-@var errors "
                "that csNGValidateWindowForAI (config-only) misses. Returns generated "
                "SQL + first rows, or the SQL + exact runtime error."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "where": {"type": "string", "description": "Optional @where JSON (default null)."},
                    "top": {"type": "integer", "description": "Row cap 1..50 (default 5)."},
                    "cs_companies_id": {"type": "integer", "description": "Company context (default: min company)."},
                    "cs_usr_id": {"type": "integer", "description": "User context (default: min user)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_bulk_layout",
            description=(
                "Bulk upsert grid layout columns in one call. columns: "
                "[{field, visible?, ord?, width?, group?}]. Same pitfalls as "
                "ng_set_layout_col (minimal-U, int isVisible, non-null width on INSERT, "
                "group existence check). Ideal for hiding technical cols + reordering a "
                "whole grid after csCreateNGWindowFromTableForAI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{field, visible?, ord?, width?, group?}] — group='' detaches.",
                    },
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "columns"],
            },
        ),
        Tool(
            name="ng_register_translates",
            description=(
                "Register gT() idents on a window (csNGAppWindowTranslates). Each item: "
                "{ident, cs_translate_g?} to reuse, OR {ident, PL, EN, ...} to "
                "reuse-by-content (match Content_PL+Content_EN) or create a new "
                "csTranslate. Idempotent on (appWindowIdent, translateIdent)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "translates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{ident, cs_translate_g?} | {ident, PL, EN, DE, ...}]",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "translates"],
            },
        ),
        Tool(
            name="ng_add_linked_window",
            description=(
                "Link a detail window to a master (csNGAppWindowsLinks + LinksFields) in "
                "one call. map_fields: [{from, to?}] (master main field -> detail where-field). "
                "placement: bottom-panel|outer-side-panel|side-panel|inner-side-panel. "
                "Optional tab_default sets master where-field 'tabIdent-<placement>' (multi-tab "
                "placements). labels {PL,EN,..} = tab caption. linkedWindows cache rebuilds "
                "automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident_from": {"type": "string"},
                    "app_window_ident_to": {"type": "string"},
                    "placement": {"type": "string"},
                    "map_fields": {"type": "array", "items": {"type": "object"}, "description": "[{from, to?}]"},
                    "ord": {"type": "integer", "description": "Tab order (default 1)."},
                    "labels": {"type": "object", "description": "{PL,EN,DE,..} tab caption."},
                    "tab_default": {"type": "string", "description": "appWindowIdentTo of the default tab (for multi-tab placement)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident_from", "app_window_ident_to", "placement", "map_fields"],
            },
        ),
        Tool(
            name="ng_add_filter",
            description=(
                "Create a filter-panel where-field (+ optional lookup wiring + watermark) "
                "in one call. Lookup hosts (string) default notUseForGetData=1. Provide "
                "watermark {PL,EN,..} for :showLabel=false fields. lookup_window_ident + "
                "lookup_sets wire ng_add_lookup with source_kind='where'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "format_type": {"type": "string", "description": "string|integer|boolean|date|..."},
                    "sql_base_type": {"type": "string", "description": "nvarchar|bigint|int|bit|date|..."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "sql_column_params": {"type": "string", "description": "e.g. '(max)'."},
                    "value_def": {"type": "string", "description": "dataFieldValueDef (radiogroup/checkbox default)."},
                    "not_use_for_get_data": {"type": "boolean", "description": "Default: true for string lookup hosts."},
                    "ord": {"type": "integer"},
                    "label_pl": {"type": "string"},
                    "label_en": {"type": "string"},
                    "watermark": {"type": "object", "description": "{PL,EN,..} placeholder for :showLabel=false."},
                    "lookup_window_ident": {"type": "string", "description": "Optional: wire a filter lookup."},
                    "lookup_sets": {"type": "array", "items": {"type": "object"}, "description": "[{from_field, to_field?, source_kind_to?}]"},
                    "lookup_gets": {"type": "array", "items": {"type": "object"}, "description": "[{value|from_field, to_field}]"},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "format_type", "sql_base_type"],
            },
        ),
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
                "sets labelDataFieldIdent and a non-null width to avoid silent collapse. "
                "Translated fields (*Desc over *Desc_PL/EN columns): pass is_translate=true — "
                "sets isTranslate=1 + dataFieldIdentBeforeTranslate with the mandatory trailing '_'."
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
                    "is_translate": {"type": "boolean", "description": "Translated field (*Desc_PL/EN... columns): isTranslate=1 + dataFieldIdentBeforeTranslate. Default false."},
                    "before_translate": {"type": "string", "description": "Column prefix for translated field (default '<field_ident>_'; trailing '_' enforced). Only with is_translate."},
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
        Tool(
            name="ng_add_action",
            description=(
                "Register/update an NG dataset action with conventions handled: crud='ins'|'upd'|'del' "
                "presets the standard auto-action flags; custom action gets isAuto=0/ord=max+1/"
                "position='default'. labels {PL,EN,..} -> actionDesc_*. fields -> ActionsFields. "
                "Wires granular ActionsPrivileges automatically. Reminds/performs the rights-cache "
                "rebuild (button invisible without it)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "action_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "labels": {"type": "object", "description": "{PL,EN,..} -> actionDesc_*."},
                    "sql_name": {"type": "string", "description": "SQLName, e.g. 'dbo.csFooJSONSave' or a dispatcher proc."},
                    "kind": {"type": "string"},
                    "crud": {"type": "string", "enum": ["ins", "upd", "del"], "description": "Standard CRUD preset."},
                    "show_view": {"type": "boolean", "description": "1 = action opens a form (.vue)."},
                    "view_html": {"type": "string", "description": "Action form template (implies showView=1)."},
                    "ord": {"type": "integer"},
                    "hide_when_empty": {"type": "boolean"},
                    "show_confirmation": {"type": "boolean"},
                    "add_current_row": {"type": "boolean"},
                    "add_where": {"type": "boolean"},
                    "ref_kind": {"type": "integer"},
                    "close_after_exec": {"type": "boolean"},
                    "is_auto": {"type": "boolean"},
                    "fields": {"type": "array", "items": {"type": "object"},
                               "description": "[{dataFieldIdent, dataFieldValueDef?, dataFieldIdentForNewRowValue?}]"},
                    "extra": {"type": "object", "description": "Extra column overrides (advanced)."},
                    "wire_privileges": {"type": "boolean", "description": "Default true."},
                    "rebuild": {"type": "boolean", "description": "Rebuild rights cache afterwards."},
                    "cs_companies_id": {"type": "integer", "description": "Scope for the rebuild."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "action_ident"],
            },
        ),
        Tool(
            name="ng_ensure_privileges",
            description=(
                "Audit and repair NG window privileges: no csNGAppWindowsPrivileges row = eternal "
                "spinner. Creates a full-rights privilege when missing (create_if_missing), reports/"
                "fixes granular gaps (datasets/actions not covered — fix_gaps), grants to a user "
                "(csCompaniesUsrsPrivileges) and rebuilds the rights cache."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "create_if_missing": {"type": "boolean", "description": "Default true."},
                    "privilege_desc_pl": {"type": "string", "description": "Default: window PL desc."},
                    "privilege_desc_en": {"type": "string"},
                    "privilege_group_pl": {"type": "string", "description": "Default 'DSM'."},
                    "fix_gaps": {"type": "boolean", "description": "Insert missing granular dataset/action grants."},
                    "grant_cs_usr_id": {"type": "integer"},
                    "grant_cs_companies_id": {"type": "integer"},
                    "grant_privilege_g": {"type": "string", "description": "Required when the window has >1 privilege."},
                    "rebuild": {"type": "boolean"},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_add_menu_entry",
            description=(
                "Add the NGDict menu entry for an NG window WITHOUT touching the Dict entry (project "
                "rule: both coexist). Clones the Dict predecessor's entry when it exists (labels/"
                "ContentGuid/parent/Id; menuPath slug via generator formula), otherwise creates a "
                "fresh entry under parent_menu_path with labels (csTranslate reuse/create). "
                "Idempotent; reminds/performs the menu cache rebuild."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "dict_app_window": {"type": "string", "description": "Dict predecessor name (default = app_window_ident)."},
                    "parent_menu_path": {"type": "string", "description": "menuPath of the parent node (fresh entries), e.g. '/rozrachunki'."},
                    "labels": {"type": "object", "description": "{PL,EN,..} menu caption (fresh entries; overrides clone captions)."},
                    "menu_path": {"type": "string", "description": "Explicit menuPath (default: parentPath + slug)."},
                    "ord": {"type": "integer", "description": "Menu Id/order (default: clone source or max+1)."},
                    "usable": {"type": "boolean", "description": "Default true."},
                    "rebuild": {"type": "boolean"},
                    "cs_companies_id": {"type": "integer", "description": "Scope for the rebuild."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_set_sort",
            description=(
                "Define/replace a dataset sort in one call: upserts SortIdents (single isDef=1 per "
                "layout — others unset first) and REPLACES the LayoutsColsSortOrder column list. "
                "columns: [{field, desc?}] in order; fields validated; warns on <T>G sort "
                "(business column required)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "object"}, "description": "[{field, desc?}]"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "sort_ident": {"type": "string", "description": "Default 'default'."},
                    "is_def": {"type": "boolean", "description": "Default true."},
                    "labels": {"type": "object", "description": "{PL,EN,..} -> sortDesc_*."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "columns"],
            },
        ),
        Tool(
            name="ng_diff_with_dict",
            description=(
                "READ-ONLY migration gap report: NG window vs Dict/table context "
                "(csNGDictWindowContextForAI + AppWindowXML). Reports: suggested-visible fields "
                "missing from the NG layout, FK lookups not wired, default-sort presence "
                "(<T>G warning), where-field/filter count, Dict vs NG datasets. Use after a "
                "Dict->NG migration BEFORE declaring it complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "dict_app_window": {"type": "string", "description": "Dict window name (default = app_window_ident)."},
                    "table_name": {"type": "string", "description": "Source table (default: resolved by the context proc)."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="help_upsert_topic",
            description=(
                "Upsert a help topic (csHelpContents) + link it to NG windows "
                "(csHelpContentsNGAppWindows) in one call. subject/content/description/keywords: "
                "string (=PL) or {PL,EN,..}. Matches by help_contents_g or Subject_PL; an UNKNOWN "
                "help_contents_g creates the topic WITH that GUID (stable series). Content updates "
                "run a transactional D+I with the same G (links re-attached, persisted length "
                "verified) — csHelpContentsJSONSave U-path silently skips Content_*. WARNs on "
                "external <img> URLs (only inline base64 in Content_* renders)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"description": "String (PL) or {PL,EN,..}."},
                    "content": {"description": "HTML string (PL) or {PL,EN,..}; images inline base64."},
                    "description": {"description": "String or {PL,EN,..}."},
                    "keywords": {"description": "String or {PL,EN,..}."},
                    "window_idents": {"type": "array", "items": {"type": "string"},
                                      "description": "NG windows to link the topic to."},
                    "help_contents_g": {"type": "string", "description": "Topic GUID: existing → update; unknown → INSERT with this GUID (requires subject)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["subject"],
            },
        ),
        Tool(
            name="ai_tool_register",
            description=(
                "Register an AI tool end-to-end: csAIAgentsTools upsert (match by SQLProcedure/name) "
                "+ params sync (ai_tool_sync_params pitfalls handled) + ATTACH to agents via "
                "csAIAgentsToolsAgents — the chronically forgotten step (unattached tool = invisible "
                "to every agent). agents: names or csAIAgentsId."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name (usually = proc name without dbo.)."},
                    "description": {"type": "string", "description": "What the tool does (for the LLM)."},
                    "sql_procedure": {"type": "string", "description": "Backing procedure (dbo. optional). Required for new tools."},
                    "tool_type": {"type": "string", "description": "Default 'function'."},
                    "params": {"type": "array", "items": {"type": "object"},
                               "description": "[{name, type, description, isRequired?, typeJSON?}] — synced via ai_tool_sync_params."},
                    "agents": {"type": "array", "items": {}, "description": "Agent names or csAIAgentsId to attach the tool to."},
                    "use_permissions": {"type": "boolean"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="ng_create_lookup_window",
            description=(
                "Create a dedicated lookup window '<Table>Lookup' via csCreateNGDictFromTableDef "
                "(@DictType='lookup') and fix the auto-gen pitfalls: viewHTML NULL (not ''), "
                "onlyAsLookup=1, at least one VISIBLE layout column (else empty rows in c-list), "
                "col-header warning. Runs csNGValidateWindowForAI. Idempotent. Wire the host field "
                "afterwards with ng_add_lookup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Source cs* table, e.g. 'csHolidays'."},
                    "visible_fields": {"type": "array", "items": {"type": "string"},
                                       "description": "Columns to show in the lookup list (default: first string field)."},
                    "run_validator": {"type": "boolean", "description": "Default true."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["table_name"],
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
            is_translate=bool(arguments.get("is_translate", False)),
            before_translate=arguments.get("before_translate"),
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

    if name == "describe":
        return describe(
            connection_string,
            object_name=arguments.get("object_name", ""),
        )

    if name == "sql_grep":
        return sql_grep(
            connection_string,
            pattern=arguments.get("pattern", ""),
            name_like=arguments.get("name_like"),
            top=int(arguments.get("top") or 100),
        )

    if name == "ng_preview_dataset":
        cid = arguments.get("cs_companies_id")
        uid = arguments.get("cs_usr_id")
        return ng_preview_dataset(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            where=arguments.get("where"),
            top=int(arguments.get("top") or 5),
            cs_companies_id=int(cid) if cid is not None else None,
            cs_usr_id=int(uid) if uid is not None else None,
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_bulk_layout":
        return ng_bulk_layout(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            columns=arguments.get("columns") or [],
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_register_translates":
        return ng_register_translates(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            translates=arguments.get("translates") or [],
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_linked_window":
        return ng_add_linked_window(
            connection_string,
            app_window_ident_from=arguments.get("app_window_ident_from", ""),
            app_window_ident_to=arguments.get("app_window_ident_to", ""),
            placement=arguments.get("placement", ""),
            map_fields=arguments.get("map_fields") or [],
            ord=int(arguments.get("ord") or 1),
            labels=arguments.get("labels"),
            tab_default=arguments.get("tab_default"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_filter":
        return ng_add_filter(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            format_type=arguments.get("format_type", ""),
            sql_base_type=arguments.get("sql_base_type", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            sql_column_params=arguments.get("sql_column_params"),
            value_def=arguments.get("value_def"),
            not_use_for_get_data=arguments.get("not_use_for_get_data"),
            ord=arguments.get("ord"),
            label_pl=arguments.get("label_pl"),
            label_en=arguments.get("label_en"),
            watermark=arguments.get("watermark"),
            lookup_window_ident=arguments.get("lookup_window_ident"),
            lookup_sets=arguments.get("lookup_sets"),
            lookup_gets=arguments.get("lookup_gets"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_action":
        return ng_add_action(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            action_ident=arguments.get("action_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            labels=arguments.get("labels"),
            sql_name=arguments.get("sql_name"),
            kind=arguments.get("kind"),
            crud=arguments.get("crud"),
            show_view=arguments.get("show_view"),
            view_html=arguments.get("view_html"),
            ord=arguments.get("ord"),
            hide_when_empty=arguments.get("hide_when_empty"),
            show_confirmation=arguments.get("show_confirmation"),
            add_current_row=arguments.get("add_current_row"),
            add_where=arguments.get("add_where"),
            ref_kind=arguments.get("ref_kind"),
            close_after_exec=arguments.get("close_after_exec"),
            is_auto=arguments.get("is_auto"),
            fields=arguments.get("fields"),
            extra=arguments.get("extra"),
            wire_privileges=bool(arguments.get("wire_privileges", True)),
            rebuild=bool(arguments.get("rebuild", False)),
            cs_companies_id=arguments.get("cs_companies_id"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_ensure_privileges":
        return ng_ensure_privileges(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            create_if_missing=bool(arguments.get("create_if_missing", True)),
            privilege_desc_pl=arguments.get("privilege_desc_pl"),
            privilege_desc_en=arguments.get("privilege_desc_en"),
            privilege_group_pl=arguments.get("privilege_group_pl") or "DSM",
            fix_gaps=bool(arguments.get("fix_gaps", False)),
            grant_cs_usr_id=arguments.get("grant_cs_usr_id"),
            grant_cs_companies_id=arguments.get("grant_cs_companies_id"),
            grant_privilege_g=arguments.get("grant_privilege_g"),
            rebuild=bool(arguments.get("rebuild", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_menu_entry":
        return ng_add_menu_entry(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            dict_app_window=arguments.get("dict_app_window"),
            parent_menu_path=arguments.get("parent_menu_path"),
            labels=arguments.get("labels"),
            menu_path=arguments.get("menu_path"),
            ord=arguments.get("ord"),
            usable=bool(arguments.get("usable", True)),
            rebuild=bool(arguments.get("rebuild", False)),
            cs_companies_id=arguments.get("cs_companies_id"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_sort":
        return ng_set_sort(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            columns=arguments.get("columns") or [],
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            sort_ident=arguments.get("sort_ident") or "default",
            is_def=bool(arguments.get("is_def", True)),
            labels=arguments.get("labels"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_diff_with_dict":
        return ng_diff_with_dict(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            dict_app_window=arguments.get("dict_app_window"),
            table_name=arguments.get("table_name"),
            data_set_ident=arguments.get("data_set_ident") or "main",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "help_upsert_topic":
        return help_upsert_topic(
            connection_string,
            subject=arguments.get("subject"),
            content=arguments.get("content"),
            description=arguments.get("description"),
            keywords=arguments.get("keywords"),
            window_idents=arguments.get("window_idents"),
            help_contents_g=arguments.get("help_contents_g"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ai_tool_register":
        return ai_tool_register(
            connection_string,
            name=arguments.get("name", ""),
            description=arguments.get("description"),
            sql_procedure=arguments.get("sql_procedure"),
            tool_type=arguments.get("tool_type") or "function",
            params=arguments.get("params"),
            agents=arguments.get("agents"),
            use_permissions=arguments.get("use_permissions"),
        )

    if name == "ng_create_lookup_window":
        return ng_create_lookup_window(
            connection_string,
            table_name=arguments.get("table_name", ""),
            visible_fields=arguments.get("visible_fields"),
            run_validator=bool(arguments.get("run_validator", True)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    raise ValueError(f"Unknown cs tool: {name}")
