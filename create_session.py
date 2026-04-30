#!/usr/bin/env python3
import asyncio
from hydrogram import Client

api_id = 34441493
api_hash = "60df83a7ba18292fd0890434fd172924"

async def main():
    print("Telegram Login — du bekommst gleich einen Code in Telegram/SMS")
    print()
    async with Client("_tmp_session", api_id=api_id, api_hash=api_hash) as app:
        session = await app.export_session_string()
    print("\n✓ Fertig! Kopiere diesen String in Railway als TG_SESSION:\n")
    print(session)
    print()

asyncio.run(main())
