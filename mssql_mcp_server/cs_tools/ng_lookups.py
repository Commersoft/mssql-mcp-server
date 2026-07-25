"""Lookupy NG: ng_add_lookup / ng_create_lookup_window / ng_add_linked_window / ng_add_filter."""

from __future__ import annotations

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import (
    DEFAULT_NAMESPACE_G,
    NG_COLSGROUP_LANGS,
    NG_LABEL_LANGS,
    _as_int,
    _exec_scalar,
    _jsonsave,
    _new_guid,
    _xml_response_to_text,
)
from .ng_window import ng_bulk_layout


# ---------------------------------------------------------------------------
# 15. ng_add_lookup
# ---------------------------------------------------------------------------

_SET_CANDIDATE_SUFFIXES = ("Id", "G", "Ident", "Desc", "Code")
_SET_CANDIDATE_SKIP = {"csappnamespacesg"}


def _propose_set_candidates(cur, namespace_g, app_window_ident, data_set_ident,
                            field_ident, lookup_window_ident, lookup_ds,
                            source_kind, explicit_sets):
    """Match lookup-window fields to host fields that lack a Set mapping.
    Conventions (evidence: csDocsHeaders_Agreements 2026-07-25 — Id+Desc Sets existed,
    the symbol/VATCode ones were silently missing, so lookup picks did not refresh them):
      - exact case-insensitive ident match for *Id/*G/*Ident/*Desc/*Code lookup fields;
      - symbol field == host prefix: lookup 'paymentType' -> host 'PaymentType'
        (host field 'PaymentTypeDesc' -> prefix 'PaymentType', match is CI);
      - host-prefix concat: host 'CustomerDesc' -> prefix 'Customer', lookup 'VATCode'
        -> host 'CustomerVATCode'.
    Returns [{'from_field':..,'to_field':..}] not covered by DB rows nor explicit_sets."""
    host_table = ("csNGAppWindowDataSetsFields" if source_kind == "rows"
                  else "csNGAppWindowDataSetsWhereFields")
    cur.execute(
        f"select dataFieldIdent from dbo.{host_table} with(nolock) "
        "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ?",
        namespace_g, app_window_ident, data_set_ident,
    )
    host_by_lower = {r[0].lower(): r[0] for r in cur.fetchall()}
    cur.execute(
        "select distinct dataFieldIdent from dbo.csNGAppWindowDataSetsFields with(nolock) "
        "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and addToSelect = 1",
        namespace_g, lookup_window_ident, lookup_ds,
    )
    lookup_fields = [r[0] for r in cur.fetchall()]
    cur.execute(
        "select dataFieldIdentFrom, dataFieldIdentTo "
        "from dbo.csNGAppWindowDataSetsLookupDefsSet with(nolock) "
        "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
        namespace_g, app_window_ident, data_set_ident, field_ident,
    )
    covered = {((t or f) or "").lower() for f, t in cur.fetchall()}
    covered |= {((s.get("to_field") or s.get("from_field")) or "").lower() for s in explicit_sets}
    prefix = field_ident[:-4] if field_ident.lower().endswith("desc") else field_ident

    out = []
    for lf in lookup_fields:
        lfl = lf.lower()
        if lfl in _SET_CANDIDATE_SKIP:
            continue
        target = None
        if lfl in host_by_lower and (lf.endswith(_SET_CANDIDATE_SUFFIXES) or lfl == prefix.lower()):
            target = host_by_lower[lfl]
        elif (prefix + lf).lower() in host_by_lower:
            target = host_by_lower[(prefix + lf).lower()]
        if target is None or target.lower() in covered:
            continue
        out.append({"from_field": lf, "to_field": target})
    return out


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
    auto_sets: bool = False,
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
      - SET candidates: lookup fields are matched to host fields by convention
        (*Id/*G/*Ident/*Desc/*Code exact CI match, symbol==host-prefix like
        paymentType -> PaymentType, host-prefix+field like VATCode -> CustomerVATCode);
        unmapped matches are REPORTED as a warning, auto_sets=True wires them too
        (a missing Set = lookup pick silently does not refresh that host field);
      - idempotent: existing identical mappings are skipped;
      - warns when the lookup window is missing onlyAsLookup=1 or has no sort idents.

    gets: [{"from_field": .., "to_field": .., "value": .., "source_kind_from": ..,
            "source_kind_to": ..}]  (from_field XOR value)
    sets: [{"from_field": .., "to_field": .., "data_set_ident_from": ..,
            "source_kind_to": ..}]
    """
    if source_kind not in ("rows", "where"):
        return "Error: source_kind must be 'rows' (form field) or 'where' (filter panel)."
    if not sets and not search_get and not gets and not auto_sets:
        return "Error: nothing to wire — provide sets/gets/auto_sets or leave search_get=True."

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

            # --- SET candidates (todo pkt 2): matches computed BEFORE wiring, against
            #     DB rows + explicitly passed sets; auto_sets=True appends them to the
            #     normal SET loop below, otherwise they are reported as a warning ---
            candidates = _propose_set_candidates(
                cur, namespace_g, app_window_ident, data_set_ident, field_ident,
                lookup_window_ident, lookup_ds, source_kind, sets or [],
            )
            if candidates and auto_sets:
                sets = list(sets or []) + candidates
                log.append("AUTO-SETS (matched by convention): "
                           + ", ".join(f"{c['from_field']} -> {c['to_field']}" for c in candidates))
            elif candidates:
                warnings.append(
                    "SET candidates NOT wired — matching lookup->host fields without a Set "
                    "(lookup pick will not refresh them); re-run with auto_sets=true or pass sets=["
                    + ", ".join("{'from_field': '%s', 'to_field': '%s'}"
                                % (c["from_field"], c["to_field"]) for c in candidates)
                    + "].")

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

            # DB state, not call args — a re-run on an already-wired lookup is fine
            total_sets = _exec_scalar(
                cur,
                "select count(*) from dbo.csNGAppWindowDataSetsLookupDefsSet with(nolock) "
                "where csAppNameSpacesG = ? and appWindowIdent = ? and dataSetIdent = ? and dataFieldIdent = ?",
                namespace_g, app_window_ident, data_set_ident, field_ident,
            )

    msg = (f"OK: lookup {app_window_ident}/{data_set_ident}/{field_ident} -> {lookup_window_ident} "
           f"(+{n_get} get, +{n_set} set).\n  " + "\n  ".join(log))
    if not total_sets:
        warnings.append("no Set mappings — the lookup will open but select nothing back; "
                        "add sets like [{'from_field': 'csXId'}, {'from_field': 'XDesc'}].")
    if warnings:
        msg += "\nWARNINGS:\n  - " + "\n  - ".join(warnings)
    return msg


# ---------------------------------------------------------------------------
# 21. ng_add_linked_window — master->detail link (bottom/side panel) in one call
# ---------------------------------------------------------------------------

def ng_add_linked_window(connection_string: str, app_window_ident_from: str,
                         app_window_ident_to: str, placement: str,
                         map_fields: Sequence[dict], ord: int = 1,
                         labels: Optional[dict] = None, tab_default: Optional[str] = None,
                         wire_one_item_only: bool = True,
                         namespace_g: str = DEFAULT_NAMESPACE_G) -> str:
    """Link a detail window to a master (csNGAppWindowsLinks + LinksFields). map_fields:
    [{from, to?}] (from = master main field, to = detail where-field; default to=from).
    placement: 'bottom-panel'|'outer-side-panel'|'side-panel'|'inner-side-panel'
    (STANDARD dla detali = outer-side-panel).
    Optional tab_default sets the master where-field 'tabIdent-<placement>' (needed when a
    placement holds several tabs). labels: {'PL':..,'EN':..} for the tab caption. The
    linkedWindows cache rebuilds automatically via csNGAppWindowsLinksJSONSave.
    wire_one_item_only (default True) dopina kontrakt oneItemOnly automatycznie:
    where-field 'oneItemOnly' na detalu (tworzy gdy brak), stala LinksFields
    oneItemOnly=1 (sourceKindFrom='value'), linkedParamName + initNewRow=1 na
    mapowanych where-fieldach FK (bez linkedParamName panel NIGDY nie wysyla getData —
    'invalid field link ... no query param'). Ochrone w stmSQL detalu (oneItemOnly=1 +
    FK null => 0 wierszy) MUSISZ dodac sam — tool tylko ostrzega, gdy jej nie widzi."""
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

            # --- kontrakt oneItemOnly (patrz oneItemOnly-stmSQL-pattern.instructions.md) ---
            if wire_one_item_only:
                # 1) linkedParamName + initNewRow=1 na mapowanych where-fieldach FK detalu
                #    (bez linkedParamName frontend: 'destination field has no query param'
                #     i panel nigdy nie wysyla getData; initNewRow=1 = nowy wiersz w detalu
                #     dziedziczy FK mastera)
                for m in map_fields:
                    if (m.get("source_kind_from") or "rows") != "rows":
                        continue
                    ft = m.get("to") or m.get("from")
                    ds_to = m.get("data_set_ident_to") or "main"
                    cur.execute(
                        "select csNGAppWindowDataSetsWhereFieldsId, csNGAppWindowDataSetsWhereFieldsG, "
                        "  linkedParamName, initNewRow "
                        "from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                        "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                        namespace_g, app_window_ident_to, ds_to, ft,
                    )
                    wf = cur.fetchone()
                    if not wf:
                        log.append(f"  WARNING: detail where-field '{ft}' NOT FOUND — create it "
                                   f"(ng_add_filter) and re-run, panel will not filter without it.")
                        continue
                    if (wf[2] or "") != ft or not wf[3]:
                        resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [{
                            "_opr": "U",
                            "csNGAppWindowDataSetsWhereFieldsId": int(wf[0]),
                            "csNGAppWindowDataSetsWhereFieldsG": str(wf[1]).upper(),
                            "linkedParamName": ft,
                            "initNewRow": 1,
                        }])
                        if resp:
                            return f"whereField linkedParamName WARNING ({ft}):\n{resp}"
                        log.append(f"  FK where-field {ft}: linkedParamName='{ft}', initNewRow=1 (U)")

                # 2) where-field oneItemOnly na detalu (tworzy gdy brak)
                exo = cur.execute(
                    "select csNGAppWindowDataSetsWhereFieldsId, csNGAppWindowDataSetsWhereFieldsG, linkedParamName "
                    "from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=N'main' and dataFieldIdent=N'oneItemOnly'",
                    namespace_g, app_window_ident_to,
                ).fetchone()
                if not exo:
                    resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [{
                        "_opr": "I",
                        "csNGAppWindowDataSetsWhereFieldsG": _new_guid(),
                        "csAppNameSpacesG": namespace_g,
                        "appWindowIdent": app_window_ident_to,
                        "dataSetIdent": "main",
                        "dataFieldIdent": "oneItemOnly",
                        "formatType": "integer",
                        "SQLBaseType": "tinyint",
                        "dataFieldValueDef": "0",
                        "linkedParamName": "oneItemOnly",
                        "initNewRow": 0,
                    }])
                    if resp:
                        return f"oneItemOnly whereField WARNING:\n{resp}"
                    log.append("  where-field oneItemOnly (I)")
                elif (exo[2] or "") != "oneItemOnly":
                    resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [{
                        "_opr": "U",
                        "csNGAppWindowDataSetsWhereFieldsId": int(exo[0]),
                        "csNGAppWindowDataSetsWhereFieldsG": str(exo[1]).upper(),
                        "linkedParamName": "oneItemOnly",
                    }])
                    if resp:
                        return f"oneItemOnly linkedParamName WARNING:\n{resp}"
                    log.append("  where-field oneItemOnly: linkedParamName fixed (U)")

                # 3) stala LinksFields oneItemOnly=1
                exc = _exec_scalar(
                    cur,
                    "select count(*) from dbo.csNGAppWindowsLinksFields with(nolock) "
                    "where csAppNameSpacesGFrom=? and appWindowIdentFrom=? and csAppNameSpacesGTo=? "
                    "and appWindowIdentTo=? and dataSetIdentTo=N'main' and dataFieldIdentTo=N'oneItemOnly'",
                    namespace_g, app_window_ident_from, namespace_g, app_window_ident_to,
                )
                if not exc:
                    resp = _jsonsave(cur, "csNGAppWindowsLinksFieldsJSONSave", [{
                        "_opr": "I",
                        "csNGAppWindowsLinksFieldsG": _new_guid(),
                        "csAppNameSpacesGFrom": namespace_g,
                        "appWindowIdentFrom": app_window_ident_from,
                        "csAppNameSpacesGTo": namespace_g,
                        "appWindowIdentTo": app_window_ident_to,
                        "sourceKindFrom": "value",
                        "dataFieldValueFrom": "1",
                        "dataSetIdentTo": "main",
                        "dataFieldIdentTo": "oneItemOnly",
                    }])
                    if resp:
                        return f"oneItemOnly LinksFields WARNING:\n{resp}"
                    log.append("  LINK CONST oneItemOnly=1 (I)")

                # 4) ochrona w stmSQL — tylko ostrzezenie, nie auto-fix
                stm_reads = _exec_scalar(
                    cur,
                    "select count(*) from dbo.csNGAppWindowDataSets with(nolock) "
                    "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=N'main' "
                    "and cast(stmSQL as nvarchar(max)) like N'%oneItemOnly%'",
                    namespace_g, app_window_ident_to,
                )
                if not stm_reads:
                    log.append("  WARNING: detail stmSQL does NOT read oneItemOnly — add the guard "
                               "(oneItemOnly=1 + master FK null => '= null' => 0 rows), see "
                               "oneItemOnly-stmSQL-pattern.instructions.md.")

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
        # LookupDefsSet validation rejects the whole batch when the target field
        # (dataFieldIdentTo) does not exist yet — auto-create missing hidden host
        # where-fields for every set target BEFORE wiring the lookup.
        if lookup_sets:
            with connect(connection_string, autocommit=True) as conn:
                with conn.cursor() as cur:
                    for st in lookup_sets:
                        target = (st.get("to_field") or st.get("from_field") or "").strip()
                        if not target or target == field_ident:
                            continue
                        if (st.get("source_kind_to") or "where") != "where":
                            continue
                        ex2 = _exec_scalar(
                            cur,
                            "select 1 from dbo.csNGAppWindowDataSetsWhereFields with(nolock) "
                            "where csAppNameSpacesG=? and appWindowIdent=? and dataSetIdent=? and dataFieldIdent=?",
                            namespace_g, app_window_ident, data_set_ident, target,
                        )
                        if ex2:
                            continue
                        is_id = target.lower().endswith("id")
                        resp = _jsonsave(cur, "csNGAppWindowDataSetsWhereFieldsJSONSave", [{
                            "_opr": "I",
                            "csNGAppWindowDataSetsWhereFieldsG": _new_guid(),
                            "csAppNameSpacesG": namespace_g,
                            "appWindowIdent": app_window_ident,
                            "dataSetIdent": data_set_ident,
                            "dataFieldIdent": target,
                            "formatType": "integer" if is_id else "string",
                            "SQLBaseType": "bigint" if is_id else "nvarchar",
                            **({} if is_id else {"SQLColumnParams": "(max)"}),
                            "isActive": 1,
                        }])
                        if resp:
                            return f"auto host where-field '{target}' JSONSave WARNING:\n{resp}"
                        log.append(f"  AUTO where-field host '{target}' "
                                   f"({'integer/bigint' if is_id else 'string/nvarchar(max)'}) — Set target")
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
