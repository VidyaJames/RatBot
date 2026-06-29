# RatBot

A Discord bot for Smash Remix tournament use.

## Features

- `/blindpick opponent:<user>` — Starts a private blind pick. Both players receive a DM with a paged roster menu, and the bot reveals both starting characters only after both picks are made.
- `/rps opponent:<user>` — Starts a private rock-paper-scissors match. Each player selects rock, paper, or scissors in DMs, and the bot reports the result privately and publicly.
- `/strike opponent:<user>` — Randomly determines a winner via coinflip. The winner bans two stages by DM buttons, then the loser selects the starting stage from the remaining options.
- `/strikerps opponent:<user>` — Plays an RPS match in DMs to decide who strikes first. The winner bans two stages by DM buttons, and the loser then chooses the starting stage.
- `/coinflip opponent:<user>` — Randomly chooses a winner, who bans two stages by DM buttons, while the loser then picks the starting stage.