"""
cs_tools — write/automation tools for the cs* framework (MSSQL MCP Server).

These tools encapsulate the implicit rules of the cs* framework that otherwise
require an agent to remember 5-10 niche conventions per operation. Each tool
enforces those rules programmatically (parametrized @data, no `dbo.` prefix in
objectName, unique @v, latest @pv, full 12-language ColumnDesc, both rebuild
params, etc.).

Tools:
- deploy_sql_object   : deploy a procedure/function/view/trigger via csAddObjVer
                        (auto @pv=latest, fresh unique @v, 3-batch split, orphan cleanup).
- cs_jsonsave         : call any <T>JSONSave with parametrized @data (safe escaping),
                        parse @response xml into a readable result.
- add_cs_column       : add a column to a cs* table (csSysColumnsJSONSave + rebuild),
                        auto ColumnOrder, full 12x ColumnDesc, both rebuild params.
- register_cs_table   : register a table END-TO-END (csSysTables + csSysColumns 12-lang +
                        csSysIndexes + rebuild + verification; managed auto Id/G + pk/uq).
- register_job        : register a csCompaniesJobs job (stable G, dbo. prefix, JobOrder,
                        weekly/daily interval conventions; cross-server via `server`).
- add_ng_field        : add a field to an NG window (Fields + LayoutCols + optional
                        ins/upd viewHTML injection).
- get_cs_object_versions : list csSysObjVer history + inProgress state for an object.
- ng_get_window_config : read-only dump of a full NG window configuration
                        (datasets, fields, layout cols, groups, actions, lookups...).
- ng_set_field_labels : update field/whereField labels (lab/col/watermark) with the
                        formatType pitfall handled (silent reset to 'string' otherwise).
- ng_set_layout_col   : upsert grid layout col props (width/isVisible/ord/colsGroupIdent)
                        via minimal-U (no natural key -> no label re-validation trap).
- ng_upsert_cols_group : upsert a grid column group (csNGAppWindowColsGroups).
- ng_upsert_tabs_group : upsert a tabs group of linked windows (csNGAppWindowTabsGroups,
                        per master window) + optionally attach links (tabGroupIdent).
- ng_set_stmsql       : replace a dataset stmSQL with a MANDATORY sp_executesql test
                        BEFORE saving (with and without dates).
- ng_set_dataset_props : update whitelisted csNGAppWindowDataSets columns (pageSize,
                        pagingDisabled, getMetaInfo-like toggles) via minimal-U.
- rebuild_user_rights : rebuild per-user cache (appMainMenuJSON, appWindowIdentsWithRights,
                        warehousesRights) after menu/window/privileges changes.
- ai_tool_sync_params : sync csAIAgentsToolsParams with an AI tool procedure (upsert with
                        the U-required-fields gotcha, heuristic $.key diff, redeploy script).
- ng_add_lookup       : wire a lookup on an NG field (LookupDefs + Get/Set mappings with
                        all sourceKind conventions).
- describe            : compact schema of a DB object — table/view columns (type/null/PK/FK)
                        or procedure/function parameters (avoids 'Invalid column name' guessing).
- sql_grep           : case-insensitive substring search over SQL object bodies
                        (object:line:content, like Grep over files).
- ng_preview_dataset  : dry-run an NG dataset — expand /*FIELDS*/ + stmSQL into the real data
                        SELECT and execute it (top N), catching reserved-word/invalid-column/
                        @var runtime errors the config-only validator misses.
- ng_bulk_layout      : bulk upsert grid layout columns (visible/ord/width/group) in one call.
- ng_register_translates : register gT() idents (reuse csTranslate by content/GUID or create).
- ng_add_linked_window : master->detail link (csNGAppWindowsLinks + LinksFields + optional
                        tabIdent where-field) in one call.
- ng_add_filter       : filter-panel where-field + optional lookup + watermark in one call.
- ng_replicate_window : replicate the FULL config of an NG window DEV -> target (server=PROD)
                        with the DEV G on every row (HARD RULE 24); replaces the manual
                        batch-replay + '~' trick; prune=drift repair, dry_run supported.
- repl_apply_pending  : client-side replication queue (csReplConfigChangesClientLog):
                        status (Status -1/0/1, errors, blocks, jobs Active), start the backlog
                        in background (ApplyBackground + sp_set_session_context csUsrId in the
                        same session), progress (Execution rows, msdb job, rate, ETA). server=PROD/…

All tools are WRITE-CAPABLE (except describe/sql_grep/ng_preview_dataset and
repl_apply_pending(action=status|progress) — read-only). They run against the same connection_string as the
read tools. Destructive safety: deploy_sql_object never drops unless csAddObjVer
returns @drop=1; orphan cleanup only flips inProgress=0 (never DELETE).
"""

from __future__ import annotations

from ._core import (
    COLUMN_DESC_LANGS,
    DEFAULT_NAMESPACE_G,
    NG_COLSGROUP_LANGS,
    NG_DATASET_PROPS_WHITELIST,
    NG_LABEL_LANGS,
    SCRIPT_CONVERTERS_G,
    _as_int,
    _ensure_translate,
    _exec_scalar,
    _jsonsave,
    _new_guid,
    _split_go_batches,
    _stable_guid,
    _xml_response_to_text,
)
from .deploy import cs_jsonsave, deploy_sql_object, get_cs_object_versions
from .schema import add_cs_column, register_cs_table, register_job
from .ng_window import (
    STMSQL_TEST_PARAMS_DECL,
    _extract_template,
    _test_stmsql,
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
from .replicate import (
    _REPLICATE_TABLES,
    _copy_ref_row,
    _fetch_dicts,
    _json_value,
    _table_columns,
    _table_exists,
    ng_replicate_window,
)
from .help_tools import _help_content_replace, help_upsert_topic
from .ai_tools import ai_tool_register, ai_tool_sync_params
from .repl_queue import repl_apply_pending
from .registry import CS_TOOL_NAMES, handle_tool, tool_descriptors
from ._core import logger
