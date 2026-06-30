"""
Ailin channel poster — posts content to "Ailin Life" channel as Ailin's user account.
Session file: ~/Axon/ailin.session (excluded from git via .gitignore)
"""
import asyncio
import os
import sys
import logging
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

API_ID   = int(os.environ["AILIN_API_ID"])
API_HASH = os.environ["AILIN_API_HASH"]
SESSION  = os.path.join(os.path.dirname(__file__), "ailin")  # -> ailin.session

logging.basicConfig(level=logging.WARNING)


async def get_client() -> TelegramClient:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    return client


async def post_text(channel: str, text: str) -> None:
    client = await get_client()
    async with client:
        await client.send_message(channel, text)
        print(f"Posted to {channel}")


async def post_photo(channel: str, image_path: str, caption: str = "") -> None:
    client = await get_client()
    async with client:
        await client.send_file(channel, image_path, caption=caption)
        print(f"Posted photo to {channel}")


async def list_channels() -> None:
    client = await get_client()
    async with client:
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                print(f"  {dialog.id}  {dialog.name}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ailin channel poster")
    sub = parser.add_subparsers(dest="cmd")

    p_post = sub.add_parser("post", help="Post text to channel")
    p_post.add_argument("channel", help="Channel username or ID, e.g. @ailinlife")
    p_post.add_argument("text", help="Text to post")

    p_photo = sub.add_parser("photo", help="Post photo to channel")
    p_photo.add_argument("channel")
    p_photo.add_argument("image", help="Path to image file")
    p_photo.add_argument("--caption", default="")

    p_list = sub.add_parser("channels", help="List channels the account is in")

    args = parser.parse_args()

    if args.cmd == "post":
        asyncio.run(post_text(args.channel, args.text))
    elif args.cmd == "photo":
        asyncio.run(post_photo(args.channel, args.image, args.caption))
    elif args.cmd == "channels":
        asyncio.run(list_channels())
    else:
        parser.print_help()
