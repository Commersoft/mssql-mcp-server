"""ai_tool_sync_params / ai_tool_register — narzędzia AI agentów."""

from __future__ import annotations

import json
import re

from typing import List, Optional, Sequence
from pyodbc import connect

from ._core import _as_int, _exec_scalar, _jsonsave, _new_guid


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
