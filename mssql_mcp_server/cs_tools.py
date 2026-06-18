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
# MCP tool descriptors + dispatcher
# ---------------------------------------------------------------------------

CS_TOOL_NAMES = {
    "deploy_sql_object",
    "cs_jsonsave",
    "add_cs_column",
    "add_ng_field",
    "get_cs_object_versions",
    "update_view_html",
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

    raise ValueError(f"Unknown cs tool: {name}")
