import os

import discord
from dotenv import load_dotenv

load_dotenv()

# No privileged intents needed:
# - Slash commands give us structured input (no need to read raw message text in guilds)
# - DMs are exempt from the message_content intent requirement, so we can
#   still read what users reply with in their DMs to the bot.
intents = discord.Intents.default()

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(token)
