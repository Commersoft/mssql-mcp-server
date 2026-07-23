"""Akcje i uprawnienia NG: ng_add_action / ng_ensure_privileges / ng_add_menu_entry / rebuild_user_rights."""

from __future__ import annotations

import re

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import (
    DEFAULT_NAMESPACE_G,
    NG_COLSGROUP_LANGS,
    NG_LABEL_LANGS,
    _as_int,
    _ensure_translate,
    _exec_scalar,
    _jsonsave,
    _new_guid,
    _stable_guid,
)


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

            # --- privileges wiring (granular EDITING privileges only) ---
            # Grant tylko do przywilejow granularnych, ktore JUZ maja ktorys z ins/upd/del —
            # przywileje "tylko podglad" (0 grantow akcji lub sam 'show') pomijamy z ostrzezeniem.
            # Bez tego nowa akcja wykonawcza laduje u userow podgladowych = eskalacja uprawnien
            # (klasa incydentu fix_gaps / ng-window-jsonsave-pitfalls par.34).
            if wire_privileges:
                cur.execute(
                    "select p.csPrivilegesG, p.hasRightsAllDataSets, isnull(pr.PrivilegeDesc_PL, N'') "
                    "from dbo.csNGAppWindowsPrivileges p with(nolock) "
                    "  left join dbo.csPrivileges pr with(nolock) on pr.csPrivilegesG = p.csPrivilegesG "
                    "where p.csAppNameSpacesG=? and p.appWindowIdent=?",
                    namespace_g, aw,
                )
                privs = cur.fetchall()
                wired = 0
                skipped_view_only: List[str] = []
                for pg, all_ds, priv_desc in privs:
                    if all_ds:
                        continue  # hasRightsAllDataSets=1 pokrywa akcje bez grantu granularnego
                    dsp = cur.execute(
                        "select hasRightsAllActions from dbo.csNGAppWindowsDataSetsPrivileges with(nolock) "
                        "where csPrivilegesG=? and csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                        pg, namespace_g, aw, data_set_ident,
                    ).fetchone()
                    if not dsp or dsp[0]:
                        continue
                    cur.execute(
                        "select actionIdent from dbo.csNGAppWindowsDataSetsActionsPrivileges with(nolock) "
                        "where csPrivilegesG=? and csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=?",
                        pg, namespace_g, aw, data_set_ident,
                    )
                    granted = {str(r[0]) for r in cur.fetchall()}
                    if act in granted:
                        continue
                    if not granted & {"ins", "upd", "del"}:
                        skipped_view_only.append(priv_desc or str(pg))
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
                if skipped_view_only:
                    out.append("  SKIPPED view-only privilege(s) (no ins/upd/del grants): "
                               + ", ".join(skipped_view_only)
                               + " — add the grant manually ONLY if the action is intentionally view-safe.")

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
    grant_app_roles_id: Optional[int] = None,
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
      - grant_app_roles_id + grant_cs_companies_id: grants to an APP ROLE via
        csAppRolesPrivileges (PROD standard — role Developer / Dyrektor Logistyki itp.);
        stable G = md5('approlepriv:<companiesId>:<roleId>:<privG>') so re-runs and
        manual work align.
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

            # --- grant to app role (csAppRolesPrivileges — PROD standard) ---
            if grant_app_roles_id:
                if not grant_cs_companies_id:
                    return "\n".join(out) + "\nError: role grant requires grant_cs_companies_id."
                pg = (grant_privilege_g or "").upper()
                if not pg:
                    if len(privs) == 1:
                        pg = str(privs[0][0]).upper()
                    else:
                        return "\n".join(out) + "\nError: window has multiple privileges — pass grant_privilege_g."
                role_desc = _exec_scalar(
                    cur, "select AppRoleDesc_PL from dbo.csAppRoles with(nolock) "
                         "where csCompaniesId=? and csAppRolesId=?",
                    int(grant_cs_companies_id), int(grant_app_roles_id))
                if role_desc is None:
                    return ("\n".join(out) + f"\nError: app role {grant_app_roles_id} not found "
                            f"in company {grant_cs_companies_id} (csAppRoles).")
                already = _exec_scalar(
                    cur,
                    "select count(*) from dbo.csAppRolesPrivileges with(nolock) "
                    "where csCompaniesId=? and csAppRolesId=? and csPrivilegesG=?",
                    int(grant_cs_companies_id), int(grant_app_roles_id), pg,
                )
                if already:
                    out.append(f"GRANT: role '{role_desc}' ({grant_app_roles_id}) already has {pg}.")
                else:
                    seed = f"approlepriv:{int(grant_cs_companies_id)}:{int(grant_app_roles_id)}:{pg}"
                    resp = _jsonsave(cur, "csAppRolesPrivilegesJSONSave", [{
                        "_opr": "I", "csAppRolesPrivilegesG": _stable_guid(cur, seed),
                        "csCompaniesId": int(grant_cs_companies_id),
                        "csAppRolesId": int(grant_app_roles_id), "csPrivilegesG": pg,
                    }])
                    if resp:
                        return f"csAppRolesPrivilegesJSONSave ERROR:\n{resp}"
                    out.append(f"GRANT: privilege {pg} -> role '{role_desc}' ({grant_app_roles_id}, "
                               f"company {grant_cs_companies_id}). Pamiętaj o rebuild praw "
                               f"(rebuild=True lub rebuild_user_rights).")

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
    parent_g: Optional[str] = None,
    labels: Optional[dict] = None,
    menu_path: Optional[str] = None,
    ord: Optional[int] = None,
    kind: str = "NGDict",
    icon: Optional[str] = None,
    usable: bool = True,
    rebuild: bool = False,
    cs_companies_id: Optional[int] = None,
    namespace_g: str = DEFAULT_NAMESPACE_G,
) -> str:
    """
    Add an NG menu entry (Kind='NGDict') or a MENU NODE (kind='Menu'), enforcing the
    project rule: the Dict entry is NEVER replaced — both entries coexist.
      - If the Dict predecessor has a menu entry (dict_app_window, default =
        app_window_ident): the NGDict entry is CLONED from it (same parent/Id/labels/
        ContentGuid/Icon, generator formula for menuPath slug from appWindowDesc_PL).
      - Otherwise a fresh entry is created under the parent given by parent_menu_path
        (menuPath, e.g. '/rozrachunki') OR parent_g (csAppMainMenusItemsG — works for
        NODES that have no menuPath); labels {PL,...} required (ContentGuid is
        reused/created in csTranslate by content — ContentGuid is REQUIRED also for nodes).
      - kind='Menu' creates a grouping NODE (no window, no menuPath): parent_g/
        parent_menu_path + labels required; app_window_ident is used only as a stable
        ident for logs.
    Idempotent: existing NGDict entry -> reports it (and can flip usable).
    REMEMBER: menu changes need the user cache rebuild (rebuild=True + cs_companies_id).
    """
    aw = (app_window_ident or "").strip()
    if not aw:
        return "Error: app_window_ident is required."
    if kind not in ("NGDict", "Menu"):
        return "Error: kind must be 'NGDict' or 'Menu'."
    out: List[str] = []

    def _fetch_parent(cur):
        """Resolve parent by parent_g (works for nodes) or parent_menu_path."""
        if parent_g:
            cur.execute(
                "select csAppMainMenusItemsG, csAppMainMenusG, menuPath "
                "from dbo.csAppMainMenusItems with(nolock) where csAppMainMenusItemsG = ?",
                str(parent_g),
            )
            p = cur.fetchone()
            if not p:
                return None, f"Error: parent menu item with G='{parent_g}' not found."
            return p, None
        if parent_menu_path:
            cur.execute(
                "select csAppMainMenusItemsG, csAppMainMenusG, menuPath "
                "from dbo.csAppMainMenusItems with(nolock) where menuPath = ?",
                parent_menu_path,
            )
            p = cur.fetchone()
            if not p:
                return None, f"Error: parent menu item with menuPath='{parent_menu_path}' not found."
            return p, None
        return None, "Error: pass parent_menu_path or parent_g."

    if kind == "Menu":
        if not labels or not labels.get("PL"):
            return "Error: labels with at least PL are required for a Menu node."
        with connect(connection_string, autocommit=True) as conn:
            with conn.cursor() as cur:
                parent, perr = _fetch_parent(cur)
                if perr:
                    return perr
                dup = _exec_scalar(
                    cur,
                    "select top 1 m.csAppMainMenusItemsId from dbo.csAppMainMenusItems m with(nolock) "
                    "join dbo.csTranslate t with(nolock) on t.csTranslateG = m.ContentGuid "
                    "where m.csAppMainMenusParentItemsG = ? and m.Kind = N'Menu' and t.Content_PL = ?",
                    str(parent[0]), labels["PL"],
                )
                if dup:
                    return f"EXISTS: Menu node '{labels['PL']}' already under this parent (Id={dup})."
                try:
                    content_g, tg_action = _ensure_translate(cur, labels)
                except (ValueError, RuntimeError) as e:
                    return f"Error (csTranslate): {e}"
                max_id = _exec_scalar(
                    cur,
                    "select isnull(max(Id),0) from dbo.csAppMainMenusItems with(nolock) "
                    "where csAppMainMenusG=? and csAppMainMenusParentItemsG=?",
                    parent[1], parent[0],
                )
                row = {
                    "_opr": "I", "csAppMainMenusItemsG": _new_guid(),
                    "csAppMainMenusParentItemsG": str(parent[0]).upper(),
                    "csAppMainMenusG": str(parent[1]).upper(),
                    "Id": int(ord if ord is not None else int(max_id) + 100),
                    "Kind": "Menu", "IsQuick": 0,
                    "ContentGuid": content_g,
                    "notShowInAppMenu": 0, "deprecated": 0, "usable": _as_int(usable),
                }
                if icon:
                    row["Icon"] = icon
                resp = _jsonsave(cur, "csAppMainMenusItemsJSONSave", [row])
                if resp:
                    return f"csAppMainMenusItemsJSONSave ERROR:\n{resp}"
                out.append(f"MENU NODE added: '{labels['PL']}' Id={row['Id']} "
                           f"G={row['csAppMainMenusItemsG']} (csTranslate {tg_action}).")
        if rebuild:
            out.append(rebuild_user_rights(connection_string, cs_companies_id=cs_companies_id))
        else:
            out.append("REMINDER: rebuild_user_rights (menu cache) or the node stays invisible.")
        return "\n".join(out)
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
                # childMenuPath MUSI być spójny z menuPath — łańcuch zapisu (csAppMainMenusItemsJSONSave)
                # traktuje childMenuPath jako źródło prawdy i przelicza z niego menuPath; niespójność
                # cofa jawny menu_path do kolizyjnego sluga (incydent paczek menu 2026-07-21/22)
                child_slug = new_path.rsplit("/", 1)[-1] or slug
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
                    "childMenuPath": child_slug, "menuPath": new_path,
                    "notShowInAppMenu": _as_int(d["notShowInAppMenu"] or 0),
                    "commands": d["commands"], "params": d["params"],
                    "deprecated": 0, "usable": _as_int(usable),
                }
                for lang in NG_COLSGROUP_LANGS:
                    row[f"Content_{lang}"] = (labels or {}).get(lang) or d.get(f"Content_{lang}")
                mode = f"cloned from Dict entry of '{dict_name}'"
            else:
                # Case B: fresh entry under parent_menu_path / parent_g
                if not parent_menu_path and not parent_g:
                    return (f"Error: no Dict menu entry found for '{dict_name}' — pass parent_menu_path "
                            f"(menuPath of the parent node) or parent_g, plus labels for a fresh entry.")
                parent, perr = _fetch_parent(cur)
                if perr:
                    return perr
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
                    "childMenuPath": (menu_path.rsplit("/", 1)[-1] if menu_path else slug) or slug,
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
