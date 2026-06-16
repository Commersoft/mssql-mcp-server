#!/usr/bin/env python3
"""
Simple runner script for MSSQL MCP Server
This ensures the server runs correctly when called from MCP configuration
"""

def _load_env():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    _load_env()
    from mssql_mcp_server import main
    import asyncio
    asyncio.run(main())