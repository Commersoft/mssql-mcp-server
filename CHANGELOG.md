# Changelog

All notable changes to the MSSQL MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — cs* write/automation tools (`cs_tools.py`)
Narzędzia kapsułkujące niejawne reguły frameworku cs* (egzekwowane programowo zamiast „z pamięci agenta"):

- **`deploy_sql_object`** — deploy procedury/funkcji/widoku/triggera przez `csAddObjVer`. Automatycznie: `objectName` bez prefiksu `dbo.`, `@pv` = najnowsza wersja, świeży unikalny `@v` (sprawdzany pod kątem kolizji), podział na 3 batche (omija problem `GO`), sprzątanie osieroconego `inProgress=1`.
- **`cs_jsonsave`** — generyczny wrapper na `<T>JSONSave` z **parametryzowanym** `@data` (koniec z błędami escapowania multiline/diakrytyków). Parsuje `@response xml` → czytelny wynik.
- **`add_cs_column`** — kolumna do tabeli cs* przez `csSysColumnsJSONSave` + `csSysTablesRebuild`. Auto `ColumnOrder`, wymóg kompletu 12× `ColumnDesc_XX`, oba parametry rebuild. Idempotentne.
- **`add_ng_field`** — pole okna NG: Fields + LayoutsCols (+ opcjonalne wstrzyknięcie `<c-edit>` do `viewHTML` akcji ins/upd). Idempotentny UPSERT; ustawia `labelDataFieldIdent` i niezerowy `width`.
- **`get_cs_object_versions`** — historia `csSysObjVer` + stan `inProgress` obiektu (diagnostyka deployu).
- **`update_view_html`** — synchronizacja `<template>` z pliku `.vue` do `viewHTML` w bazie na żądanie (ten sam efekt co husky pre-commit, bez commit+push). `component == app_window_ident` → `csNGAppWindows.viewHTML` (przez `csNGAppWindowsJSONSave`); `component` w formie `<dataSet>_<action>` (np. `main_ins`) → `csNGAppWindowDataSetsActions.viewHTML` (przez `csNGAppWindowDataSetsActionsJSONSave`). Ekstrakcja `<template>` jak husky (linie między `<template>` a ostatnim `</template>`, trim, `##asterix##`→`*`); zapis przez JSONSave (cache `dataSets` przebudowuje się sam). Wejście: `file_path` (preferowane) lub `content`.

Wszystkie podłączone w `server.py` (`CS_TOOL_NAMES`, `tool_descriptors`, dispatch w `call_tool`).

## [1.0.0] - 2025-07-02

### Added
- Initial release of MSSQL MCP Server
- Full Model Context Protocol (MCP) implementation for SQL Server
- Advanced query preprocessing with multi-line support
- Comprehensive environment variable configuration
- Support for both Windows and SQL authentication
- Table schema introspection capabilities
- Sample data viewing with pagination
- Connection pooling and timeout management
- Detailed error messages and logging
- Support for SQL Server 2016+ and Azure SQL Database

### Features
- **Query Preprocessing Engine**: Handles complex multi-line queries, comments, and GO statements
- **Dual Authentication**: Seamless support for both Windows and SQL authentication modes
- **Resource Management**: Browse tables as MCP resources with schema and data views
- **Security First**: Connection encryption, certificate validation, and secure credential handling
- **Developer Friendly**: Comprehensive logging, error messages, and debugging support

### Technical Details
- Built on MCP protocol for AI assistant integration
- Uses pyodbc for robust SQL Server connectivity
- Implements connection pooling for performance
- Full Python 3.8+ compatibility
- Type hints throughout the codebase