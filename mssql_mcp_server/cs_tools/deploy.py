"""deploy_sql_object / cs_jsonsave / get_cs_object_versions — wdrażanie obiektów SQL i zapisy JSONSave."""

from __future__ import annotations

import json
import re

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import SCRIPT_CONVERTERS_G, _exec_scalar, _new_guid, _xml_response_to_text


# ---------------------------------------------------------------------------
# 1. deploy_sql_object
# ---------------------------------------------------------------------------

def deploy_sql_object(
    connection_string: str,
    object_name: str,
    body: str,
    description: str,
    object_type: str = "procedure",
    dev_connection_string: Optional[str] = None,
    target_label: str = "DEV",
) -> str:
    """
    Deploy a SQL object through csAddObjVer with all version-mechanism pitfalls handled:
      - objectName WITHOUT dbo. prefix (HARD RULE).
      - @pv = latest registered verG (or NULL for a brand-new object).
      - @v = freshly generated, collision-checked GUID.
      - 3-batch deploy (csAddObjVer + DDL body + csSysRestoreObject), no GO sent to driver.
      - orphan cleanup: clears a stuck inProgress=1 row from a prior failed attempt (UPDATE only).

    Multi-server (target_label != 'DEV', e.g. PROD): the version chain stays CONSISTENT —
    @v on the target REUSES the latest DEV verG for the object (DEV registry = source of
    truth), @pv = the target's own latest verG. Requires the object to be deployed on DEV
    FIRST; if the DEV latest verG already exists on the target, the deploy is skipped
    (idempotent). NEVER patch managed objects with raw ALTER — the registry then carries
    the stale body and the next upgrade package ships the bug.

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

    # CRLF normalization: LF-only bodies land verbatim in sys.sql_modules and break
    # csSysScriptSqlObject (splits lines by CRLF -> whole module becomes one giant line,
    # truncated by SSMS). DB convention is CRLF, same as the repo (npm run crlf).
    body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

    full_name = f"dbo.{name}"
    log: List[str] = []
    is_cross = target_label.upper() != "DEV"

    dev_latest = None
    if is_cross:
        if not dev_connection_string:
            return "Error: cross-server deploy requires the DEV connection (internal)."
        with connect(dev_connection_string, autocommit=True) as dconn:
            with dconn.cursor() as dcur:
                dev_latest = _exec_scalar(
                    dcur,
                    "select top 1 v.verG from dbo.csSysObjVer v with(nolock) "
                    "where v.objectName = ? and v.inProgress = 0 order by v.verId desc",
                    name,
                )
        if not dev_latest:
            return (f"Error: '{name}' has no completed version on DEV — deploy on DEV first "
                    f"(rejestr wersji DEV jest źródłem @v dla {target_label}).")
        dev_latest = str(dev_latest).upper()
        log.append(f"target = {target_label}; @v reused from DEV latest = {dev_latest}")

    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            if is_cross:
                already = _exec_scalar(
                    cur,
                    "select 1 from dbo.csSysObjVer with(nolock) "
                    "where objectName = ? and verG = ? and inProgress = 0",
                    name, dev_latest,
                )
                if already:
                    return (f"SKIPPED: version {dev_latest} of {full_name} is already deployed "
                            f"on {target_label} (idempotent).")

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

            # c) @v: cross-server = DEV latest verG; DEV = fresh unique GUID
            if is_cross:
                v = dev_latest
            else:
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
    target_info = f" [{target_label}]" if is_cross else ""
    return f"{status}{target_info}: {full_name}\n  " + "\n  ".join(log)


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
