"""Konfiguracja okna NG: pola, viewHTML, layout, grupy kolumn, stmSQL, propsy datasetu, translaty, sort."""

from __future__ import annotations

import json
import re

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import (
    DEFAULT_NAMESPACE_G,
    NG_COLSGROUP_LANGS,
    NG_DATASET_PROPS_WHITELIST,
    NG_LABEL_LANGS,
    _as_int,
    _exec_scalar,
    _jsonsave,
    _new_guid,
    _stable_guid,
    _xml_response_to_text,
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
# 6. update_view_html  (sync .vue <template> -> DB viewHTML, like husky)
# ---------------------------------------------------------------------------

_TEMPLATE_TAG_RE = re.compile(r"<template(?=[\s>])|</template\s*>", re.IGNORECASE)


def _extract_template(vue_source: str) -> Optional[str]:
    """
    Extract the inner content of the ROOT <template>...</template> block: trim each
    line, drop blanks, join with CRLF, then ##asterix## -> *  (content convention of
    csNGAppWindows.viewHTML, same as the husky hook).

    The closing boundary is found by NESTING COUNT, not by matching a line that is
    exactly '</template>'. Line matching picked the wrong boundary in both directions
    and silently truncated viewHTML in the DB (runtime X_MISSING_END_TAG, 2026-07-28):
      - root '</template>' does not have to sit alone on its line
        (csB2BPortalsSearchWords had '  </c-window-auto></template>'),
      - while a NESTED <template v-for> / <template v-else> / <template #slot> closes
        with '</template>' alone on its line — so it looked like the root.

    A SELF-CLOSING nested template ('<template #header />') has no closing tag, so it
    must NOT increase the depth — otherwise depth never returns to 0 and the whole file
    is rejected (incydent csUsrEMailsInbox 2026-07-28).
    """
    open_m = re.search(r"<template\s*>", vue_source, re.IGNORECASE)
    if open_m is None:
        return None

    depth = 0
    close_at = None
    for m in _TEMPLATE_TAG_RE.finditer(vue_source, open_m.start()):
        if m.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                close_at = m.start()
                break
        else:
            gt = vue_source.find(">", m.end())
            if gt != -1 and vue_source[gt - 1] == "/":
                continue  # self-closing <template ... /> — no matching close tag
            depth += 1
    if close_at is None:
        return None

    inner = [line.strip() for line in vue_source[open_m.end():close_at].splitlines()]
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
            # utf-8-sig: BOM z .vue nie może wjechać na początek viewHTML w bazie
            with open(file_path, "r", encoding="utf-8-sig") as fh:
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
# 10b. ng_upsert_tabs_group
# ---------------------------------------------------------------------------

def ng_upsert_tabs_group(
    connection_string: str,
    app_window_ident: str,
    tab_group_ident: str,
    descriptions: Optional[dict] = None,
    ord: Optional[int] = None,
    translate_ident: Optional[str] = None,
    link_to_windows: Optional[Sequence[str]] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Upsert a tabs group of linked windows (csNGAppWindowTabsGroups — per MASTER window, the
    analog of csNGAppWindowColsGroups). app_window_ident = the window hosting the tab bar
    (= csNGAppWindowsLinks.appWindowIdentFrom). Column names are tabGroupDesc_XX; only the
    provided languages are written (HARD RULE 13), PL is required on create (NOT NULL).
    G of a new row is the stable md5('tabsGroup:<window>:<ident>') so DEV and PROD match
    (HARD RULE 24). `ord` = explicit group order on the vertical tab bar (NULL = position of
    the group's first tab); `translate_ident` = optional gT fallback (tabGroupTranslateIdent).
    link_to_windows: appWindowIdentTo of existing links to attach (U with tabGroupIdent) —
    done AFTER the group row exists because of the FK; the JSONSave custom code refreshes the
    master's linkedWindows cache. Groups render only in vertical tab layouts.
    """
    descriptions = descriptions or {}
    if descriptions and not isinstance(descriptions, dict):
        return "Error: descriptions must be a dict, e.g. {'PL': 'Oferta', 'EN': 'Offer'}."
    bad = [l for l in descriptions if l not in NG_COLSGROUP_LANGS]
    if bad:
        return f"Error: unsupported language(s): {', '.join(bad)} (allowed: {', '.join(NG_COLSGROUP_LANGS)})."
    if not app_window_ident or not tab_group_ident:
        return "Error: app_window_ident and tab_group_ident are required."

    warnings: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            if not _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindows with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ?",
                namespace_g, app_window_ident,
            ):
                return (f"Error: master window '{app_window_ident}' not found in csNGAppWindows "
                        f"(namespace {namespace_g}) — the tabs group belongs to the window hosting the tab bar.")

            cur.execute(
                "select csNGAppWindowTabsGroupsId, csNGAppWindowTabsGroupsG "
                "from dbo.csNGAppWindowTabsGroups with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and tabGroupIdent = ?",
                namespace_g, app_window_ident, tab_group_ident,
            )
            g = cur.fetchone()
            if not g and not descriptions.get("PL"):
                return "Error: tabGroupDesc_PL is NOT NULL — pass descriptions with at least 'PL' when creating a group."

            rec = {
                "_opr": "U" if g else "I",
                "csNGAppWindowTabsGroupsG": str(g[1]).upper() if g else _stable_guid(
                    cur, f"tabsGroup:{app_window_ident}:{tab_group_ident}"),
                "csAppNameSpacesG": namespace_g,
                "appWindowIdent": app_window_ident,
                "tabGroupIdent": tab_group_ident,
            }
            if g:
                rec["csNGAppWindowTabsGroupsId"] = int(g[0])
            for lang, text in descriptions.items():
                rec[f"tabGroupDesc_{lang}"] = text
            if ord is not None:
                rec["ord"] = int(ord)
            if translate_ident is not None:
                rec["tabGroupTranslateIdent"] = translate_ident or None
            # len(rec) == 6 = only the key fields (_opr, Id, G, namespace, window, ident) — no group change;
            # attaching links alone is still a valid call, so don't reject it and don't send an empty update.
            group_changed = not g or len(rec) > 6
            if g and not group_changed and not link_to_windows:
                return (f"Error: nothing to change for existing group '{tab_group_ident}' "
                        "(pass descriptions / ord / translate_ident / link_to_windows).")

            if group_changed:
                resp = _jsonsave(cur, "csNGAppWindowTabsGroupsJSONSave", [rec])
                if resp:
                    return f"TabsGroups JSONSave WARNING:\n{resp}"

            linked = 0
            if link_to_windows:
                rows = []
                for to_ident in link_to_windows:
                    cur.execute(
                        "select csNGAppWindowsLinksId, csNGAppWindowsLinksG, csAppNameSpacesGTo, tabGroupIdent "
                        "from dbo.csNGAppWindowsLinks with(nolock) "
                        "where csAppNameSpacesGFrom = ? and appWindowIdentFrom = ? and appWindowIdentTo = ?",
                        namespace_g, app_window_ident, to_ident,
                    )
                    lk = cur.fetchone()
                    if not lk:
                        warnings.append(f"link {app_window_ident} -> {to_ident} does not exist (ng_add_linked_window first).")
                        continue
                    if lk[3] == tab_group_ident:
                        continue
                    rows.append({
                        "_opr": "U",
                        "csNGAppWindowsLinksId": int(lk[0]),
                        "csNGAppWindowsLinksG": str(lk[1]).upper(),
                        "csAppNameSpacesGFrom": namespace_g,
                        "appWindowIdentFrom": app_window_ident,
                        "csAppNameSpacesGTo": str(lk[2]).upper(),
                        "appWindowIdentTo": to_ident,
                        "tabGroupIdent": tab_group_ident,
                    })
                if rows:
                    resp = _jsonsave(cur, "csNGAppWindowsLinksJSONSave", rows)
                    if resp:
                        return f"OK: group saved, but Links JSONSave WARNING:\n{resp}"
                    linked = len(rows)

    out = [
        f"OK: tabs group '{tab_group_ident}' {('updated' if group_changed else 'unchanged') if g else 'created'} in {app_window_ident} "
        f"(langs: {', '.join(sorted(descriptions)) or '-'}"
        f"{', ord=' + str(ord) if ord is not None else ''}"
        f"{', translateIdent=' + translate_ident if translate_ident else ''}).",
    ]
    if link_to_windows is not None:
        out.append(f"  links attached: {linked} (already in group / skipped: {len(link_to_windows) - linked - len(warnings)}).")
    out.append("  linkedWindows cache of the master refreshed by the JSONSave custom code (on real changes); "
               "groups render only in vertical tab layouts (outer-side-panel-tab-layout='left').")
    out.extend(f"  WARNING: {w}" for w in warnings)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 11. ng_set_stmsql
# ---------------------------------------------------------------------------

STMSQL_TEST_PARAMS_DECL = (
    "@stmSQLOut nvarchar(max) out, @ch nvarchar(2), @csCompaniesIdStr nvarchar(30), "
    "@csCompaniesId bigint, @csUsrId bigint, @isRefreshOneRecord int, "
    "@LanguageSuffix nvarchar(2), @where nvarchar(max), @whereLists nvarchar(max)"
)


def _test_stmsql(cur, stm: str, where_json: str) -> Optional[str]:
    """Run the stmSQL template through sp_executesql; return error text or None on success."""
    try:
        cur.execute(
            "declare @stmSQLOut nvarchar(max) = N'', @testUsrId bigint; "
            "select top 1 @testUsrId = cu.csUsrId "
            "from dbo.csCompaniesUsrs cu with(nolock) "
            "where cu.csCompaniesId = 1435126 order by cu.csUsrId; "
            "exec sp_executesql @stmt = ?, @params = N'" + STMSQL_TEST_PARAMS_DECL + "', "
            "@stmSQLOut = @stmSQLOut out, @ch = ?, @csCompaniesIdStr = N'1435126', "
            "@csCompaniesId = 1435126, @csUsrId = @testUsrId, @isRefreshOneRecord = 0, "
            "@LanguageSuffix = N'PL', @where = ?, @whereLists = N'{}'; "
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
