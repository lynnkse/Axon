#!/usr/bin/env python3
"""
AutoCAD Search MCP Server

Exposes search_autocad(query, count) tool that performs semantic search
over the autocad_docs table in Supabase.

Register once:
  claude mcp add autocad-search -- python3 /path/to/autocad_search_mcp.py

SUPABASE_URL and SUPABASE_ANON_KEY are inherited from the relay environment.
"""

import asyncio
import json
import os
import urllib.request

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

server = Server("autocad-search")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_autocad",
            description=(
                "Search the AutoCAD 2025 official Russian documentation. "
                "Returns relevant documentation chunks to help answer questions about "
                "AutoCAD commands, menus, ribbons, buttons, workflows, and features. "
                "Always call this before answering any AutoCAD question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (Russian or English)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default: 8, max: 15)",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "search_autocad":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments.get("query", "").strip()
    count = min(int(arguments.get("count", 8)), 15)

    if not query:
        return [types.TextContent(type="text", text="Error: query is required")]
    if not SUPABASE_URL or not SUPABASE_KEY:
        return [types.TextContent(type="text", text="Error: Supabase not configured (SUPABASE_URL/SUPABASE_ANON_KEY missing)")]

    payload = json.dumps({
        "query": query,
        "table": "autocad_docs",
        "match_count": count,
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL.rstrip('/')}/functions/v1/search",
        data=payload,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            chunks = json.load(r)
    except Exception as e:
        return [types.TextContent(type="text", text=f"Search error: {e}")]

    if not chunks:
        return [types.TextContent(type="text", text="No results found in documentation.")]

    parts = []
    for i, c in enumerate(chunks, 1):
        title = c.get("title", "Untitled")
        content = c.get("content", "")
        sim = c.get("similarity", 0)
        parts.append(f"[{i}] {title} (sim={sim:.3f})\n{content}")

    return [types.TextContent(type="text", text="\n\n---\n\n".join(parts))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
