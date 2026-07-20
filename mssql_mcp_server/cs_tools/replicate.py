"""ng_replicate_window — pełna replikacja konfiguracji okna NG DEV -> target po G (HARD RULE 24)."""

from __future__ import annotations

import datetime

from typing import List, Optional
from pyodbc import connect

from ._core import DEFAULT_NAMESPACE_G, _exec_scalar, _jsonsave


# ---------------------------------------------------------------------------
# 33. ng_replicate_window — full NG window config DEV -> target, rows copied by G
# ---------------------------------------------------------------------------

# Window-scoped config tables in FK order (parent -> child). The exclude set holds
# CACHE columns rebuilt by JSONSave cascades on the target — copying them verbatim
# would freeze stale JSON (csNGAppWindows.dataSets / linkedWindows, DataSets.fields).
_REPLICATE_TABLES = (
    ("csNGAppWindows", {"dataSets", "linkedWindows"}),
    ("csNGAppWindowDataSets", {"fields"}),
    ("csNGAppWindowDataSetsFields", set()),
    ("csNGAppWindowDataSetsKeyFields", set()),
    ("csNGAppWindowDataSetsLayouts", set()),
    ("csNGAppWindowDataSetsLayoutsCols", set()),
    ("csNGAppWindowDataSetsLayoutsColsSortOrder", set()),
    ("csNGAppWindowDataSetsLayoutsAggrs", set()),
    ("csNGAppWindowDataSetsLayoutsRows", set()),
    ("csNGAppWindowDataSetsSortIdents", set()),
    ("csNGAppWindowDataSetsPageSizesIdents", set()),
    ("csNGAppWindowDataSetsWhereFields", set()),
    ("csNGAppWindowDataSetsActions", set()),
    ("csNGAppWindowDataSetsActionsFields", set()),
    ("csNGAppWindowDataSetsActionsSources", set()),
    ("csNGAppWindowDataSetsActionsDefParams", set()),
    ("csNGAppWindowDataSetsLookupDefs", set()),
    ("csNGAppWindowDataSetsLookupDefsGet", set()),
    ("csNGAppWindowDataSetsLookupDefsSet", set()),
    ("csNGAppWindowDataSetsLookupDefsSortIdents", set()),
    ("csNGAppWindowDataSetsExports", set()),
    ("csNGAppWindowDataSetsImports", set()),
    ("csNGAppWindowColsGroups", set()),
    ("csNGAppWindowTranslates", set()),
    ("csNGAppWindowsPrivileges", set()),
    ("csHelpContentsNGAppWindows", set()),
)


def _table_exists(cur, table: str) -> bool:
    return _exec_scalar(cur, "select object_id(?)", f"dbo.{table}") is not None


def _table_columns(cur, table: str) -> List[str]:
    cur.execute(
        "select c.name from sys.columns c where c.object_id = object_id(?) order by c.column_id",
        f"dbo.{table}",
    )
    return [r[0] for r in cur.fetchall()]


def _json_value(value):
    """pyodbc value -> JSON-safe scalar for a JSONSave payload (openjson converts back).
    bool BEFORE int (bool is an int subclass) — true/false in JSON breaks int columns.
    datetime: max milliseconds — str() emits 6-digit microseconds and SQL `datetime`
    conversion fails on them ('Conversion failed when converting date and/or time')."""
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)  # Decimal / UUID / bytes-repr


def _fetch_dicts(cur, sql: str, *params) -> List[dict]:
    cur.execute(sql, *params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _copy_ref_row(scur, tcur, table: str, gcol: str, g: str, warns: List[str]) -> bool:
    """Ensure a referenced row (csTranslate / csPrivileges) exists on the target —
    copy it from DEV WITH its G when missing. Returns False when it cannot be provided."""
    if _exec_scalar(tcur, f"select count(*) from dbo.{table} with(nolock) where {gcol}=?", g):
        return True
    rows = _fetch_dicts(scur, f"select * from dbo.{table} with(nolock) where {gcol}=?", g)
    if not rows:
        warns.append(f"{table} {g}: missing on DEV too — dependent row skipped.")
        return False
    idcol = f"{table}Id"
    payload = {k: _json_value(v) for k, v in rows[0].items() if k != idcol}
    payload["_opr"] = "I"
    resp = _jsonsave(tcur, f"{table}JSONSave", [payload])
    if resp:
        warns.append(f"{table}JSONSave I {g} ERROR: {resp}")
        return False
    return True


def ng_replicate_window(
    connection_string: str,
    app_window_ident: str,
    dev_connection_string: Optional[str] = None,
    target_label: str = "DEV",
    namespace_g: str = DEFAULT_NAMESPACE_G,
    include_view_html: bool = True,
    include_stmsql: bool = True,
    prune: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Replicate the FULL configuration of one NG window from DEV to the target
    environment (call with server=PROD) — every row is copied WITH ITS DEV G
    (HARD RULE 24: upgrade packages replicate by G; a row created independently on
    the target with a different G blows up the package). Covers window + datasets
    (+ stmSQL) + fields + key fields + layouts/cols/sort/aggrs + where fields +
    actions (+ fields/sources/params + action viewHTML) + lookup defs + links init
    + cols groups + window translates (referenced csTranslate rows are copied when
    missing) + privileges (csPrivileges copied; grants are NOT — env-specific) +
    linked-window links in BOTH directions (with the master's tabIdent-<placement>
    where-field) + help-topic links (skipped when the topic is absent on the target).
    Cache columns (csNGAppWindows.dataSets/linkedWindows, DataSets.fields) are NOT
    copied — target JSONSave cascades rebuild them. prune=True deletes target rows
    (in-scope) that no longer exist on DEV (drift repair per HARD RULE 24), child
    tables first. dry_run=True only reports the planned I/U/D counts.
    NOT replicated: menu entries (ng_add_menu_entry), user/role grants
    (ng_ensure_privileges), viewHTML gdy include_view_html=False, stmSQL gdy
    include_stmsql=False.
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    if target_label.upper() == "DEV" or not dev_connection_string:
        return ("Error: replication source is always DEV — call with server=PROD "
                "(or another non-DEV profile) to pick the target.")

    out: List[str] = [f"REPLICATE {aw}: DEV -> {target_label}"
                      + (" [DRY RUN]" if dry_run else "")]
    warns: List[str] = []
    totals = {"I": 0, "U": 0, "D": 0}
    prune_report: List[str] = []
    delete_batches: List[tuple] = []  # (table, rows) collected forward, executed reversed

    with connect(dev_connection_string, autocommit=True) as src, \
            connect(connection_string, autocommit=True) as tgt:
        scur = src.cursor()
        tcur = tgt.cursor()

        if not _exec_scalar(
                scur, "select count(*) from dbo.csNGAppWindows with(nolock) "
                      "where csAppNameSpacesG=? and appWindowIdent=?", namespace_g, aw):
            return f"Error: window '{aw}' not found on DEV (namespace {namespace_g})."

        def replicate(table: str, exclude: set, where_sql: str, params: tuple,
                      pre_row=None) -> None:
            if not _table_exists(scur, table) or not _table_exists(tcur, table):
                return
            gcol = f"{table}G"
            idcol = f"{table}Id"
            if gcol not in _table_columns(scur, table):
                warns.append(f"{table}: no {gcol} column — skipped.")
                return
            src_rows = _fetch_dicts(
                scur, f"select * from dbo.{table} with(nolock) where {where_sql}", *params)
            tgt_rows = _fetch_dicts(
                tcur, f"select * from dbo.{table} with(nolock) where {where_sql}", *params)
            tgt_by_g = {str(r[gcol]).upper(): r for r in tgt_rows}
            src_gs = set()
            payload: List[dict] = []
            for row in src_rows:
                if pre_row and not pre_row(row):
                    continue
                g = str(row[gcol]).upper()
                src_gs.add(g)
                d = {k: _json_value(v) for k, v in row.items()
                     if k != idcol and k not in exclude}
                existing = tgt_by_g.get(g)
                if existing is not None:
                    d["_opr"] = "U"
                    d[idcol] = _json_value(existing[idcol])
                    totals["U"] += 1
                else:
                    d["_opr"] = "I"
                    totals["I"] += 1
                payload.append(d)
            ins = sum(1 for d in payload if d["_opr"] == "I")
            upd = len(payload) - ins
            if payload:
                out.append(f"  {table}: I={ins} U={upd}")
                if not dry_run:
                    for i in range(0, len(payload), 200):
                        resp = _jsonsave(tcur, f"{table}JSONSave", payload[i:i + 200])
                        if resp:
                            raise RuntimeError(f"{table}JSONSave ERROR:\n{resp}")
            # target rows out of DEV scope = drift (HARD RULE 24: fix by DELETE on target)
            orphans = [r for g, r in tgt_by_g.items() if g not in src_gs]
            if orphans:
                if prune:
                    rows = []
                    for r in orphans:
                        d = {k: _json_value(v) for k, v in r.items() if k not in exclude}
                        d["_opr"] = "D"
                        rows.append(d)
                    delete_batches.append((table, rows))
                else:
                    prune_report.append(
                        f"  {table}: {len(orphans)} orphan row(s) on {target_label} "
                        f"(G: {', '.join(str(r[f'{table}G']).upper() for r in orphans[:5])}"
                        + ("..." if len(orphans) > 5 else "") + ")")

        # --- pre-row guards -------------------------------------------------
        def _pre_translates(row) -> bool:
            return _copy_ref_row(scur, tcur, "csTranslate", "csTranslateG",
                                 str(row["csTranslateG"]).upper(), warns)

        def _pre_privileges(row) -> bool:
            return _copy_ref_row(scur, tcur, "csPrivileges", "csPrivilegesG",
                                 str(row["csPrivilegesG"]).upper(), warns)

        def _pre_help(row) -> bool:
            g = str(row["csHelpContentsG"]).upper()
            if _exec_scalar(tcur, "select count(*) from dbo.csHelpContents with(nolock) "
                                  "where csHelpContentsG=?", g):
                return True
            warns.append(f"help link skipped: csHelpContents {g} absent on {target_label} "
                         "(replicate the topic first, e.g. help_upsert_topic).")
            return False

        def _pre_link(row) -> bool:
            other = row["appWindowIdentTo"] if row["appWindowIdentFrom"] == aw \
                else row["appWindowIdentFrom"]
            if _exec_scalar(tcur, "select count(*) from dbo.csNGAppWindows with(nolock) "
                                  "where csAppNameSpacesG=? and appWindowIdent=?",
                            namespace_g, other):
                return True
            warns.append(f"link {row['appWindowIdentFrom']} -> {row['appWindowIdentTo']} "
                         f"skipped: window '{other}' absent on {target_label}.")
            return False

        # --- window-scoped tables (FK order) --------------------------------
        window_where = "csAppNameSpacesG=? and appWindowIdent=?"
        for table, exclude in _REPLICATE_TABLES:
            exclude = set(exclude)
            if table == "csNGAppWindows" and not include_view_html:
                exclude.add("viewHTML")
            if table == "csNGAppWindowDataSets" and not include_stmsql:
                exclude.add("stmSQL")
            pre = {"csNGAppWindowTranslates": _pre_translates,
                   "csNGAppWindowsPrivileges": _pre_privileges,
                   "csHelpContentsNGAppWindows": _pre_help}.get(table)
            replicate(table, exclude, window_where, (namespace_g, aw), pre)

        # --- From/To-scoped tables (linki okien + init where-fieldów z linków) ---
        links_where = ("(csAppNameSpacesGFrom=? and appWindowIdentFrom=?) "
                       "or (csAppNameSpacesGTo=? and appWindowIdentTo=?)")
        links_params = (namespace_g, aw, namespace_g, aw)
        replicate("csNGAppWindowsLinks", set(), links_where, links_params, _pre_link)
        replicate("csNGAppWindowsLinksFields", set(), links_where, links_params, _pre_link)
        replicate("csNGAppWindowDataSetsLinks", set(), links_where, links_params, _pre_link)

        # master tab where-field (tabIdent-<placement>) — lives on the MASTER window,
        # required for multi-tab placements when replicating a DETAIL window
        link_rows = _fetch_dicts(
            scur, "select * from dbo.csNGAppWindowsLinks with(nolock) "
                  "where csAppNameSpacesGTo=? and appWindowIdentTo=?", namespace_g, aw)
        for lr in link_rows:
            master = lr["appWindowIdentFrom"]
            wf_ident = f"tabIdent-{lr['placement']}"
            if not _exec_scalar(tcur, "select count(*) from dbo.csNGAppWindows with(nolock) "
                                      "where csAppNameSpacesG=? and appWindowIdent=?",
                                namespace_g, master):
                continue
            replicate("csNGAppWindowDataSetsWhereFields", set(),
                      "csAppNameSpacesG=? and appWindowIdent=? and dataFieldIdent=?",
                      (namespace_g, master, wf_ident))

        # --- prune (children first = reversed collection order) --------------
        if prune and delete_batches and not dry_run:
            for table, rows in reversed(delete_batches):
                for i in range(0, len(rows), 200):
                    resp = _jsonsave(tcur, f"{table}JSONSave", rows[i:i + 200])
                    if resp:
                        raise RuntimeError(f"PRUNE {table}JSONSave ERROR:\n{resp}")
                totals["D"] += len(rows)
                out.append(f"  PRUNED {table}: D={len(rows)}")
        elif prune and delete_batches and dry_run:
            for table, rows in delete_batches:
                totals["D"] += len(rows)
                out.append(f"  WOULD PRUNE {table}: D={len(rows)}")

    if prune_report:
        out.append(f"ORPHANS on {target_label} (prune=False — NOT deleted):")
        out.extend(prune_report)
    for w in warns:
        out.append(f"  WARN: {w}")
    out.append(f"TOTAL: I={totals['I']} U={totals['U']} D={totals['D']}"
               + (" (dry run — nothing written)" if dry_run else ""))
    out.append("NOT replicated (env-specific): menu entries (ng_add_menu_entry), "
               "user/role grants (ng_ensure_privileges) — remember rebuild_user_rights "
               f"on {target_label} after granting.")
    return "\n".join(out)
