import os
from dataclasses import dataclass, field

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# No privileged intents needed:
# - Slash commands give us structured input (no need to read raw message text in guilds)
# - DMs are exempt from the message_content intent requirement, so we can
#   still read what users reply with in their DMs to the bot.
intents = discord.Intents.default()


@dataclass
class PickSession:
    player_one: discord.User
    player_two: discord.User
    channel_id: int
    responses: dict = field(default_factory=dict)  # user_id -> answer text


# Maps a user's ID to the session they're currently part of. Both players in
# a session point to the same PickSession object. In-memory only — sessions
# don't survive a bot restart.
pending_picks: dict[int, PickSession] = {}

@dataclass
class RPSSession:
    player_one: discord.User
    player_two: discord.User
    channel_id: int
    choices: dict = field(default_factory=dict)  # user_id -> "rock"/"paper"/"scissors"


# Same idea as pending_picks, but for active rock-paper-scissors rounds.
pending_rps: dict[int, RPSSession] = {}


class RPSChoiceView(discord.ui.View):
    """Buttons sent in a player's DM, constraining them to exactly one of
    rock/paper/scissors instead of free text."""

    def __init__(self, session: RPSSession, player: discord.User):
        super().__init__(timeout=None)  # players may take a while to respond
        self.session = session
        self.player = player

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Defensive check — this view is only ever sent in this player's own
        # DM, but guard against the unexpected anyway.
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "This isn't your choice to make.", ephemeral=True
            )
            return False
        return True

    async def _choose(self, interaction: discord.Interaction, choice: str):
        self.session.choices[self.player.id] = choice
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"You picked **{choice.capitalize()}**. Waiting for your opponent...",
            view=self,
        )
        if len(self.session.choices) == 2:
            await reveal_rps(self.session)

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "scissors")



class RatBotClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)


client = RatBotClient()

# Syncing to a specific guild makes the command show up instantly (vs. up to
# an hour for a global sync). Set GUILD_ID in .env to your test server's ID.
GUILD_ID = os.getenv("GUILD_ID")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        synced = await client.tree.sync(guild=guild)
    else:
        synced = await client.tree.sync()
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"Synced {len(synced)} command(s)")


@client.tree.command(
    name="blindpick",
    description="Privately ask two players to make a pick",
    guild=discord.Object(id=int(GUILD_ID)) if GUILD_ID else None,
)
@app_commands.describe(player_one="First player", player_two="Second player")
async def blindpick(
    interaction: discord.Interaction,
    player_one: discord.User,
    player_two: discord.User,
):
    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "Pick two different players.", ephemeral=True
        )
        return
    
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not a DM.", ephemeral=True)
        return


    if player_one.id in pending_picks or player_two.id in pending_picks:
        await interaction.response.send_message(
            "One of those players already has a blind pick in progress. "
            "Wait for it to finish first.",
            ephemeral=True,
        )
        return

    prompt = (
        f"🎯 You've been picked for a **blind pick** in **{interaction.guild.name}**! "
        "Reply to this DM with your pick — I'll keep it secret until your opponent answers too."
    )

    # We're about to make network calls (DMing both players), which can take
    # longer than Discord's 3-second response window. Defer now to buy more
    # time, then fill in the real message with edit_original_response once
    # we know the outcome.
    await interaction.response.defer()

    failed_players = []
    for player in (player_one, player_two):
        try:
            await player.send(prompt)
        except discord.Forbidden:
            failed_players.append(player)

    if failed_players:
        names = ", ".join(p.mention for p in failed_players)
        await interaction.edit_original_response(
            content=f"Couldn't DM {names} — they may have DMs disabled for this server. Blind pick cancelled."
        )
        # If the other player's DM *did* go through, let them know it's off.
        for player in (player_one, player_two):
            if player not in failed_players:
                try:
                    await player.send(
                        "The blind pick was cancelled because the other player's DMs are closed."
                    )
                except discord.Forbidden:
                    pass
        return

    session = PickSession(
        player_one=player_one, player_two=player_two, channel_id=interaction.channel_id
    )
    pending_picks[player_one.id] = session
    pending_picks[player_two.id] = session

    await interaction.response.send_message(
        f"🎯 Blind pick started between {player_one.mention} and {player_two.mention} "
        "— waiting for their DM responses..."
    )


@client.event
async def on_message(message: discord.Message):
    if message.author.id == client.user.id:
        return
    if message.guild is not None:
        return  # we only care about DMs here

    session = pending_picks.get(message.author.id)
    if session is None:
        return

    if message.author.id in session.responses:
        await message.channel.send("You've already submitted your pick for this round.")
        return

    session.responses[message.author.id] = message.content
    await message.channel.send(f"Got it — locked in: **{message.content}**")

    if len(session.responses) == 2:
        await reveal(session)


async def reveal(session: PickSession):
    channel = client.get_channel(session.channel_id) or await client.fetch_channel(
        session.channel_id
    )
    p1_pick = session.responses[session.player_one.id]
    p2_pick = session.responses[session.player_two.id]
    await channel.send(
        f"**Blind pick complete.**\n"
        f"**{session.player_one.mention}** selects **{p1_pick}**.\n"
        f"**{session.player_two.mention}** selects **{p2_pick}**."
    )
    del pending_picks[session.player_one.id]
    del pending_picks[session.player_two.id]

@client.tree.command(
    name="rps",
    description="Challenge two players to rock-paper-scissors",
    guild=discord.Object(id=int(GUILD_ID)) if GUILD_ID else None,
)
@app_commands.describe(player_one="First player", player_two="Second player")
async def rps(
    interaction: discord.Interaction,
    player_one: discord.User,
    player_two: discord.User,
):
    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "Pick two different players.", ephemeral=True
        )
        return

    if player_one.id in pending_rps or player_two.id in pending_rps:
        await interaction.response.send_message(
            "One of those players already has a rock-paper-scissors round in progress.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    session = RPSSession(
        player_one=player_one, player_two=player_two, channel_id=interaction.channel_id
    )

    failed_players = []
    for player in (player_one, player_two):
        try:
            await player.send(
                f"🪨📄✂️ You've been challenged to **rock-paper-scissors** in "
                f"**{interaction.guild.name}**! Pick one:",
                view=RPSChoiceView(session, player),
            )
        except discord.Forbidden:
            failed_players.append(player)

    if failed_players:
        names = ", ".join(p.mention for p in failed_players)
        await interaction.edit_original_response(
            content=f"Couldn't DM {names} — they may have DMs disabled for this server. Round cancelled."
        )
        for player in (player_one, player_two):
            if player not in failed_players:
                try:
                    await player.send(
                        "The rock-paper-scissors round was cancelled because the other player's DMs are closed."
                    )
                except discord.Forbidden:
                    pass
        return

    pending_rps[player_one.id] = session
    pending_rps[player_two.id] = session

    await interaction.edit_original_response(
        content=f"🪨📄✂️ Rock-paper-scissors started between {player_one.mention} and "
        f"{player_two.mention} — waiting for their picks..."
    )


def _rps_winner(choice_one: str, choice_two: str) -> str:
    """Returns 'one', 'two', or 'tie'."""
    if choice_one == choice_two:
        return "tie"
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    return "one" if beats[choice_one] == choice_two else "two"


async def reveal_rps(session: RPSSession):
    channel = client.get_channel(session.channel_id) or await client.fetch_channel(
        session.channel_id
    )
    choice_one = session.choices[session.player_one.id]
    choice_two = session.choices[session.player_two.id]
    winner = _rps_winner(choice_one, choice_two)

    if winner == "tie":
        #await channel.send(
            #f"Both **{session.player_one.display_name}** and **{session.player_two.display_name}** "
            #f"picked **{choice_one.capitalize()}** — tie! Going again..."
        #)
        session.choices.clear()

        failed_players = []
        for player in (session.player_one, session.player_two):
            try:
                await player.send(
                    "🪨📄✂️ Tie! Pick again:", view=RPSChoiceView(session, player)
                )
            except discord.Forbidden:
                failed_players.append(player)

        if failed_players:
            names = ", ".join(p.mention for p in failed_players)
            await channel.send(f"Couldn't reach {names} for the rematch — round cancelled.")
            del pending_rps[session.player_one.id]
            del pending_rps[session.player_two.id]
        return

    outcome = (
        f"**{session.player_one.display_name}** wins!"
        if winner == "one"
        else f"**{session.player_two.display_name}** wins!"
    )

    stageclause = (
        f"{session.player_one.mention} strikes two stages. Then, {session.player_two.mention} selects from the remaining two stages."
        if winner == "one"
        else f"{session.player_two.mention} strikes two stages. Then, {session.player_one.mention} selects from the remaining two stages."

    )


    await channel.send(
        f"**{session.player_one.display_name}** picked **{choice_one.capitalize()}**.\n"
        f"**{session.player_two.display_name}** picked **{choice_two.capitalize()}**.\n"
        f"{outcome}\n\n"
        f"{stageclause}\n"
    )

    del pending_rps[session.player_one.id]
    del pending_rps[session.player_two.id]

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(token)