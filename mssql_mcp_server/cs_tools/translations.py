"""Read-only coverage audit of UI translations; business data and URL slugs are excluded."""

from __future__ import annotations

import json
from pyodbc import connect


UI_TRANSLATION_FIELDS = {
    "csTranslate": ["Content"],
    "csTranslate4Companies": ["Content"],
    "csNGAppWindows": ["appWindowDesc"],
    "csNGAppWindowDataSetsFields": ["dataFieldLabDesc", "dataFieldColDesc", "dataFieldWatermarkDesc"],
    "csNGAppWindowDataSetsWhereFields": ["dataFieldLabDesc", "dataFieldColDesc", "dataFieldWatermarkDesc"],
    "csNGAppWindowDataSetsActions": ["actionDesc"],
    "csNGAppWindowDataSetsLayouts": ["layoutDesc"],
    "csNGAppWindowDataSetsSortIdents": ["sortDesc"],
    "csNGAppWindowDataSetsPageSizesIdents": ["pageSizesDesc"],
    "csNGAppWindowDataSetsLayoutsAggrs": ["aggrDesc"],
    "csNGAppWindowColsGroups": ["dataFieldColsGroupDesc"],
    "csNGAppWindowTabsGroups": ["tabGroupDesc"],
    "csNGAppWindowsLinks": ["appWindowLinkDesc"],
    "csNGAppWindowDataSetsExports": ["exportDesc"],
    "csNGAppWindowDataSetsExports4Companies": ["exportDesc"],
    "csNGMenuDef": ["menuDesc"],
    "csNGMasterMenuDef": ["NGMasterMenuDef"],
    "csNGCacheSets": ["CacheSetDesc"],
}


def _filled(column: str) -> str:
    # NULL, empty text and whitespace are all gaps; preserve the source verbatim in samples.
    return f"nullif(ltrim(rtrim(replace(replace(replace({column}, char(9), N' '), char(10), N' '), char(13), N' '))), N'')"


def audit_ui_translations(connection_string: str, languages=None, table_name=None, sample_limit=20) -> str:
    """Inspect real columns and csSupLang; never infer coverage from a fixed language list."""
    if table_name is not None and table_name not in UI_TRANSLATION_FIELDS:
        return "Error: table_name must be one of: " + ", ".join(UI_TRANSLATION_FIELDS)
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int) or not 0 <= sample_limit <= 100:
        return "Error: sample_limit must be an integer between 0 and 100."
    if languages is not None and (not isinstance(languages, list) or not languages or not all(isinstance(l, str) for l in languages)):
        return "Error: languages must be a nonempty list of language suffixes."
    result = {"languages": [], "coverage": [], "missing_columns": [], "samples": []}
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            available = [r[0] for r in cur.execute("select LanguageSuffix from dbo.csSupLang with(nolock) order by Ord, csSupLangId").fetchall()]
            selected = list(dict.fromkeys(l.strip().upper() for l in languages)) if languages is not None else available
            if any(l not in available for l in selected):
                return "Error: languages must exist in csSupLang: " + ", ".join(available)
            result["languages"] = selected
            for table, prefixes in UI_TRANSLATION_FIELDS.items():
                if table_name and table != table_name:
                    continue
                columns = {r[0] for r in cur.execute(
                    "select name from sys.columns with(nolock) where object_id = object_id(?)", "dbo." + table
                ).fetchall()}
                if not columns:
                    result["missing_columns"].append({"table": table, "reason": "table unavailable"})
                    continue
                for prefix in prefixes:
                    sources = [f"t.[{prefix}_{l}]" for l in ("PL", "EN") if f"{prefix}_{l}" in columns]
                    if not sources:
                        result["missing_columns"].append({"table": table, "field": prefix, "reason": "no source column"})
                        continue
                    source = sources[0] if len(sources) == 1 else "coalesce(" + ", ".join(_filled(s) + " collate database_default" for s in sources) + ")"
                    present = [l for l in selected if f"{prefix}_{l}" in columns]
                    result["missing_columns"].extend({"table": table, "field": prefix, "language": l} for l in selected if l not in present)
                    counts = ["count_big(*)"] + [f"sum(convert(bigint, iif({_filled(f't.[{prefix}_{l}]')} is null, 1, 0)))" for l in present]
                    predicate = _filled(source) + " is not null"
                    if table in ("csTranslate", "csTranslate4Companies"):
                        predicate += " and exists(select 1 from dbo.csNGAppWindowTranslates w with(nolock) where w.csTranslateG = t.csTranslateG)"
                    row = cur.execute(f"select {', '.join(counts)} from dbo.[{table}] t with(nolock) where {predicate}").fetchone()
                    missing = {l: int(row[i + 1] or 0) for i, l in enumerate(present)}
                    result["coverage"].append({"table": table, "field": prefix, "source_rows": int(row[0]), "missing": missing})
                    remaining = sample_limit - len(result["samples"])
                    if remaining > 0 and any(missing.values()):
                        condition = " or ".join(_filled(f"t.[{prefix}_{l}]") + " is null" for l in present)
                        sample = cur.execute(
                            f"select top (?) t.[{table}G], {source} from dbo.[{table}] t with(nolock) where {predicate} and ({condition}) order by t.[{table}Id]", remaining
                        ).fetchall()
                        result["samples"].extend({"table": table, "field": prefix, "row_g": str(r[0]), "source": r[1]} for r in sample)
    result["missing_cells"] = sum(sum(c["missing"].values()) for c in result["coverage"])
    return json.dumps(result, ensure_ascii=False)
