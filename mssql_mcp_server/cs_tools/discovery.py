"""Read-only: describe / sql_grep / ng_preview_dataset / ng_diff_with_dict."""

from __future__ import annotations

import re

from typing import List, Optional
from pyodbc import connect

from ._core import DEFAULT_NAMESPACE_G, _exec_scalar


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
