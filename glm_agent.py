#!/usr/bin/env python3
"""
GLM Agent MCP Server

Exposes a `delegate_to_glm` tool that Claude can call to delegate tasks
to ZhipuAI's GLM-Z1-32B reasoning model.

Usage:
  claude mcp add glm-agent python3 /path/to/glm_agent.py
  Set env var: ZHIPUAI_API_KEY=your_key

Tool: delegate_to_glm(task, context="", model="glm-z1-32b")
"""

import os
import sys
import asyncio
from zhipuai import ZhipuAI
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("glm-agent")

def get_client() -> ZhipuAI:
    api_key = os.environ.get("ZHIPUAI_API_KEY")
    if not api_key:
        raise RuntimeError("ZHIPUAI_API_KEY environment variable not set")
    return ZhipuAI(api_key=api_key)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="delegate_to_glm",
            description=(
                "Delegate a task to GLM-Z1-32B (ZhipuAI reasoning model). "
                "Use for complex code tasks, multi-file analysis, math-heavy reasoning, "
                "or when you want a second opinion. GLM will reason through the task "
                "and return a detailed response. You review and apply the result."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task or question for GLM. Be specific and include all necessary context."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: file contents, code snippets, or additional background. Paste raw content here.",
                        "default": ""
                    },
                    "model": {
                        "type": "string",
                        "description": "GLM model to use. Default: glm-5.1 (flagship, best quality). Use glm-4-flash for faster/cheaper simple tasks.",
                        "default": "glm-5.1"
                    }
                },
                "required": ["task"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "delegate_to_glm":
        raise ValueError(f"Unknown tool: {name}")

    task = arguments["task"]
    context = arguments.get("context", "")
    model = arguments.get("model", "glm-5.1")

    # Build prompt
    if context:
        prompt = f"{task}\n\n---\nContext:\n{context}"
    else:
        prompt = task

    client = get_client()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        content = response.choices[0].message.content

        # Include usage info if available
        usage_note = ""
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage_note = (
                f"\n\n---\n[GLM usage: {u.prompt_tokens} in / "
                f"{u.completion_tokens} out / {u.total_tokens} total tokens | model: {model}]"
            )

        return [types.TextContent(type="text", text=content + usage_note)]

    except Exception as e:
        return [types.TextContent(type="text", text=f"[GLM error: {e}]")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
