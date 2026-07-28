from __future__ import annotations

import discord
from redbot.core import bank, checks, commands

from .casino_core import ACTIVE_GAMES, CONFIG, DEFAULT_GAME_SETTINGS


class Casino(commands.Cog):
    """Unified casino profiles, leaderboards, analytics, and settings."""

    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="casino", invoke_without_command=True)
    @commands.guild_only()
    async def casino(self, ctx: commands.Context, member: discord.Member = None):
        """Show a unified casino profile or manage the casino."""
        member = member or ctx.author
        data = await CONFIG.member(member).all()
        net = data["total_paid"] - data["total_wagered"]
        win_rate = data["wins"] / data["total_games"] * 100 if data["total_games"] else 0
        favorite = max(data["games"].items(), key=lambda item: item[1].get("games", 0))[0].title() if data["games"] else "None"
        embed = discord.Embed(title=f"🎰 {member.display_name}'s Casino Profile", color=discord.Color.gold())
        embed.add_field(name="Record", value=f"Games: **{data['total_games']:,}**\nWins: **{data['wins']:,}**\nLosses: **{data['losses']:,}**\nPushes: **{data['pushes']:,}**\nWin rate: **{win_rate:.1f}%**")
        embed.add_field(name="Economy", value=f"Wagered: **{data['total_wagered']:,}**\nReturned: **{data['total_paid']:,}**\nNet: **{net:+,}**\nBiggest payout: **{data['biggest_payout']:,}**")
        embed.add_field(name="Favorite Game", value=f"**{favorite}**", inline=False)
        embed.set_footer(text="Unified tracking begins when this update is installed; legacy per-game stats remain available.")
        await ctx.send(embed=embed)

    async def _send_casinoboard(
        self, ctx: commands.Context, metric: str = "wagered", game: str = None
    ):
        aliases = {
            "bet": "wagered",
            "bets": "wagered",
            "played": "games",
            "win": "wins",
            "loss": "losses",
            "biggest": "payout",
            "biggestwin": "payout",
        }
        metric = aliases.get(metric.lower(), metric.lower())
        if metric not in {"wagered", "profit", "games", "wins", "losses", "payout"}:
            return await ctx.send(
                "Category must be: `wagered`, `profit`, `wins`, `losses`, "
                "`played`, or `biggestwin`."
            )

        game = game.lower().strip() if game else None
        all_members = await CONFIG.all_members(ctx.guild)
        rows = []
        for user_id, data in all_members.items():
            member = ctx.guild.get_member(int(user_id))
            if not member or member.bot:
                continue

            source = data
            if game:
                source = data.get("games", {}).get(game)
                if not source:
                    continue

            if game:
                wagered = source.get("wagered", 0)
                paid = source.get("paid", 0)
                values = {
                    "wagered": wagered,
                    "profit": paid - wagered,
                    "games": source.get("games", 0),
                    "wins": source.get("wins", 0),
                    "losses": source.get("losses", 0),
                    "payout": source.get("biggest_payout", 0),
                }
            else:
                wagered = data.get("total_wagered", 0)
                paid = data.get("total_paid", 0)
                values = {
                    "wagered": wagered,
                    "profit": paid - wagered,
                    "games": data.get("total_games", 0),
                    "wins": data.get("wins", 0),
                    "losses": data.get("losses", 0),
                    "payout": data.get("biggest_payout", 0),
                }
            if not values["games"]:
                continue
            rows.append((member.display_name, values[metric], values))

        rows.sort(key=lambda row: row[1], reverse=True)
        title_game = f" — {game.title()}" if game else ""
        display_metric = {"games": "played", "payout": "biggest win"}.get(metric, metric)
        embed = discord.Embed(
            title=f"🎰 Casino Board{title_game}",
            description=f"Top players by **{display_metric}**",
            color=discord.Color.gold(),
        )
        for index, (name, value, values) in enumerate(rows[:10], 1):
            record = (
                f"Wins: **{values['wins']:,}** | "
                f"Losses: **{values['losses']:,}** | "
                f"Played: **{values['games']:,}**"
            )
            display_value = f"{value:+,}" if metric == "profit" else f"{value:,}"
            embed.add_field(
                name=f"#{index} — {name}",
                value=f"{display_metric.title()}: **{display_value}**\n{record}",
                inline=False,
            )
        if not rows:
            embed.description = "No matching casino games have been recorded yet."
        await ctx.send(embed=embed)

    @commands.command(name="casinoboard", aliases=["casinoleaderboard"])
    @commands.guild_only()
    async def casinoboard(
        self, ctx: commands.Context, category: str = "wagered", game: str = None
    ):
        """Show the overall casino board or filter it by category and game.

        Categories: wagered, profit, wins, losses, played, biggestwin.
        A game may be supplied by itself or after a category.
        """
        category = category.lower().strip()

        # `[p]casinoboard roulette` means the wagered board for Roulette.
        known_games = set(ACTIVE_GAMES) | {"dailyspin"}
        if category in known_games and game is None:
            game = category
            category = "wagered"

        if game:
            game = game.lower().strip()
            if game not in known_games:
                return await ctx.send(
                    f"Game must be one of: {', '.join(sorted(known_games))}."
                )

        await self._send_casinoboard(ctx, metric=category, game=game)

    @casino.command(name="analytics")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def analytics(self, ctx: commands.Context):
        data = await CONFIG.guild(ctx.guild).all()
        house_profit = data["total_wagered"] - data["total_paid"]
        edge = house_profit / data["total_wagered"] * 100 if data["total_wagered"] else 0
        winner = ctx.guild.get_member(data["biggest_payout_user"])
        winner_text = winner.display_name if winner else "Unknown"
        embed = discord.Embed(title="📊 Casino Analytics", color=discord.Color.blurple())
        embed.add_field(name="Volume", value=f"Games: **{data['total_games']:,}**\nWagered: **{data['total_wagered']:,}**\nPaid out: **{data['total_paid']:,}**")
        embed.add_field(name="House", value=f"Profit: **{house_profit:+,}**\nObserved edge: **{edge:.2f}%**")
        embed.add_field(name="Results", value=f"Wins: **{data['total_wins']:,}**\nLosses: **{data['total_losses']:,}**\nPushes: **{data['total_pushes']:,}**")
        embed.add_field(name="Largest Return", value=f"**{data['biggest_payout']:,}** to **{winner_text}** via **{data['biggest_payout_game'].title() or 'Unknown'}**", inline=False)
        await ctx.send(embed=embed)

    @casino.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def settings(self, ctx: commands.Context):
        games = await CONFIG.guild(ctx.guild).games()
        lines = []
        for game in ACTIVE_GAMES:
            setting = dict(DEFAULT_GAME_SETTINGS[game])
            setting.update(games.get(game, {}))
            maximum = f"{setting['max_bet']:,}" if setting["max_bet"] else "Unlimited"
            lines.append(f"**{game.title()}** — {'Enabled' if setting['enabled'] else 'Disabled'} | Min: {setting['min_bet']:,} | Max: {maximum} | Cooldown: {setting['cooldown']}s")
        await ctx.send(embed=discord.Embed(title="⚙️ Casino Settings", description="\n".join(lines), color=discord.Color.gold()))

    async def _set_value(self, ctx, game: str, key: str, value):
        game = game.lower()
        if game not in ACTIVE_GAMES:
            return await ctx.send(f"Game must be one of: {', '.join(ACTIVE_GAMES)}")
        async with CONFIG.guild(ctx.guild).games() as games:
            games.setdefault(game, dict(DEFAULT_GAME_SETTINGS[game]))[key] = value
        await ctx.send(f"Updated **{game}** `{key}` to **{value}**.")

    @casino.command(name="setmin")
    @checks.admin_or_permissions(manage_guild=True)
    async def setmin(self, ctx, game: str, amount: int):
        if amount < 1:
            return await ctx.send("Minimum bet must be at least 1.")
        await self._set_value(ctx, game, "min_bet", amount)

    @casino.command(name="setmax")
    @checks.admin_or_permissions(manage_guild=True)
    async def setmax(self, ctx, game: str, amount: int):
        if amount < 0:
            return await ctx.send("Maximum bet cannot be negative. Use 0 for unlimited.")
        await self._set_value(ctx, game, "max_bet", amount)

    @casino.command(name="setcooldown")
    @checks.admin_or_permissions(manage_guild=True)
    async def setcooldown(self, ctx, game: str, seconds: int):
        if not 0 <= seconds <= 3600:
            return await ctx.send("Cooldown must be between 0 and 3600 seconds.")
        await self._set_value(ctx, game, "cooldown", seconds)

    @casino.command(name="enable")
    @checks.admin_or_permissions(manage_guild=True)
    async def enable(self, ctx, game: str):
        await self._set_value(ctx, game, "enabled", True)

    @casino.command(name="disable")
    @checks.admin_or_permissions(manage_guild=True)
    async def disable(self, ctx, game: str):
        await self._set_value(ctx, game, "enabled", False)

