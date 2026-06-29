import os
import random
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

CHARACTERS_FILE = os.path.join(os.path.dirname(__file__), "characters.txt")
CHARACTERS_PAGE_SIZE = 25
STAGES = [
    "Dream Land",
    "Goomba Road",
    "Pokemon Stadium",
    "Glacial River (Remix)",
]

def load_characters() -> list[str]:
    try:
        with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        raise RuntimeError(
            f"Character roster file not found: {CHARACTERS_FILE}. "
            "Create characters.txt with one character per line."
        )

CHARACTERS = load_characters()

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
    stage_strike: bool = False


# Same idea as pending_picks, but for active rock-paper-scissors rounds.
pending_rps: dict[int, RPSSession] = {}

@dataclass
class StrikeSession:
    winner: discord.User
    loser: discord.User
    channel_id: int
    banned_stages: list[str] = field(default_factory=list)
    selected_stage: Optional[str] = None

pending_strikes: dict[int, StrikeSession] = {}


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


class BlindPickSelect(discord.ui.Select):
    def __init__(self, view: "BlindPickChoiceView", options: list[str], page: int, total_pages: int):
        placeholder = f"Choose your character (page {page + 1}/{total_pages})"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=char, value=char) for char in options],
        )
        self.view_reference = view

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        self.view_reference.session.responses[self.view_reference.player.id] = choice
        for child in self.view_reference.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"You selected **{choice}**. Waiting for your opponent...",
            view=self.view_reference,
        )
        if len(self.view_reference.session.responses) == 2:
            await reveal(self.view_reference.session)


class BlindPickChoiceView(discord.ui.View):
    """Paged select menu sent in a player's DM to choose a starting character."""

    class PreviousPage(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Previous Page (more characters)", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction):
            view: BlindPickChoiceView = self.view  # type: ignore
            view.page = max(0, view.page - 1)
            view._refresh_options()
            await interaction.response.edit_message(view=view)

    class NextPage(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Next Page (more characters)", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction):
            view: BlindPickChoiceView = self.view  # type: ignore
            view.page += 1
            view._refresh_options()
            await interaction.response.edit_message(view=view)

    def __init__(self, session: PickSession, player: discord.User, page: int = 0):
        super().__init__(timeout=None)
        self.session = session
        self.player = player
        self.page = page
        self._refresh_options()

    def _refresh_options(self) -> None:
        start = self.page * CHARACTERS_PAGE_SIZE
        end = start + CHARACTERS_PAGE_SIZE
        page_chars = CHARACTERS[start:end]
        total_pages = (len(CHARACTERS) + CHARACTERS_PAGE_SIZE - 1) // CHARACTERS_PAGE_SIZE

        self.clear_items()
        self.add_item(BlindPickSelect(self, page_chars, self.page, total_pages))

        if self.page > 0:
            self.add_item(self.PreviousPage())
        if end < len(CHARACTERS):
            self.add_item(self.NextPage())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "This isn't your character selection to make.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await interaction.response.send_message("Something went wrong with the roster menu.", ephemeral=True)


class StageStrikeButton(discord.ui.Button):
    def __init__(self, stage: str):
        super().__init__(label=stage, style=discord.ButtonStyle.primary)
        self.stage = stage

    async def callback(self, interaction: discord.Interaction) -> None:
        view: StageStrikeView = self.view  # type: ignore
        session = view.session
        stage = self.stage
        session.banned_stages.append(stage)

        if len(session.banned_stages) == 1:
            view._refresh_buttons()
            await interaction.response.edit_message(
                content=(
                    f"You banned **{stage}** as your first strike. "
                    "Choose your second strike from the remaining stages."
                ),
                view=view,
            )
            return

        # Second strike completed.
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"You banned **{session.banned_stages[0]}** and **{session.banned_stages[1]}**. "
                "Waiting for your opponent to pick from the remaining stages..."
            ),
            view=view,
        )

        remaining = [stage for stage in STAGES if stage not in session.banned_stages]
        loser = session.loser
        try:
            await loser.send(
                "Your opponent has banned two stages. Choose the **starting stage** from the remaining options:",
                view=StageChooseView(session, remaining),
            )
        except discord.Forbidden:
            channel = session.channel_id
            channel_obj = client.get_channel(channel) or await client.fetch_channel(channel)
            await channel_obj.send(
                "Could not DM the loser to choose the starting stage. Stage strike cancelled."
            )
            del pending_strikes[session.winner.id]
            del pending_strikes[session.loser.id]


class StageStrikeView(discord.ui.View):
    def __init__(self, session: StrikeSession, player: discord.User):
        super().__init__(timeout=None)
        self.session = session
        self.player = player
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.clear_items()
        remaining = [stage for stage in STAGES if stage not in self.session.banned_stages]
        for stage in remaining:
            self.add_item(StageStrikeButton(stage))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "This isn't your stage ban to make.", ephemeral=True
            )
            return False
        return True


class StageChooseView(discord.ui.View):
    def __init__(self, session: StrikeSession, options: list[str]):
        super().__init__(timeout=None)
        self.session = session
        for stage in options:
            self.add_item(StageChooseButton(stage))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.loser.id:
            await interaction.response.send_message(
                "This isn't your stage to choose.", ephemeral=True
            )
            return False
        return True


class StageChooseButton(discord.ui.Button):
    def __init__(self, stage: str):
        super().__init__(label=stage, style=discord.ButtonStyle.primary)
        self.stage = stage

    async def callback(self, interaction: discord.Interaction) -> None:
        view: StageChooseView = self.view  # type: ignore
        session = view.session
        session.selected_stage = self.stage
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"You chose **{self.stage}** as the starting stage.",
            view=view,
        )

        channel = session.channel_id
        channel_obj = client.get_channel(channel) or await client.fetch_channel(channel)
        await channel_obj.send(
            f"🎮 Stage strike complete! {session.winner.mention} banned **{session.banned_stages[0]}** and **{session.banned_stages[1]}**. "
            f"{session.loser.mention} picked **{self.stage}** as the starting stage."
        )
        del pending_strikes[session.winner.id]
        del pending_strikes[session.loser.id]


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
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    guild_synced = []
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            guild_synced = await client.tree.sync(guild=guild)
    except discord.Forbidden:
        print(
            f"Warning: could not sync guild commands to GUILD_ID={GUILD_ID}. "
            "The bot may not be in that server or may lack access."
        )
    except Exception as exc:
        print(f"Warning: failed to sync guild commands to GUILD_ID={GUILD_ID}: {exc}")

    try:
        global_synced = await client.tree.sync()
        if guild_synced:
            print(f"Synced {len(guild_synced)} guild command(s) and {len(global_synced)} global command(s)")
        else:
            print(f"Synced {len(global_synced)} global command(s)")
    except Exception as exc:
        print(f"Error syncing global commands: {exc}")

    invite_url = (
        f"https://discord.com/oauth2/authorize?client_id={client.user.id}"
        f"&permissions=2048&scope=bot%20applications.commands"
    )
    print(f"Invite URL: {invite_url}")


@client.tree.command(
    name="blindpick",
    description="Privately ask your opponent to make a pick",
)
@app_commands.describe(opponent="Opponent for the blind pick")
async def blindpick(
    interaction: discord.Interaction,
    opponent: discord.User,
):
    player_one = interaction.user
    player_two = opponent

    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "You can't blind pick against yourself.", ephemeral=True
        )
        return
    
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not a DM.", ephemeral=True)
        return


    if player_one.id in pending_picks or player_two.id in pending_picks:
        await interaction.response.send_message(
            "One of you already has a blind pick in progress. "
            "Wait for it to finish first.",
            ephemeral=True,
        )
        return

    prompt = (
        f"🎯 You've been picked for a **blind pick** in **{interaction.guild.name}**! "
        "Select your character from the menu in this DM — I'll keep it secret until your opponent answers too.\n"
        "If you don't see your character, use the Next/Previous buttons to see more options."
    )

    # We're about to make network calls (DMing both players), which can take
    # longer than Discord's 3-second response window. Defer now to buy more
    # time, then fill in the real message with edit_original_response once
    # we know the outcome.
    await interaction.response.defer()

    session = PickSession(
        player_one=player_one, player_two=player_two, channel_id=interaction.channel_id
    )

    failed_players = []
    for player in (player_one, player_two):
        try:
            await player.send(prompt, view=BlindPickChoiceView(session, player))
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

    pending_picks[player_one.id] = session
    pending_picks[player_two.id] = session

    await interaction.edit_original_response(
        content=f"🎯 Blind pick started between {player_one.mention} and {player_two.mention} "
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

    await message.channel.send(
        "Please choose your character using the menu I sent. "
        "Free-text replies are no longer accepted."
    )
    return


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

    for player, own_pick, opponent_pick in (
        (session.player_one, p1_pick, p2_pick),
        (session.player_two, p2_pick, p1_pick),
    ):
        try:
            await player.send(
                f"Your blind pick is complete. You chose **{own_pick}** and your opponent chose **{opponent_pick}**."
            )
        except discord.Forbidden:
            pass

    del pending_picks[session.player_one.id]
    del pending_picks[session.player_two.id]

@client.tree.command(
    name="rps",
    description="Challenge your opponent to rock-paper-scissors",
)
@app_commands.describe(opponent="Opponent for the rock-paper-scissors challenge")
async def rps(
    interaction: discord.Interaction,
    opponent: discord.User,
):
    player_one = interaction.user
    player_two = opponent

    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "You can't challenge yourself.", ephemeral=True
        )
        return

    if player_one.id in pending_rps or player_two.id in pending_rps:
        await interaction.response.send_message(
            "One of you already has a rock-paper-scissors round in progress.",
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


@client.tree.command(
    name="strikerps",
    description="Play rock-paper-scissors; winner bans two stages and loser picks the starting stage",
)
@app_commands.describe(opponent="Opponent for the strikerps match")
async def strikerps(
    interaction: discord.Interaction,
    opponent: discord.User,
):
    player_one = interaction.user
    player_two = opponent

    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "You can't challenge yourself.", ephemeral=True
        )
        return

    if (
        player_one.id in pending_rps
        or player_two.id in pending_rps
        or player_one.id in pending_strikes
        or player_two.id in pending_strikes
    ):
        await interaction.response.send_message(
            "One of you already has a match or strike in progress.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    session = RPSSession(
        player_one=player_one,
        player_two=player_two,
        channel_id=interaction.channel_id,
        stage_strike=True,
    )

    failed_players = []
    for player in (player_one, player_two):
        try:
            await player.send(
                f"🪨📄✂️ You've been challenged to **rock-paper-scissors** in **{interaction.guild.name}**! "
                "The winner will ban two stages, and the loser will choose the starting stage.",
                view=RPSChoiceView(session, player),
            )
        except discord.Forbidden:
            failed_players.append(player)

    if failed_players:
        names = ", ".join(p.mention for p in failed_players)
        await interaction.edit_original_response(
            content=f"Couldn't DM {names} — they may have DMs disabled for this server. Rock-paper-scissors cancelled."
        )
        for player in (player_one, player_two):
            if player not in failed_players:
                try:
                    await player.send(
                        "The rock-paper-scissors match was cancelled because the other player's DMs are closed."
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


@client.tree.command(
    name="coinflip",
    description="Randomly choose who wins and who strikes first",
)
@app_commands.describe(opponent="Opponent for the coinflip")
async def coinflip(
    interaction: discord.Interaction,
    opponent: discord.User,
):
    player_one = interaction.user
    player_two = opponent

    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "You can't coinflip against yourself.", ephemeral=True
        )
        return

    winner = random.choice((player_one, player_two))
    loser = player_two if winner is player_one else player_one

    await interaction.response.send_message(
        f"🪙 Coinflip result:\n"
        f"{winner.mention} wins and will strike two stages.\n"
        f"{loser.mention} will then pick from the remaining two stages to begin the set.\n\n"
        f"**Legal Stages: Dream Land, Goomba Road, Pokemon Stadium, Glacial River (Remix).**"
    )


@client.tree.command(
    name="strike",
    description="Randomly select a player via coinflip: winner bans two stages, loser picks the starting stage",
)
@app_commands.describe(opponent="Opponent for the stage strike")
async def strike(
    interaction: discord.Interaction,
    opponent: discord.User,
):
    player_one = interaction.user
    player_two = opponent

    if player_one.id == player_two.id:
        await interaction.response.send_message(
            "You can't strike against yourself.", ephemeral=True
        )
        return

    if player_one.id in pending_strikes or player_two.id in pending_strikes:
        await interaction.response.send_message(
            "One of you already has a stage strike in progress.", ephemeral=True
        )
        return

    winner = random.choice((player_one, player_two))
    loser = player_two if winner is player_one else player_one
    session = StrikeSession(
        winner=winner,
        loser=loser,
        channel_id=interaction.channel_id,
    )
    pending_strikes[winner.id] = session
    pending_strikes[loser.id] = session

    await interaction.response.send_message(
        f"🪙 {winner.mention} wins the coinflip and will ban two stages. "
        f"{loser.mention} will pick from the remaining stages.",
    )

    try:
        await winner.send(
            "You won the strike. Ban two stages from the list below:",
            view=StageStrikeView(session, winner),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Couldn't DM the winner for stage banning. Strike cancelled.", ephemeral=True
        )
        del pending_strikes[winner.id]
        del pending_strikes[loser.id]


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
        f"{session.player_one.mention} strikes two stages. Then, {session.player_two.mention} will pick from the remaining two stages to begin the set."
        if winner == "one"
        else f"{session.player_two.mention} strikes two stages. Then, {session.player_one.mention} will pick from the remaining two stages to begin the set."
    )

    if session.stage_strike:
        winner_user = session.player_one if winner == "one" else session.player_two
        loser_user = session.player_two if winner == "one" else session.player_one
        strike_session = StrikeSession(
            winner=winner_user,
            loser=loser_user,
            channel_id=session.channel_id,
        )
        pending_strikes[winner_user.id] = strike_session
        pending_strikes[loser_user.id] = strike_session

        await channel.send(
            f"**{session.player_one.display_name}** picked **{choice_one.capitalize()}**.\n"
            f"**{session.player_two.display_name}** picked **{choice_two.capitalize()}**.\n"
            f"{outcome}\n\n"
            #f"{stageclause}\n\n"
            f"The winner will now ban two stages via DM."
        )

        try:
            await loser_user.send(
                f"{outcome} Your opponent won strikerps and will ban two stages. "
                "You'll choose the starting stage from the remaining stages once they finish."
            )
        except discord.Forbidden:
            pass

        try:
            await winner_user.send(
                "You won rock-paper-scissors. Ban two stages from the list below:",
                view=StageStrikeView(strike_session, winner_user),
            )
        except discord.Forbidden:
            await channel.send(
                "Could not DM the winner for stage banning. Stage strike cancelled."
            )
            del pending_strikes[winner_user.id]
            del pending_strikes[loser_user.id]
    else:
        await channel.send(
            f"**{session.player_one.display_name}** picked **{choice_one.capitalize()}**.\n"
            f"**{session.player_two.display_name}** picked **{choice_two.capitalize()}**.\n"
            f"{outcome}\n\n"
            f"{stageclause}\n\n"
            f"**Legal Stages: Dream Land, Goomba Road, Pokemon Stadium, Glacial River (Remix).**"
        )

    if not session.stage_strike:
        for player, own_choice, opponent_choice in (
            (session.player_one, choice_one, choice_two),
            (session.player_two, choice_two, choice_one),
        ):
            try:
                await player.send(
                    f"Your RPS round is complete. You chose **{own_choice.capitalize()}** and your opponent chose **{opponent_choice.capitalize()}**. "
                    f"{outcome}"
                )
            except discord.Forbidden:
                pass

    del pending_rps[session.player_one.id]
    del pending_rps[session.player_two.id]

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(token)