#!/usr/bin/env python3
"""
web_cli_bridge.py — WebSocket <-> Unix socket bridge so the dashboard's
CLI tab can talk directly to SessionManagerNode's display.sock /
cli_input.sock, the same low-level channel cli_node.py uses — instead of
going through a tmux pane + ttyd.

Each browser WebSocket connection gets its own independent pair of Unix
socket connections. SessionManagerNode already broadcasts display.sock
output to every connected reader and accepts keyboard/resize input from
any number of cli_input.sock writers, so multiple simultaneous browser
tabs (and the real CLINode terminal) can all be attached at once without
kicking each other off.

Wire protocol between browser and this bridge:
  - binary WS frame  -> raw keystroke bytes, forwarded as-is to cli_input.sock
  - text WS frame     -> JSON control message, currently just
                          {"type": "resize", "rows": N, "cols": N}
  - bridge -> browser -> raw PTY bytes as binary WS frames (direct passthrough)
"""

import asyncio
import json
import os
import sys

import websockets

sys.path.insert(0, os.path.dirname(__file__))
import config

BRIDGE_PORT = int(os.environ.get("WEB_CLI_BRIDGE_PORT", "7690"))


async def handle_client(websocket):
    try:
        display_reader, display_writer = await asyncio.open_unix_connection(config.DISPLAY_SOCK)
        input_reader, input_writer = await asyncio.open_unix_connection(config.CLI_INPUT_SOCK)
    except Exception as e:
        await websocket.close(reason=f"SessionManagerNode unreachable: {e}")
        return

    async def pty_to_ws():
        try:
            while True:
                data = await display_reader.read(4096)
                if not data:
                    break
                await websocket.send(data)
        except Exception:
            pass

    async def ws_to_pty():
        try:
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        msg = json.loads(message)
                        if msg.get("type") == "resize":
                            payload = ("\x00" + json.dumps({
                                "type": "resize",
                                "rows": int(msg["rows"]),
                                "cols": int(msg["cols"]),
                            }) + "\n").encode()
                            input_writer.write(payload)
                            await input_writer.drain()
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass
                else:
                    input_writer.write(message)
                    await input_writer.drain()
        except Exception:
            pass

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    finally:
        display_writer.close()
        input_writer.close()


async def main():
    async with websockets.serve(handle_client, "0.0.0.0", BRIDGE_PORT, max_size=None):
        print(f"web_cli_bridge listening on :{BRIDGE_PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
