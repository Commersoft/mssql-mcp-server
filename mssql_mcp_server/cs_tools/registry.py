"""Deskryptory MCP (tool_descriptors), zbiór nazw (CS_TOOL_NAMES) i dispatch (handle_tool)."""

from __future__ import annotations

from ._core import DEFAULT_NAMESPACE_G
from .deploy import cs_jsonsave, deploy_sql_object, get_cs_object_versions
from .schema import add_cs_column, register_cs_table, register_job
from .ng_window import (
    add_ng_field,
    ng_bulk_layout,
    ng_get_window_config,
    ng_register_translates,
    ng_set_dataset_props,
    ng_set_field_labels,
    ng_set_layout_col,
    ng_set_sort,
    ng_set_stmsql,
    ng_upsert_cols_group,
    ng_upsert_tabs_group,
    update_view_html,
)
from .ng_lookups import ng_add_filter, ng_add_linked_window, ng_add_lookup, ng_create_lookup_window
from .ng_actions import ng_add_action, ng_add_menu_entry, ng_ensure_privileges, rebuild_user_rights
from .discovery import describe, ng_diff_with_dict, ng_preview_dataset, sql_grep
from .replicate import ng_replicate_window
from .help_tools import help_upsert_topic
from .ai_tools import ai_tool_register, ai_tool_sync_params
from .repl_queue import repl_apply_pending


CS_TOOL_NAMES = {
    "deploy_sql_object",
    "cs_jsonsave",
    "add_cs_column",
    "register_cs_table",
    "register_job",
    "add_ng_field",
    "get_cs_object_versions",
    "update_view_html",
    "ng_get_window_config",
    "ng_set_field_labels",
    "ng_set_layout_col",
    "ng_upsert_cols_group",
    "ng_upsert_tabs_group",
    "ng_set_stmsql",
    "ng_set_dataset_props",
    "rebuild_user_rights",
    "ai_tool_sync_params",
    "ng_add_lookup",
    "describe",
    "sql_grep",
    "ng_preview_dataset",
    "ng_bulk_layout",
    "ng_register_translates",
    "ng_add_linked_window",
    "ng_add_filter",
    "ng_add_action",
    "ng_ensure_privileges",
    "ng_add_menu_entry",
    "ng_set_sort",
    "ng_diff_with_dict",
    "help_upsert_topic",
    "ai_tool_register",
    "ng_create_lookup_window",
    "ng_replicate_window",
    "repl_apply_pending",
}


def tool_descriptors():
    from mcp.types import Tool

    return [
        Tool(
            name="describe",
            description=(
                "Compact schema of a DB object: for a table/view -> columns "
                "(name | type(len) NULL/NOT NULL [PK][identity][-> ref]); for a "
                "procedure/function -> parameter list. Use INSTEAD of guessing "
                "column names (avoids repeated 'Invalid column name'). Works on any "
                "environment via `server` (default DEV). "
                "REQUIRED param: object_name (snake_case — NOT 'object'/'name'/'query')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Object name (dbo. prefix optional), e.g. 'csNGAppWindowDataSetsActionsFields'."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "SLGRODNO", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": ["object_name"],
            },
        ),
        Tool(
            name="sql_grep",
            description=(
                "Case-insensitive substring search over SQL object bodies "
                "(sys.sql_modules). Returns object:line:content hits, like Grep over "
                "files. Optional name_like narrows candidate objects. Use to locate "
                "where a column/string is built instead of ad-hoc LIKE + substring. "
                "REQUIRED param: pattern (NOT 'query'/'phrase')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Literal substring to find (case-insensitive)."},
                    "name_like": {"type": "string", "description": "Optional: only objects whose name contains this."},
                    "top": {"type": "integer", "description": "Max hits (default 100)."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "SLGRODNO", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="ng_preview_dataset",
            description=(
                "Dry-run an NG dataset the way the runtime does: expand /*FIELDS*/ + "
                "stmSQL template into the real data SELECT and EXECUTE it (top N, for "
                "json). Catches reserved-word / invalid-column / missing-@var errors "
                "that csNGValidateWindowForAI (config-only) misses. Returns generated "
                "SQL + first rows, or the SQL + exact runtime error. Works on any "
                "environment via `server` (read-only dry-run, e.g. server=PROD to "
                "verify a replicated window on real data)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "where": {"type": "string", "description": "Optional @where JSON (default null)."},
                    "top": {"type": "integer", "description": "Row cap 1..50 (default 5)."},
                    "cs_companies_id": {"type": "integer", "description": "Company context (default: min company)."},
                    "cs_usr_id": {"type": "integer", "description": "User context (default: min user)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_bulk_layout",
            description=(
                "Bulk upsert grid layout columns in one call. columns: "
                "[{field, visible?, ord?, width?, group?}]. Same pitfalls as "
                "ng_set_layout_col (minimal-U, int isVisible, non-null width on INSERT, "
                "group existence check). Ideal for hiding technical cols + reordering a "
                "whole grid after csCreateNGWindowFromTableForAI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{field, visible?, ord?, width?, group?}] — group='' detaches.",
                    },
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "columns"],
            },
        ),
        Tool(
            name="ng_register_translates",
            description=(
                "Register gT() idents on a window (csNGAppWindowTranslates). Each item: "
                "{ident, cs_translate_g?} to reuse, OR {ident, PL, EN, ...} to "
                "reuse-by-content (match Content_PL+Content_EN) or create a new "
                "csTranslate. Idempotent on (appWindowIdent, translateIdent)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "translates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{ident, cs_translate_g?} | {ident, PL, EN, DE, ...}]",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "translates"],
            },
        ),
        Tool(
            name="ng_add_linked_window",
            description=(
                "Link a detail window to a master (csNGAppWindowsLinks + LinksFields) in "
                "one call. map_fields: [{from, to?}] (master main field -> detail where-field). "
                "placement: bottom-panel|outer-side-panel|side-panel|inner-side-panel — STANDARD "
                "for details = outer-side-panel. Optional tab_default sets master where-field "
                "'tabIdent-<placement>' (multi-tab placements). labels {PL,EN,..} = tab caption. "
                "linkedWindows cache rebuilds automatically. Wires the oneItemOnly contract by "
                "default (wire_one_item_only): detail where-field 'oneItemOnly' (created if "
                "missing), LinksFields constant oneItemOnly=1, linkedParamName + initNewRow=1 on "
                "mapped FK where-fields (without linkedParamName the panel NEVER sends getData). "
                "The stmSQL guard (oneItemOnly=1 + FK null => 0 rows) is YOURS to add — the tool "
                "only warns when missing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident_from": {"type": "string"},
                    "app_window_ident_to": {"type": "string"},
                    "placement": {"type": "string"},
                    "map_fields": {"type": "array", "items": {"type": "object"}, "description": "[{from, to?}]"},
                    "ord": {"type": "integer", "description": "Tab order (default 1)."},
                    "labels": {"type": "object", "description": "{PL,EN,DE,..} tab caption."},
                    "tab_default": {"type": "string", "description": "appWindowIdentTo of the default tab (for multi-tab placement)."},
                    "wire_one_item_only": {"type": "boolean", "description": "Default true: auto-wire the oneItemOnly contract (where-field, link constant, linkedParamName+initNewRow on FK)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident_from", "app_window_ident_to", "placement", "map_fields"],
            },
        ),
        Tool(
            name="ng_add_filter",
            description=(
                "Create a filter-panel where-field (+ optional lookup wiring + watermark) "
                "in one call. Lookup hosts (string) default notUseForGetData=1. Provide "
                "watermark {PL,EN,..} for :showLabel=false fields. lookup_window_ident + "
                "lookup_sets wire ng_add_lookup with source_kind='where'; missing hidden "
                "host where-fields for Set targets (e.g. csWarehousesId list) are "
                "AUTO-CREATED before the Set rows (validation requires target-first)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "format_type": {"type": "string", "description": "string|integer|boolean|date|..."},
                    "sql_base_type": {"type": "string", "description": "nvarchar|bigint|int|bit|date|..."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "sql_column_params": {"type": "string", "description": "e.g. '(max)'."},
                    "value_def": {"type": "string", "description": "dataFieldValueDef (radiogroup/checkbox default)."},
                    "not_use_for_get_data": {"type": "boolean", "description": "Default: true for string lookup hosts."},
                    "ord": {"type": "integer"},
                    "label_pl": {"type": "string"},
                    "label_en": {"type": "string"},
                    "watermark": {"type": "object", "description": "{PL,EN,..} placeholder for :showLabel=false."},
                    "lookup_window_ident": {"type": "string", "description": "Optional: wire a filter lookup."},
                    "lookup_sets": {"type": "array", "items": {"type": "object"}, "description": "[{from_field, to_field?, source_kind_to?}]"},
                    "lookup_gets": {"type": "array", "items": {"type": "object"}, "description": "[{value|from_field, to_field}]"},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "format_type", "sql_base_type"],
            },
        ),
        Tool(
            name="deploy_sql_object",
            description=(
                "Deploy a procedure/function/view/trigger through the cs* versioning "
                "framework (csAddObjVer). Handles all pitfalls automatically: objectName "
                "WITHOUT dbo. prefix, @pv=latest version, fresh unique @v, 3-batch split "
                "(no GO), and orphan inProgress cleanup. `body` is the CREATE statement only. "
                "server=PROD deploys the SAME @v as the DEV latest version (consistent chain; "
                "deploy on DEV first; idempotent when the version is already on the target). "
                "NEVER patch managed objects with raw ALTER — registry divergence ships bugs "
                "in upgrade packages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "description": "Object name WITHOUT schema, e.g. 'csFooBar'."},
                    "body": {"type": "string", "description": "The CREATE statement only (CREATE procedure dbo.<Name> ... )."},
                    "description": {"type": "string", "description": "Version description (>=3 chars)."},
                    "object_type": {"type": "string", "description": "procedure|function|view|trigger (informational)."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV). Non-DEV reuses the DEV latest @v."},
                },
                "required": ["object_name", "body", "description"],
            },
        ),
        Tool(
            name="register_cs_table",
            description=(
                "Register a cs* table END-TO-END: csSysTables + csSysColumns (full "
                "12-language ColumnDesc enforced) + csSysIndexes + csSysTablesRebuild "
                "(both params) + physical/JSONSave verification. Idempotent (skips existing, "
                "adds missing). is_managed=true auto-adds <T>Id/<T>G columns and default "
                "pk/uq indexes; non-managed tables require an explicit clustered PK. "
                "Deterministic GUIDs (md5 'table:<T>'/'col:<T>:<c>'/'idx:<T>:<name>')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "columns": {
                        "type": "array", "items": {"type": "object"},
                        "description": ("[{name, type ('bigint'|'nvarchar'|'numeric'|...), params? ('(18,6)'), "
                                        "nullable? (default true), default? ('((0))'), "
                                        "desc: {EN,PL,DE,FR,ES,IT,NL,PT,RU,UK,SK,SE — all 12, real translations}}]"),
                    },
                    "indexes": {
                        "type": "array", "items": {"type": "object"},
                        "description": ("[{name?, keys: 'colA,colB' | ['colA','colB'] | '[colA] asc,...', "
                                        "pk?, uq?, clustered?}]. Default for managed: pk(Id)+uq(G)."),
                    },
                    "description_pl": {"type": "string", "description": "csSysTables.Description_PL."},
                    "description_en": {"type": "string", "description": "csSysTables.Description_EN."},
                    "is_managed": {"type": "boolean", "description": "IsManagedTable (JSONSave generated). Default false (aggregate/work table — direct DML allowed)."},
                    "track_changes": {"type": "boolean", "description": "TrackChanges audit. Default false."},
                },
                "required": ["table_name", "columns"],
            },
        ),
        Tool(
            name="register_job",
            description=(
                "Register a job in csCompaniesJobs (idempotent by company+ProcedureName). "
                "Conventions handled: dbo. prefix, stable G, JobOrder=max+100 when omitted, "
                "wrapper-proc existence check. interval_seconds: 86400=daily, 604800=weekly "
                "(pick the right weekday in start_time!). Works cross-server via `server`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "procedure_name": {"type": "string", "description": "Wrapper proc, e.g. 'csFooFillJob' (dbo. optional)."},
                    "cs_companies_id": {"type": "integer"},
                    "start_time": {"type": "string", "description": "'YYYY-MM-DDTHH:MM:SS' — first run; weekday matters for weekly jobs."},
                    "interval_seconds": {"type": "integer", "description": "86400=daily, 604800=weekly."},
                    "job_desc_pl": {"type": "string"},
                    "job_desc_en": {"type": "string"},
                    "thread_no": {"type": "integer", "description": "Default 8100. UWAGA: wątek bez workera = job NIGDY nie ruszy; wątki są SZEREGOWE — długie joby (LLM) trzymaj osobno od jobów o sztywnej porze. Sprawdź żywotność: select ThreadNo, max(LastInvokeTime) from csCompaniesJobs group by ThreadNo (DEV 2026-07-22: 200000 szybki pipeline, 8100 ciężkie AI, 100000 nocne, 88 minutowe). Tool ostrzega gdy wątek wygląda martwo."},
                    "job_order": {"type": "integer", "description": "Default max+100 for the company."},
                    "active": {"type": "boolean", "description": "Default true."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": ["procedure_name", "cs_companies_id", "start_time", "interval_seconds", "job_desc_pl"],
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
                "sets labelDataFieldIdent and a non-null width to avoid silent collapse. "
                "Translated fields (*Desc over *Desc_PL/EN columns): pass is_translate=true — "
                "sets isTranslate=1 + dataFieldIdentBeforeTranslate with the mandatory trailing '_'."
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
                    "is_translate": {"type": "boolean", "description": "Translated field (*Desc_PL/EN... columns): isTranslate=1 + dataFieldIdentBeforeTranslate. Default false."},
                    "before_translate": {"type": "string", "description": "Column prefix for translated field (default '<field_ident>_'; trailing '_' enforced). Only with is_translate."},
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
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
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
        Tool(
            name="ng_get_window_config",
            description=(
                "READ-ONLY compact dump of a full NG window configuration: window props, "
                "datasets, fields, layout cols, cols groups, actions, where fields, sort "
                "idents, key fields, lookup defs, links. Use INSTEAD of ad-hoc SELECTs. "
                "include_stmsql=true returns full stmSQL text (also useful as a snapshot "
                "before ng_set_stmsql). "
                "REQUIRED param: app_window_ident (snake_case — NOT 'appWindowIdent')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Optional filter (default: all datasets)."},
                    "include_stmsql": {"type": "boolean", "description": "Include full stmSQL text (default false)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_set_field_labels",
            description=(
                "Update NG field (or whereField) labels: lab (edit label), col (grid header), "
                "watermark — per language. Handles the formatType pitfall (re-sends current "
                "formatType, otherwise JSONSave silently resets date/decimal/bool to 'string'). "
                "Writes ONLY provided languages — never copies PL to others (HARD RULE)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "labels": {
                        "type": "object",
                        "description": "{'PL': {'lab': '...', 'col': '...', 'watermark': '...'}, 'EN': {...}} — each key optional; langs: PL,EN,DE,FR,ES,NL,PT,RU,UK,IT.",
                    },
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "target": {"type": "string", "description": "'field' (default) or 'whereField' (filter panel)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "labels"],
            },
        ),
        Tool(
            name="ng_set_layout_col",
            description=(
                "Upsert a grid layout column: width, isVisible, ord, attach/detach column "
                "group. UPDATE uses minimal-U (avoids label re-validation trap for joined "
                "fields); INSERT auto-fills labelDataSetIdent/labelDataFieldIdent + non-null "
                "width (NULL width silently collapses the column). isVisible as int 1/0. "
                "Verifies the cols group exists. Grouped columns must be adjacent by ord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "width": {"type": "number", "description": "Multiple of 60 (120 code / 220 desc)."},
                    "is_visible": {"type": "boolean"},
                    "ord": {"type": "integer"},
                    "cols_group_ident": {"type": "string", "description": "Attach to this column group."},
                    "clear_cols_group": {"type": "boolean", "description": "Detach from its column group."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident"],
            },
        ),
        Tool(
            name="ng_upsert_cols_group",
            description=(
                "Upsert a grid column group (csNGAppWindowColsGroups; per-window, column "
                "names dataFieldColsGroupDesc_XX). Only provided languages are written "
                "(never copies PL). Then attach columns via ng_set_layout_col — grouped "
                "columns must be adjacent by ord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "cols_group_ident": {"type": "string"},
                    "descriptions": {
                        "type": "object",
                        "description": "{'PL': 'Obowiązuje', 'EN': 'Valid', ...} — langs: PL,EN,DE,FR,ES,NL,PT,RU,UK,IT,SE,SK.",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "cols_group_ident", "descriptions"],
            },
        ),
        Tool(
            name="ng_upsert_tabs_group",
            description=(
                "Upsert a tabs group of linked windows (csNGAppWindowTabsGroups — per MASTER window "
                "hosting the tab bar; analog of cols groups): tabGroupDesc_XX per language (only provided "
                "langs written, never copies PL; PL required on create), ord (explicit group order on the "
                "vertical tab bar; NULL = position of the group's first tab), translate_ident (optional gT "
                "fallback). Stable G = md5('tabsGroup:<window>:<ident>') so DEV/PROD match. link_to_windows "
                "attaches the group to existing csNGAppWindowsLinks rows (appWindowIdentTo list) AFTER the "
                "group row exists (FK) — the JSONSave custom code refreshes the master's linkedWindows cache. "
                "Groups render only in vertical tab layouts (outer-side-panel-tab-layout='left')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string", "description": "MASTER window (appWindowIdentFrom of the links)."},
                    "tab_group_ident": {"type": "string", "description": "e.g. 'TAB_GROUP_OFFER'."},
                    "descriptions": {
                        "type": "object",
                        "description": "{'PL': 'Oferta', 'EN': 'Offer', ...} — langs: PL,EN,DE,FR,ES,NL,PT,RU,UK,IT,SE,SK. PL required on create.",
                    },
                    "ord": {"type": "integer", "description": "Explicit group order on the tab bar."},
                    "translate_ident": {"type": "string", "description": "Optional gT ident fallback (tabGroupTranslateIdent); '' clears."},
                    "link_to_windows": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "appWindowIdentTo of existing links to attach to this group (sets csNGAppWindowsLinks.tabGroupIdent).",
                    },
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "tab_group_ident"],
            },
        ),
        Tool(
            name="ng_set_stmsql",
            description=(
                "Replace a dataset stmSQL with a MANDATORY sp_executesql test BEFORE saving: "
                "template runs twice (with dates — apostrophe bugs only show with non-null "
                "dates — and with empty where). Saved only if both pass. Harness provides "
                "@where, @whereLists='{}', @isRefreshOneRecord, @csCompaniesIdStr, "
                "@LanguageSuffix — templates using @whereLists (warehouse masks) test fine. "
                "Snapshot the old stmSQL first via ng_get_window_config(include_stmsql=true). "
                "test=false only for templates needing OTHER extra params (@csItemsIdStr...) — "
                "verify manually then (e.g. ng_preview_dataset)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "stm_sql": {"type": "string", "description": "Full new stmSQL template text."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "test": {"type": "boolean", "description": "Run sp_executesql test before save (default true)."},
                    "test_where": {"type": "string", "description": "JSON for @where in the test (default has searchText+dates)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "stm_sql"],
            },
        ),
        Tool(
            name="ng_set_dataset_props",
            description=(
                "Update whitelisted csNGAppWindowDataSets columns (pageSize, pagingDisabled, "
                "notUseSort, dataFieldIdent4ReadOnly, loadDataImmediate, calcAggregates...) "
                "via minimal-U JSONSave. stmSQL NOT allowed here — use ng_set_stmsql "
                "(enforces the sp_executesql test). Booleans coerced to int 1/0."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "props": {"type": "object", "description": "{column: value} — whitelisted csNGAppWindowDataSets columns only."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "props"],
            },
        ),
        Tool(
            name="rebuild_user_rights",
            description=(
                "Rebuild per-user cache in csCompaniesUsrs (appMainMenuJSON, "
                "appWindowIdentsWithRights, warehousesRights). MANDATORY after changing menu "
                "items, NG windows or privileges — otherwise missing menu/buttons/spinner. "
                "On DEV narrow to the working company/user; full run needs confirm_all=true "
                "(slow). Direct UPDATE is correct (cache columns = HARD RULE 1 exception b)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cs_companies_id": {"type": "integer", "description": "Narrow to this company (recommended on DEV)."},
                    "cs_usr_id": {"type": "integer", "description": "Narrow to this user."},
                    "confirm_all": {"type": "boolean", "description": "Required for a full rebuild of ALL internal users."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
            },
        ),
        Tool(
            name="ai_tool_sync_params",
            description=(
                "Sync the AI tool parameter registry (csAIAgentsToolsParams): upsert params "
                "(handles the U-batch-rollback gotcha — required fields always re-sent; "
                "typeJSON stored as plain string), heuristic diff registry vs $.keys in the "
                "procedure body, and a csSysGenManagedSync redeploy script for data/."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "csAIAgentsTools.name or SQLProcedure (with/without dbo.)."},
                    "params": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{name, type, description, isRequired, typeJSON}] — upserted by name. Omit to only get the diff report. typeJSON may be an object (auto-dumped to string).",
                    },
                    "generate_sync_script": {"type": "boolean", "description": "Return csSysGenManagedSync script for touched params (default true)."},
                },
                "required": ["tool_name"],
            },
        ),
        Tool(
            name="ng_add_lookup",
            description=(
                "Wire a lookup on an NG field: LookupDefs + Get/Set mappings. Handles all "
                "conventions: csAppNameSpacesGLookup on INSERT, sourceKind on every Get/Set "
                "row (silently ignored otherwise), source_kind='rows' (form) vs 'where' "
                "(filter panel host, auto closeKind=onLostFocus), auto Get host->searchText, "
                "Set dataSetIdentFrom = lookup's first dataset. Proposes SET candidates by "
                "matching lookup fields to host fields (*Id/*G/*Ident/*Desc/*Code exact CI, "
                "symbol==host-prefix like paymentType->PaymentType, host-prefix+field like "
                "VATCode->CustomerVATCode); auto_sets=true wires them (missing Set = lookup "
                "pick silently does not refresh the field). Idempotent. Warns about "
                "missing onlyAsLookup/sort idents on the lookup window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "field_ident": {"type": "string", "description": "Host field (Fields or WhereFields), usually *Desc."},
                    "lookup_window_ident": {"type": "string", "description": "e.g. csCustomersLookup."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "source_kind": {"type": "string", "description": "'rows' (form field, default) or 'where' (filter panel host)."},
                    "is_multi_select": {"type": "boolean"},
                    "close_kind": {"type": "string", "description": "e.g. 'onLostFocus' (auto for source_kind='where')."},
                    "search_get": {"type": "boolean", "description": "Auto-add Get: host field -> searchText (default true)."},
                    "gets": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Extra Get mappings: [{from_field XOR value, to_field, source_kind_from?, source_kind_to?}]. value -> constant filter (e.g. isSupplier=1).",
                    },
                    "sets": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Set mappings (lookup row -> host): [{from_field, to_field? (default =from_field), data_set_ident_from?, source_kind_to?}]. Typically Id + Desc pair.",
                    },
                    "auto_sets": {"type": "boolean", "description": "Also wire the convention-matched SET candidates automatically (default false = report them only)."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "field_ident", "lookup_window_ident"],
            },
        ),
        Tool(
            name="ng_add_action",
            description=(
                "Register/update an NG dataset action with conventions handled: crud='ins'|'upd'|'del' "
                "presets the standard auto-action flags; custom action gets isAuto=0/ord=max+1/"
                "position='default' — TOOLBAR BUTTON requires is_auto=true (c-action-toolbar renders "
                "isAuto=1 only; leave 0 for programmatic actions like onCF/form-invoked). "
                "labels {PL,EN,..} -> actionDesc_*. fields -> ActionsFields. Wires granular "
                "ActionsPrivileges only into EDITING privileges (already granting ins/upd/del); "
                "view-only privileges are skipped with a warning (privilege-escalation guard). "
                "Reminds/performs the rights-cache rebuild (button invisible without it)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "action_ident": {"type": "string"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "labels": {"type": "object", "description": "{PL,EN,..} -> actionDesc_*."},
                    "sql_name": {"type": "string", "description": "SQLName, e.g. 'dbo.csFooJSONSave' or a dispatcher proc."},
                    "kind": {"type": "string"},
                    "crud": {"type": "string", "enum": ["ins", "upd", "del"], "description": "Standard CRUD preset."},
                    "show_view": {"type": "boolean", "description": "1 = action opens a form (.vue)."},
                    "view_html": {"type": "string", "description": "Action form template (implies showView=1)."},
                    "ord": {"type": "integer"},
                    "hide_when_empty": {"type": "boolean"},
                    "show_confirmation": {"type": "boolean"},
                    "add_current_row": {"type": "boolean"},
                    "add_where": {"type": "boolean"},
                    "ref_kind": {"type": "integer"},
                    "close_after_exec": {"type": "boolean"},
                    "is_auto": {"type": "boolean"},
                    "fields": {"type": "array", "items": {"type": "object"},
                               "description": "[{dataFieldIdent, dataFieldValueDef?, dataFieldIdentForNewRowValue?}]"},
                    "extra": {"type": "object", "description": "Extra column overrides (advanced)."},
                    "wire_privileges": {"type": "boolean", "description": "Default true."},
                    "rebuild": {"type": "boolean", "description": "Rebuild rights cache afterwards."},
                    "cs_companies_id": {"type": "integer", "description": "Scope for the rebuild."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "action_ident"],
            },
        ),
        Tool(
            name="ng_ensure_privileges",
            description=(
                "Audit and repair NG window privileges: no csNGAppWindowsPrivileges row = eternal "
                "spinner. Creates a full-rights privilege when missing (create_if_missing), reports/"
                "fixes granular gaps (datasets/actions not covered — fix_gaps), grants to a user "
                "(csCompaniesUsrsPrivileges) or to an APP ROLE (csAppRolesPrivileges — PROD "
                "standard; stable md5 G) and rebuilds the rights cache. Works on any environment "
                "via `server` (e.g. server=PROD for role grants after ng_replicate_window)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "create_if_missing": {"type": "boolean", "description": "Default true."},
                    "privilege_desc_pl": {"type": "string", "description": "Default: window PL desc."},
                    "privilege_desc_en": {"type": "string"},
                    "privilege_group_pl": {"type": "string", "description": "Default 'DSM'."},
                    "fix_gaps": {"type": "boolean", "description": "Insert missing granular dataset/action grants."},
                    "grant_cs_usr_id": {"type": "integer"},
                    "grant_app_roles_id": {"type": "integer", "description": "csAppRolesId — grant do ROLI (csAppRolesPrivileges); wymaga grant_cs_companies_id."},
                    "grant_cs_companies_id": {"type": "integer"},
                    "grant_privilege_g": {"type": "string", "description": "Required when the window has >1 privilege."},
                    "rebuild": {"type": "boolean"},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_add_menu_entry",
            description=(
                "Add an NGDict menu entry OR a grouping Menu NODE (kind='Menu') WITHOUT touching "
                "the Dict entry (project rule: both coexist). Clones the Dict predecessor's entry "
                "when it exists (labels/ContentGuid/parent/Id; menuPath slug via generator formula), "
                "otherwise creates a fresh entry under parent_menu_path OR parent_g (works for nodes "
                "without menuPath) with labels (csTranslate reuse/create — ContentGuid REQUIRED also "
                "for nodes). Idempotent; reminds/performs the menu cache rebuild."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string", "description": "NG window ident; for kind='Menu' only a log ident."},
                    "dict_app_window": {"type": "string", "description": "Dict predecessor name (default = app_window_ident)."},
                    "parent_menu_path": {"type": "string", "description": "menuPath of the parent node (fresh entries), e.g. '/rozrachunki'."},
                    "parent_g": {"type": "string", "description": "csAppMainMenusItemsG of the parent — use for parents WITHOUT menuPath (nodes)."},
                    "labels": {"type": "object", "description": "{PL,EN,..} menu caption (fresh entries/nodes; overrides clone captions)."},
                    "menu_path": {"type": "string", "description": "Explicit menuPath (default: parentPath + slug)."},
                    "ord": {"type": "integer", "description": "Menu Id/order (default: clone source or max+1; nodes: max+100)."},
                    "kind": {"type": "string", "enum": ["NGDict", "Menu"], "description": "'Menu' creates a grouping NODE (no window, no menuPath)."},
                    "icon": {"type": "string", "description": "Optional menu icon ident."},
                    "usable": {"type": "boolean", "description": "Default true."},
                    "rebuild": {"type": "boolean"},
                    "cs_companies_id": {"type": "integer", "description": "Scope for the rebuild."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="ng_set_sort",
            description=(
                "Define/replace a dataset sort in one call: upserts SortIdents (single isDef=1 per "
                "layout — others unset first) and REPLACES the LayoutsColsSortOrder column list. "
                "columns: [{field, desc?}] in order; fields validated; warns on <T>G sort "
                "(business column required)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "object"}, "description": "[{field, desc?}]"},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "layout_ident": {"type": "string", "description": "Default 'default'."},
                    "sort_ident": {"type": "string", "description": "Default 'default'."},
                    "is_def": {"type": "boolean", "description": "Default true."},
                    "labels": {"type": "object", "description": "{PL,EN,..} -> sortDesc_*."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident", "columns"],
            },
        ),
        Tool(
            name="ng_diff_with_dict",
            description=(
                "READ-ONLY migration gap report: NG window vs Dict/table context "
                "(csNGDictWindowContextForAI + AppWindowXML). Reports: suggested-visible fields "
                "missing from the NG layout, FK lookups not wired, default-sort presence "
                "(<T>G warning), where-field/filter count, Dict vs NG datasets. Use after a "
                "Dict->NG migration BEFORE declaring it complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "dict_app_window": {"type": "string", "description": "Dict window name (default = app_window_ident)."},
                    "table_name": {"type": "string", "description": "Source table (default: resolved by the context proc)."},
                    "data_set_ident": {"type": "string", "description": "Default 'main'."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["app_window_ident"],
            },
        ),
        Tool(
            name="help_upsert_topic",
            description=(
                "Upsert a help topic (csHelpContents) + link it to NG windows "
                "(csHelpContentsNGAppWindows) in one call. subject/content/description/keywords: "
                "string (=PL) or {PL,EN,..}. Matches by help_contents_g or Subject_PL; an UNKNOWN "
                "help_contents_g creates the topic WITH that GUID (stable series). Content updates "
                "run a transactional D+I with the same G (links re-attached, persisted length "
                "verified) — csHelpContentsJSONSave U-path silently skips Content_*. WARNs on "
                "external <img> URLs (only inline base64 in Content_* renders). "
                "changelog_append: '<li>...</li>' (string=PL or {PL,EN,..}) appended to the FINAL "
                "</ul> per language (module-help convention: changelog list is last); idempotent; "
                "mutually exclusive with content. Works on any environment via `server` — DEV i "
                "PROD to dwa wywołania z tymi samymi argumentami."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"description": "String (PL) or {PL,EN,..}."},
                    "content": {"description": "HTML string (PL) or {PL,EN,..}; images inline base64."},
                    "description": {"description": "String or {PL,EN,..}."},
                    "keywords": {"description": "String or {PL,EN,..}."},
                    "window_idents": {"type": "array", "items": {"type": "string"},
                                      "description": "NG windows to link the topic to."},
                    "help_contents_g": {"type": "string", "description": "Topic GUID: existing → update; unknown → INSERT with this GUID (requires subject)."},
                    "changelog_append": {"description": "'<li><b>data</b> — opis</li>' (string=PL or {PL,EN,..}) doklejany do ostatniego </ul>."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV)."},
                },
                "required": [],
            },
        ),
        Tool(
            name="ai_tool_register",
            description=(
                "Register an AI tool end-to-end: csAIAgentsTools upsert (match by SQLProcedure/name) "
                "+ params sync (ai_tool_sync_params pitfalls handled) + ATTACH to agents via "
                "csAIAgentsToolsAgents — the chronically forgotten step (unattached tool = invisible "
                "to every agent). agents: names or csAIAgentsId."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name (usually = proc name without dbo.)."},
                    "description": {"type": "string", "description": "What the tool does (for the LLM)."},
                    "sql_procedure": {"type": "string", "description": "Backing procedure (dbo. optional). Required for new tools."},
                    "tool_type": {"type": "string", "description": "Default 'function'."},
                    "params": {"type": "array", "items": {"type": "object"},
                               "description": "[{name, type, description, isRequired?, typeJSON?}] — synced via ai_tool_sync_params."},
                    "agents": {"type": "array", "items": {}, "description": "Agent names or csAIAgentsId to attach the tool to."},
                    "use_permissions": {"type": "boolean"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="ng_create_lookup_window",
            description=(
                "Create a dedicated lookup window '<Table>Lookup' via csCreateNGDictFromTableDef "
                "(@DictType='lookup') and fix the auto-gen pitfalls: viewHTML NULL (not ''), "
                "onlyAsLookup=1, at least one VISIBLE layout column (else empty rows in c-list), "
                "col-header warning. Runs csNGValidateWindowForAI. Idempotent. Wire the host field "
                "afterwards with ng_add_lookup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Source cs* table, e.g. 'csHolidays'."},
                    "visible_fields": {"type": "array", "items": {"type": "string"},
                                       "description": "Columns to show in the lookup list (default: first string field)."},
                    "run_validator": {"type": "boolean", "description": "Default true."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                },
                "required": ["table_name"],
            },
        ),
        Tool(
            name="ng_replicate_window",
            description=(
                "Replicate the FULL config of one NG window DEV -> target (call with "
                "server=PROD): window + datasets(+stmSQL) + fields + key fields + layouts/"
                "cols/sort/aggrs + where fields + actions(+viewHTML) + lookup defs + links "
                "init + cols groups + window translates (missing csTranslate copied) + "
                "privileges (csPrivileges copied, grants NOT) + linked-window links BOTH "
                "directions (+ master tabIdent-<placement> where-field) + help links. Every "
                "row keeps its DEV G (HARD RULE 24 — upgrade packages replicate by G). "
                "Cache columns (dataSets/linkedWindows/fields) are rebuilt by the target's "
                "JSONSave cascades, not copied. prune=true deletes in-scope target rows "
                "absent on DEV (drift repair), children first. Zastępuje ręczny replay "
                "batchy + trick '~'. NOT replicated: menu entries, user/role grants — "
                "ng_add_menu_entry / ng_ensure_privileges(server=...) + rebuild_user_rights."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "app_window_ident": {"type": "string"},
                    "include_view_html": {"type": "boolean", "description": "Default true."},
                    "include_stmsql": {"type": "boolean", "description": "Default true."},
                    "prune": {"type": "boolean", "description": "Delete target rows absent on DEV (default false — report only)."},
                    "dry_run": {"type": "boolean", "description": "Report planned I/U/D without writing."},
                    "namespace_g": {"type": "string", "description": "csAppNameSpacesG (default Standard)."},
                    "server": {"type": "string", "enum": ["PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "CERES_TEST"],
                               "description": "TARGET environment (required — source is always DEV)."},
                },
                "required": ["app_window_ident", "server"],
            },
        ),
        Tool(
            name="repl_apply_pending",
            description=(
                "Kolejka replikacji konfiguracji U KLIENTA (csReplConfigChangesClientLog — ramki 'Up to a date' "
                "z DEV: wersje procedur V, wiersze konfiguracji I/U/D, exec E). action='status' (default, read-only): "
                "liczniki Status -1/0/1, błędy -1 z ProcessError, blokady blockFurtherRows, zaległe wg Opr/obiektu, "
                "pierwsze N zaległych (object_like zawęża), joby csCompaniesJobs (ostrzega gdy ApplyJob Active=0), "
                "joby msdb ApplyBackground, ostatnie próby. action='start': uruchamia backlog W TLE przez "
                "csReplConfigChangesClientLogApplyBackground z sp_set_session_context 'csUsrId' w tej samej sesji "
                "(bez tego job pada 'Incorrect syntax near ,'); odmawia gdy job już biegnie lub kolejka czysta; "
                "dry_run pokazuje co wykona. action='progress': job msdb, próby z csReplConfigChangesClientLogExecution "
                "w oknie since_minutes, tempo, bieżący LogId, szacowany czas do końca. Zanim ręcznie przeniesiesz obiekt "
                "na PROD (deploy_sql_object server=PROD / JSONSave) — sprawdź status: najpewniej już czeka w kolejce. "
                "Na DEV kolejka klienta nie ma zastosowania (użyj server=PROD/TESTGRODNO/…)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "start", "progress"], "description": "Default status."},
                    "server": {"type": "string", "enum": ["DEV", "PROD", "PLAY", "LOT", "CSSQL01", "SAVPOL", "TESTGRODNO", "CERES_TEST", "PBS", "PBSTEST", "PLATONPRE", "GRODNOFR"],
                               "description": "Target environment (default DEV — bez sensu dla start; kolejka klienta = PROD/TESTGRODNO/PLAY/…)."},
                    "usr_login": {"type": "string", "description": "start: login csUsr, którego csUsrId trafi do session_context (np. 'jmk'). Alternatywa: cs_usr_id."},
                    "cs_usr_id": {"type": "integer", "description": "start: csUsrId wprost (zamiast usr_login)."},
                    "cs_companies_id": {"type": "integer", "description": "start: firma instalacji; default = jedyna firma z jobami *ReplConfigChangesClientLog* w csCompaniesJobs."},
                    "object_like": {"type": "string", "description": "status: filtr ObjectName like '%x%' dla listy zaległych (np. 'csAIAgentsTools' albo nazwa procedury)."},
                    "top": {"type": "integer", "description": "status: ile pierwszych zaległych wypisać (default 20)."},
                    "since_minutes": {"type": "integer", "description": "progress: okno prób instalacji do tempa (default 60)."},
                    "dry_run": {"type": "boolean", "description": "start: tylko raport + komendy, bez uruchomienia."},
                },
                "required": [],
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
            dev_connection_string=arguments.get("_dev_connection_string"),
            target_label=arguments.get("_target_label") or "DEV",
        )

    if name == "register_cs_table":
        return register_cs_table(
            connection_string,
            table_name=arguments.get("table_name", ""),
            columns=arguments.get("columns") or [],
            indexes=arguments.get("indexes"),
            description_pl=arguments.get("description_pl"),
            description_en=arguments.get("description_en"),
            is_managed=bool(arguments.get("is_managed", False)),
            track_changes=bool(arguments.get("track_changes", False)),
        )

    if name == "register_job":
        return register_job(
            connection_string,
            procedure_name=arguments.get("procedure_name", ""),
            cs_companies_id=int(arguments.get("cs_companies_id") or 0),
            start_time=arguments.get("start_time", ""),
            interval_seconds=int(arguments.get("interval_seconds") or 0),
            job_desc_pl=arguments.get("job_desc_pl", ""),
            job_desc_en=arguments.get("job_desc_en"),
            thread_no=int(arguments.get("thread_no") or 8100),
            job_order=int(arguments["job_order"]) if arguments.get("job_order") is not None else None,
            active=bool(arguments.get("active", True)),
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
            is_translate=bool(arguments.get("is_translate", False)),
            before_translate=arguments.get("before_translate"),
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

    if name == "ng_get_window_config":
        return ng_get_window_config(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident"),
            include_stmsql=bool(arguments.get("include_stmsql", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_field_labels":
        return ng_set_field_labels(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            labels=arguments.get("labels") or {},
            data_set_ident=arguments.get("data_set_ident") or "main",
            target=arguments.get("target") or "field",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_layout_col":
        width = arguments.get("width")
        ord_v = arguments.get("ord")
        return ng_set_layout_col(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            width=float(width) if width is not None else None,
            is_visible=arguments.get("is_visible"),
            ord=int(ord_v) if ord_v is not None else None,
            cols_group_ident=arguments.get("cols_group_ident"),
            clear_cols_group=bool(arguments.get("clear_cols_group", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_upsert_cols_group":
        return ng_upsert_cols_group(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            cols_group_ident=arguments.get("cols_group_ident", ""),
            descriptions=arguments.get("descriptions") or {},
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_upsert_tabs_group":
        return ng_upsert_tabs_group(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            tab_group_ident=arguments.get("tab_group_ident", ""),
            descriptions=arguments.get("descriptions") or {},
            ord=arguments.get("ord"),
            translate_ident=arguments.get("translate_ident"),
            link_to_windows=arguments.get("link_to_windows"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_stmsql":
        return ng_set_stmsql(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            stm_sql=arguments.get("stm_sql", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            test=bool(arguments.get("test", True)),
            test_where=arguments.get("test_where")
            or '{"searchText":"test","dateFrom":"2026-01-01","dateTo":"2026-12-31"}',
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_dataset_props":
        return ng_set_dataset_props(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            props=arguments.get("props") or {},
            data_set_ident=arguments.get("data_set_ident") or "main",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "rebuild_user_rights":
        cid = arguments.get("cs_companies_id")
        uid = arguments.get("cs_usr_id")
        return rebuild_user_rights(
            connection_string,
            cs_companies_id=int(cid) if cid is not None else None,
            cs_usr_id=int(uid) if uid is not None else None,
            confirm_all=bool(arguments.get("confirm_all", False)),
        )

    if name == "ai_tool_sync_params":
        return ai_tool_sync_params(
            connection_string,
            tool_name=arguments.get("tool_name", ""),
            params=arguments.get("params"),
            generate_sync_script=bool(arguments.get("generate_sync_script", True)),
        )

    if name == "ng_add_lookup":
        return ng_add_lookup(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            lookup_window_ident=arguments.get("lookup_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            source_kind=arguments.get("source_kind") or "rows",
            is_multi_select=bool(arguments.get("is_multi_select", False)),
            close_kind=arguments.get("close_kind"),
            search_get=bool(arguments.get("search_get", True)),
            gets=arguments.get("gets"),
            sets=arguments.get("sets"),
            auto_sets=bool(arguments.get("auto_sets", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "describe":
        return describe(
            connection_string,
            object_name=arguments.get("object_name", ""),
        )

    if name == "sql_grep":
        return sql_grep(
            connection_string,
            pattern=arguments.get("pattern", ""),
            name_like=arguments.get("name_like"),
            top=int(arguments.get("top") or 100),
        )

    if name == "ng_preview_dataset":
        cid = arguments.get("cs_companies_id")
        uid = arguments.get("cs_usr_id")
        return ng_preview_dataset(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            where=arguments.get("where"),
            top=int(arguments.get("top") or 5),
            cs_companies_id=int(cid) if cid is not None else None,
            cs_usr_id=int(uid) if uid is not None else None,
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_bulk_layout":
        return ng_bulk_layout(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            columns=arguments.get("columns") or [],
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_register_translates":
        return ng_register_translates(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            translates=arguments.get("translates") or [],
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_linked_window":
        return ng_add_linked_window(
            connection_string,
            app_window_ident_from=arguments.get("app_window_ident_from", ""),
            app_window_ident_to=arguments.get("app_window_ident_to", ""),
            placement=arguments.get("placement", ""),
            map_fields=arguments.get("map_fields") or [],
            ord=int(arguments.get("ord") or 1),
            labels=arguments.get("labels"),
            tab_default=arguments.get("tab_default"),
            wire_one_item_only=bool(arguments.get("wire_one_item_only", True)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_filter":
        return ng_add_filter(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            field_ident=arguments.get("field_ident", ""),
            format_type=arguments.get("format_type", ""),
            sql_base_type=arguments.get("sql_base_type", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            sql_column_params=arguments.get("sql_column_params"),
            value_def=arguments.get("value_def"),
            not_use_for_get_data=arguments.get("not_use_for_get_data"),
            ord=arguments.get("ord"),
            label_pl=arguments.get("label_pl"),
            label_en=arguments.get("label_en"),
            watermark=arguments.get("watermark"),
            lookup_window_ident=arguments.get("lookup_window_ident"),
            lookup_sets=arguments.get("lookup_sets"),
            lookup_gets=arguments.get("lookup_gets"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_action":
        return ng_add_action(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            action_ident=arguments.get("action_ident", ""),
            data_set_ident=arguments.get("data_set_ident") or "main",
            labels=arguments.get("labels"),
            sql_name=arguments.get("sql_name"),
            kind=arguments.get("kind"),
            crud=arguments.get("crud"),
            show_view=arguments.get("show_view"),
            view_html=arguments.get("view_html"),
            ord=arguments.get("ord"),
            hide_when_empty=arguments.get("hide_when_empty"),
            show_confirmation=arguments.get("show_confirmation"),
            add_current_row=arguments.get("add_current_row"),
            add_where=arguments.get("add_where"),
            ref_kind=arguments.get("ref_kind"),
            close_after_exec=arguments.get("close_after_exec"),
            is_auto=arguments.get("is_auto"),
            fields=arguments.get("fields"),
            extra=arguments.get("extra"),
            wire_privileges=bool(arguments.get("wire_privileges", True)),
            rebuild=bool(arguments.get("rebuild", False)),
            cs_companies_id=arguments.get("cs_companies_id"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_ensure_privileges":
        return ng_ensure_privileges(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            create_if_missing=bool(arguments.get("create_if_missing", True)),
            privilege_desc_pl=arguments.get("privilege_desc_pl"),
            privilege_desc_en=arguments.get("privilege_desc_en"),
            privilege_group_pl=arguments.get("privilege_group_pl") or "DSM",
            fix_gaps=bool(arguments.get("fix_gaps", False)),
            grant_cs_usr_id=arguments.get("grant_cs_usr_id"),
            grant_app_roles_id=arguments.get("grant_app_roles_id"),
            grant_cs_companies_id=arguments.get("grant_cs_companies_id"),
            grant_privilege_g=arguments.get("grant_privilege_g"),
            rebuild=bool(arguments.get("rebuild", False)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_add_menu_entry":
        return ng_add_menu_entry(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            dict_app_window=arguments.get("dict_app_window"),
            parent_menu_path=arguments.get("parent_menu_path"),
            parent_g=arguments.get("parent_g"),
            labels=arguments.get("labels"),
            menu_path=arguments.get("menu_path"),
            ord=arguments.get("ord"),
            kind=arguments.get("kind") or "NGDict",
            icon=arguments.get("icon"),
            usable=bool(arguments.get("usable", True)),
            rebuild=bool(arguments.get("rebuild", False)),
            cs_companies_id=arguments.get("cs_companies_id"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_set_sort":
        return ng_set_sort(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            columns=arguments.get("columns") or [],
            data_set_ident=arguments.get("data_set_ident") or "main",
            layout_ident=arguments.get("layout_ident") or "default",
            sort_ident=arguments.get("sort_ident") or "default",
            is_def=bool(arguments.get("is_def", True)),
            labels=arguments.get("labels"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_diff_with_dict":
        return ng_diff_with_dict(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            dict_app_window=arguments.get("dict_app_window"),
            table_name=arguments.get("table_name"),
            data_set_ident=arguments.get("data_set_ident") or "main",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "help_upsert_topic":
        return help_upsert_topic(
            connection_string,
            subject=arguments.get("subject"),
            content=arguments.get("content"),
            description=arguments.get("description"),
            keywords=arguments.get("keywords"),
            window_idents=arguments.get("window_idents"),
            help_contents_g=arguments.get("help_contents_g"),
            changelog_append=arguments.get("changelog_append"),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "ng_replicate_window":
        return ng_replicate_window(
            connection_string,
            app_window_ident=arguments.get("app_window_ident", ""),
            dev_connection_string=arguments.get("_dev_connection_string"),
            target_label=arguments.get("_target_label") or "DEV",
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
            include_view_html=bool(arguments.get("include_view_html", True)),
            include_stmsql=bool(arguments.get("include_stmsql", True)),
            prune=bool(arguments.get("prune", False)),
            dry_run=bool(arguments.get("dry_run", False)),
        )

    if name == "ai_tool_register":
        return ai_tool_register(
            connection_string,
            name=arguments.get("name", ""),
            description=arguments.get("description"),
            sql_procedure=arguments.get("sql_procedure"),
            tool_type=arguments.get("tool_type") or "function",
            params=arguments.get("params"),
            agents=arguments.get("agents"),
            use_permissions=arguments.get("use_permissions"),
        )

    if name == "ng_create_lookup_window":
        return ng_create_lookup_window(
            connection_string,
            table_name=arguments.get("table_name", ""),
            visible_fields=arguments.get("visible_fields"),
            run_validator=bool(arguments.get("run_validator", True)),
            namespace_g=arguments.get("namespace_g") or DEFAULT_NAMESPACE_G,
        )

    if name == "repl_apply_pending":
        return repl_apply_pending(
            connection_string,
            action=arguments.get("action") or "status",
            usr_login=arguments.get("usr_login"),
            cs_usr_id=int(arguments["cs_usr_id"]) if arguments.get("cs_usr_id") is not None else None,
            cs_companies_id=int(arguments["cs_companies_id"]) if arguments.get("cs_companies_id") is not None else None,
            object_like=arguments.get("object_like"),
            top=int(arguments.get("top") or 20),
            since_minutes=int(arguments.get("since_minutes") or 60),
            dry_run=bool(arguments.get("dry_run", False)),
            target_label=arguments.get("_target_label") or "DEV",
        )

    raise ValueError(f"Unknown cs tool: {name}")
