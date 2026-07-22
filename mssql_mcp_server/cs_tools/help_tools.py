"""help_upsert_topic — tematy pomocy okien (csHelpContents)."""

from __future__ import annotations

import json
import re
import uuid

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import DEFAULT_NAMESPACE_G, NG_COLSGROUP_LANGS, _exec_scalar, _jsonsave, _new_guid, _stable_guid


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
    changelog_append=None,
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
    changelog_append: '<li>...</li>' HTML (string = PL, or {PL, EN, ...}) appended to
    the FINAL </ul> of the existing content per language (the changelog list must be
    the last element — the module-help convention). Idempotent (an identical <li>
    already present = skip). Mutually exclusive with `content`. Works on any
    environment via `server` — updating DEV and PROD is two calls with the same args.
    """
    def _langs(val, allowed):
        if val is None:
            return {}
        if isinstance(val, str):
            # Some MCP clients serialize dict params as JSON STRINGS — without this
            # the raw '{"PL": ...}' text would land verbatim in Subject_PL/Content_PL.
            stripped = val.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        return {k: v for k, v in parsed.items() if k in allowed and v}
                except (ValueError, TypeError):
                    pass
            return {"PL": val}
        return {k: v for k, v in val.items() if k in allowed and v}

    subj = _langs(subject, NG_COLSGROUP_LANGS)
    cont = _langs(content, NG_COLSGROUP_LANGS)
    desc = _langs(description, NG_COLSGROUP_LANGS)
    keyw = _langs(keywords, NG_COLSGROUP_LANGS)
    chlog = _langs(changelog_append, NG_COLSGROUP_LANGS)
    if chlog and cont:
        return "Error: changelog_append and content are mutually exclusive."
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

            # changelog append -> becomes a regular content update (D+I with the same G)
            if chlog:
                if not row_g:
                    return ("Error: changelog_append requires an EXISTING topic "
                            "(help_contents_g or Subject_PL match).")
                for lang, li in chlog.items():
                    cur.execute(
                        f"select Content_{lang} from dbo.csHelpContents with(nolock) "
                        "where csHelpContentsG=?", row_g)
                    r2 = cur.fetchone()
                    current = r2[0] if r2 else None
                    if not current:
                        out.append(f"  CHANGELOG {lang}: Content_{lang} is empty — skipped.")
                        continue
                    if li in current:
                        out.append(f"  CHANGELOG {lang}: identical <li> already present — skipped (idempotent).")
                        continue
                    trimmed = current.rstrip()
                    if not trimmed.endswith("</ul>"):
                        out.append(f"  CHANGELOG {lang}: content does not end with </ul> — skipped "
                                   "(the changelog list must be the LAST element of the topic).")
                        continue
                    cont[lang] = trimmed[: -len("</ul>")] + li + "</ul>"
                if not cont:
                    return "\n".join(out) if out else "CHANGELOG: nothing to append."

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
                # HARD RULE 24: link G MUSI być identyczne na DEV i PROD (pakiety replikują
                # po G; losowy newid() per środowisko = PK violation przy upgrade — incydent
                # csHelpContentsNGAppWindowsReplicateRow 2026-07-22). Deterministyczne md5
                # z klucza naturalnego → oba środowiska liczą to samo G.
                link_g = _stable_guid(
                    cur, f"helplink:{str(row_g).upper()}:{str(namespace_g).upper()}:{w}")
                resp = _jsonsave(cur, "csHelpContentsNGAppWindowsJSONSave", [{
                    "_opr": "I", "csHelpContentsNGAppWindowsG": link_g,
                    "csHelpContentsG": row_g, "csAppNameSpacesG": namespace_g,
                    "appWindowIdent": w,
                }])
                if resp:
                    return f"csHelpContentsNGAppWindowsJSONSave ERROR ({w}):\n{resp}"
                out.append(f"  LINK added: {w}")
    return "\n".join(out)
