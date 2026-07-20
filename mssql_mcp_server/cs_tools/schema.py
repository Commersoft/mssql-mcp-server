"""add_cs_column / register_cs_table / register_job — rejestracja tabel, kolumn i jobów cs*."""

from __future__ import annotations

import json

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import (
    COLUMN_DESC_LANGS,
    DEFAULT_NAMESPACE_G,
    _exec_scalar,
    _jsonsave,
    _stable_guid,
    _xml_response_to_text,
)


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


def register_cs_table(
    connection_string: str,
    table_name: str,
    columns: Sequence[dict],
    indexes: Optional[Sequence[dict]] = None,
    description_pl: Optional[str] = None,
    description_en: Optional[str] = None,
    is_managed: bool = False,
    track_changes: bool = False,
) -> str:
    """
    Register a cs* table end-to-end: csSysTablesJSONSave + csSysColumnsJSONSave
    (full 12-language ColumnDesc enforced) + csSysIndexesJSONSave + csSysTablesRebuild
    (both params) + physical verification. Idempotent: existing table/columns/indexes
    are skipped, missing ones added, rebuild always runs.

    columns: [{name, type, params?, nullable?, default?, desc: {EN,PL,DE,FR,ES,IT,NL,PT,RU,UK,SK,SE}}]
      - `type` = bare SQL type ('bigint', 'nvarchar', 'numeric', ...), `params` like '(18,6)'.
    indexes: [{name?, keys: 'col1[,col2...]' or [cols] or full '[c] asc,...', pk?, uq?, clustered?}]
      - default for is_managed=True: pk clustered on <T>Id + unique on <T>G;
      - for is_managed=False at least one index is required (aggregates need a clustered PK).
    is_managed=True auto-prepends <T>Id (bigint) and <T>G (uniqueidentifier) columns when
    missing from `columns` and generates the JSONSave procedure via rebuild.
    GUIDs are deterministic (md5 'table:<T>' / 'col:<T>:<c>' / 'idx:<T>:<name>').
    """
    t = (table_name or "").strip()
    if not t:
        return "Error: table_name is required."
    if not columns:
        return "Error: columns list is required."

    cols = [dict(c) for c in columns]
    if is_managed:
        have = {c.get("name") for c in cols}
        auto = []
        if f"{t}Id" not in have:
            auto.append({"name": f"{t}Id", "type": "bigint", "nullable": False,
                         "desc": {l: "ID" if l not in ("RU", "UK") else "ИД" if l == "RU" else "ІД"
                                  for l in COLUMN_DESC_LANGS}})
        if f"{t}G" not in have:
            auto.append({"name": f"{t}G", "type": "uniqueidentifier", "nullable": False,
                         "desc": {l: "IDG" for l in COLUMN_DESC_LANGS}})
        cols = auto + cols

    for c in cols:
        if not c.get("name") or not c.get("type"):
            return f"Error: each column needs name+type (got: {c})."
        desc = c.get("desc") or {}
        missing = [l for l in COLUMN_DESC_LANGS if not desc.get(l)]
        if missing:
            return (f"Error: column '{c['name']}' desc missing languages: {', '.join(missing)} "
                    f"(all 12 required: {', '.join(COLUMN_DESC_LANGS)}).")

    idxs = [dict(i) for i in (indexes or [])]
    if not idxs:
        if is_managed:
            idxs = [
                {"name": f"pk_{t}", "keys": f"[{t}Id] asc", "pk": True, "uq": True, "clustered": True},
                {"name": f"uq_{t}_G", "keys": f"[{t}G] asc", "pk": False, "uq": True, "clustered": False},
            ]
        else:
            return ("Error: indexes are required for a non-managed table "
                    "(aggregates need at least a clustered PK).")

    def _norm_keys(keys) -> str:
        if isinstance(keys, (list, tuple)):
            return ",".join(f"[{k}] asc" for k in keys)
        s = str(keys).strip()
        if "[" in s:
            return s
        return ",".join(f"[{p.strip()}] asc" for p in s.split(",") if p.strip())

    out: List[str] = []
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            tbl_g = _exec_scalar(
                cur, "select csSysTablesG from dbo.csSysTables with(nolock) where TableName = ?", t)
            if tbl_g:
                tbl_g = str(tbl_g).upper()
                out.append(f"TABLE exists in csSysTables (G={tbl_g}).")
            else:
                tbl_g = _stable_guid(cur, f"table:{t}")
                row = {
                    "_opr": "I", "csSysTablesG": tbl_g, "TableName": t,
                    "TrackChanges": 1 if track_changes else 0,
                    "HardErrorChecking": 0, "DataScriptingLevel": 0,
                    "IsManagedTable": 1 if is_managed else 0,
                }
                if description_pl:
                    row["Description_PL"] = description_pl
                if description_en:
                    row["Description_EN"] = description_en
                resp = _jsonsave(cur, "csSysTablesJSONSave", [row])
                if resp:
                    return f"csSysTablesJSONSave ERROR:\n{resp}"
                out.append(f"TABLE registered (G={tbl_g}, managed={int(is_managed)}, "
                           f"trackChanges={int(track_changes)}).")

            existing_cols = {
                r[0] for r in cur.execute(
                    "select ColumnName from dbo.csSysColumns with(nolock) where csSysTablesG = ?",
                    tbl_g).fetchall()
            }
            max_ord = int(_exec_scalar(
                cur, "select isnull(max(ColumnOrder),0) from dbo.csSysColumns with(nolock) "
                     "where csSysTablesG = ?", tbl_g) or 0)
            new_rows = []
            for c in cols:
                if c["name"] in existing_cols:
                    continue
                max_ord += 1
                row = {
                    "_opr": "I",
                    "csSysColumnsG": _stable_guid(cur, f"col:{t}:{c['name']}"),
                    "csSysTablesG": tbl_g,
                    "ColumnName": c["name"],
                    "ColumnOrder": max_ord,
                    "BaseType": c["type"],
                    "IsNullable": 1 if c.get("nullable", True) else 0,
                }
                if c.get("params"):
                    row["ColumnParams"] = c["params"]
                if c.get("default"):
                    row["DefaultDef"] = c["default"]
                for lang in COLUMN_DESC_LANGS:
                    row[f"ColumnDesc_{lang}"] = c["desc"][lang]
                new_rows.append(row)
            if new_rows:
                resp = _jsonsave(cur, "csSysColumnsJSONSave", new_rows)
                if resp:
                    return f"csSysColumnsJSONSave ERROR:\n{resp}"
                out.append(f"COLUMNS added: {len(new_rows)} "
                           f"({', '.join(r['ColumnName'] for r in new_rows)}).")
            else:
                out.append("COLUMNS: nothing to add (all present).")

            existing_idx = {
                r[0] for r in cur.execute(
                    "select IndexName from dbo.csSysIndexes with(nolock) where csSysTablesG = ?",
                    tbl_g).fetchall()
            }
            idx_rows = []
            for i, ix in enumerate(idxs, start=1):
                nm = ix.get("name") or f"ix_{t}_{i:02d}"
                if nm in existing_idx:
                    continue
                idx_rows.append({
                    "_opr": "I",
                    "csSysIndexesG": _stable_guid(cur, f"idx:{t}:{nm}"),
                    "csSysTablesG": tbl_g,
                    "IndexName": nm,
                    "IsPK": 1 if ix.get("pk") else 0,
                    "IsUQ": 1 if (ix.get("uq") or ix.get("pk")) else 0,
                    "IsClustered": 1 if ix.get("clustered") else 0,
                    "KeyFields": _norm_keys(ix.get("keys") or ""),
                    "isActive": 1,
                })
            if idx_rows:
                resp = _jsonsave(cur, "csSysIndexesJSONSave", idx_rows)
                if resp:
                    return f"csSysIndexesJSONSave ERROR:\n{resp}"
                out.append(f"INDEXES added: {len(idx_rows)} "
                           f"({', '.join(r['IndexName'] for r in idx_rows)}).")
            else:
                out.append("INDEXES: nothing to add (all present).")

            cur.execute(
                "declare @r xml; "
                "exec dbo.csSysTablesRebuild @csSysTablesG = ?, @TableName = ?, @response = @r out; "
                "select convert(nvarchar(max), @r) [response];",
                tbl_g, t,
            )
            r = cur.fetchone()
            resp = _xml_response_to_text(r[0] if r else None)
            if resp:
                return f"csSysTablesRebuild WARNING:\n{resp}\nDone so far:\n  " + "\n  ".join(out)

            phys = _exec_scalar(cur, "select object_id(?)", f"dbo.{t}")
            pk_cnt = _exec_scalar(
                cur, "select count(*) from sys.indexes where object_id = object_id(?) "
                     "and is_primary_key = 1", f"dbo.{t}")
            jsave = _exec_scalar(cur, "select object_id(?)", f"dbo.{t}JSONSave")
            out.append(f"REBUILD OK: physical={bool(phys)}, PK={int(pk_cnt or 0)}, "
                       f"JSONSave={'present' if jsave else ('MISSING!' if is_managed else 'n/a (not managed)')}")

    return f"OK: register_cs_table {t}\n  " + "\n  ".join(out)


# ---------------------------------------------------------------------------
# 3c. register_job — csCompaniesJobs registration with project conventions
# ---------------------------------------------------------------------------

def register_job(
    connection_string: str,
    procedure_name: str,
    cs_companies_id: int,
    start_time: str,
    interval_seconds: int,
    job_desc_pl: str,
    job_desc_en: Optional[str] = None,
    thread_no: int = 8100,
    job_order: Optional[int] = None,
    active: bool = True,
) -> str:
    """
    Register a job in csCompaniesJobs (idempotent by company+ProcedureName).
    Conventions handled: 'dbo.' prefix on ProcedureName, stable G
    (md5 'job:<company>:<proc>'), JobOrder = max+100 when omitted, wrapper-proc
    existence check. start_time = 'YYYY-MM-DDTHH:MM:SS' (for weekly jobs pick the
    correct weekday!); interval_seconds: 86400 = daily, 604800 = weekly.
    """
    pname = (procedure_name or "").strip()
    if not pname:
        return "Error: procedure_name is required."
    if not pname.lower().startswith("dbo."):
        pname = f"dbo.{pname}"
    if not job_desc_pl:
        return "Error: job_desc_pl is required."

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            proc_exists = _exec_scalar(cur, "select object_id(?)", pname)
            existing = cur.execute(
                "select csCompaniesJobsId, StartTime, Interval, Active from dbo.csCompaniesJobs with(nolock) "
                "where csCompaniesId = ? and ProcedureName = ?",
                int(cs_companies_id), pname,
            ).fetchone()
            if existing:
                return (f"EXISTS: job {pname} already registered for company {cs_companies_id} "
                        f"(Id={existing[0]}, StartTime={existing[1]}, Interval={existing[2]}, "
                        f"Active={existing[3]}). Nothing changed.")

            if job_order is None:
                job_order = int(_exec_scalar(
                    cur, "select isnull(max(JobOrder),0)+100 from dbo.csCompaniesJobs with(nolock) "
                         "where csCompaniesId = ?", int(cs_companies_id)) or 100)

            bare = pname[4:]
            row = {
                "_opr": "I",
                "csCompaniesJobsG": _stable_guid(cur, f"job:{int(cs_companies_id)}:{bare}"),
                "csCompaniesId": int(cs_companies_id),
                "csAppNameSpacesG": DEFAULT_NAMESPACE_G,
                "ProcedureName": pname,
                "StartTime": start_time,
                "Interval": int(interval_seconds),
                "Active": 1 if active else 0,
                "ThreadNo": int(thread_no),
                "JobOrder": int(job_order),
                "JobDesc_PL": job_desc_pl,
            }
            if job_desc_en:
                row["JobDesc_EN"] = job_desc_en
            resp = _jsonsave(cur, "csCompaniesJobsJSONSave", [row])
            if resp:
                return f"csCompaniesJobsJSONSave ERROR:\n{resp}"

            jid = _exec_scalar(
                cur, "select csCompaniesJobsId from dbo.csCompaniesJobs with(nolock) "
                     "where csCompaniesId = ? and ProcedureName = ?",
                int(cs_companies_id), pname)

    warn = "" if proc_exists else f"\nWARNING: procedure {pname} does NOT exist on this server yet!"
    return (f"OK: job {pname} registered (Id={jid}, company={cs_companies_id}, "
            f"StartTime={start_time}, Interval={interval_seconds}s, ThreadNo={thread_no}, "
            f"JobOrder={job_order}, Active={int(active)}).{warn}")
