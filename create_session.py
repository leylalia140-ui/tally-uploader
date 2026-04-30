#!/usr/bin/env python3
"""
Run this script ONCE locally to generate a Pyrogram session string.
Then add the output as TG_SESSION in Railway environment variables.
"""
from pyrogram import Client

api_id = 34441493
api_hash = "60df83a7ba18292fd0890434fd172924"

print("Telegram Login — du bekommst gleich einen Code in Telegram/SMS")
print()

with Client("_tmp_session", api_id=api_id, api_hash=api_hash) as app:
    session = app.export_session_string()

print("\n✓ Fertig! Kopiere diesen String in Railway als TG_SESSION:\n")
print(session)
print()
