from __future__ import annotations

import asyncio
from io import BytesIO

import discord
from redbot.core import bank, checks, commands

from .casino_core import ACTIVE_GAMES, CONFIG, DEFAULT_GAME_SETTINGS, GAME_LABELS, currency, get_game_settings
from .progression import ACHIEVEMENTS, ACHIEVEMENT_MAP, challenge_definitions, ensure_rotations
from .rewards import FREEBIE_DEFAULTS, freebie_settings, open_panel, status as reward_status
from .ui import BetPanelView, OwnedView

# (lobby button label, emoji, cog name, game key)
LOBBY_GAMES = (
    ("Blackjack", "🃏", "Blackjack", "blackjack"),
    ("Coin Flip", "🪙", "CoinFlip", "coinflip"),
    ("Slots", "🎰", "Slots", "slots"),
    ("Roulette", "🎡", "Roulette", "roulette"),
    ("High/Low", "🂡", "HighLow", "highlow"),
    ("Crash", "🚀", "Crash", "crash"),
    ("Mines", "💣", "Mines", "mines"),
)


class LobbyView(OwnedView):
    """The casino front door: every game, rewards and your stats one tap away."""

    def __init__(self, cog: "Casino", ctx: commands.Context) -> None:
        super().__init__(ctx.author.id, timeout=600)
        self.cog, self.ctx = cog, ctx
        for i, (label, emoji, cog_name, key) in enumerate(LOBBY_GAMES):
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=i // 4)
            btn.callback = self._game(label, emoji, cog_name, key)
            self.add_item(btn)
        for label, emoji, style, cb in (
            ("Roulette Table", "👥", discord.ButtonStyle.secondary, self._table),
            ("Daily Rewards", "🎁", discord.ButtonStyle.success, self._daily),
            ("My Stats", "📊", discord.ButtonStyle.secondary, self._stats),
            ("Challenges", "📋", discord.ButtonStyle.secondary, self._challenges),
            ("Leaderboard", "🏆", discord.ButtonStyle.secondary, self._board),
            ("How to play", "❓", discord.ButtonStyle.secondary, self._odds),
        ):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=2 if label in ("Roulette Table", "Daily Rewards", "My Stats") else 3)
            btn.callback = cb
            self.add_item(btn)

    def _game(self, label: str, emoji: str, cog_name: str, key: str):
        async def go(interaction: discord.Interaction) -> None:
            game_cog = self.cog.bot.get_cog(cog_name)
            if game_cog is None:
                await interaction.response.send_message(f"{label} isn't loaded right now.", ephemeral=True)
                return
            settings = await get_game_settings(self.ctx.guild, key)
            if not settings["enabled"]:
                await interaction.response.send_message(f"{label} is currently disabled.", ephemeral=True)
                return

            async def on_bet(i: discord.Interaction, raw: str) -> None:
                await game_cog.start(self.ctx, raw, interaction=i)

            balance = await bank.get_balance(self.ctx.author)
            limits = f"min {settings['min_bet']:,}" + (f" · max {settings['max_bet']:,}" if settings["max_bet"] else "")
            await interaction.response.send_message(
                f"{emoji} **{label}** — pick your bet (balance **{balance:,}**, {limits}). "
                "This panel stays open so you can keep playing.",
                view=BetPanelView(self.ctx.author.id, on_bet, max_bet=settings["max_bet"]),
                ephemeral=True,
            )
        return go

    async def _table(self, interaction: discord.Interaction) -> None:
        roulette = self.cog.bot.get_cog("Roulette")
        if roulette is None:
            await interaction.response.send_message("Roulette isn't loaded right now.", ephemeral=True)
            return
        await interaction.response.defer()
        await roulette.rtable.callback(roulette, self.ctx)

    async def _daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await open_panel(self.ctx)

    async def _stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.cog.stats_embed(self.ctx.author), ephemeral=True)

    async def _challenges(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.cog.challenges_embed(self.ctx.author), ephemeral=True)

    async def _board(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.cog.board_embed(self.ctx.guild, self.ctx.author), ephemeral=True)

    async def _odds(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.cog.odds_embed(self.ctx), ephemeral=True)


class Casino(commands.Cog):
    """Ruthless Dealer Casino — lobby, rewards, profiles, leaderboards and settings."""

    def __init__(self, bot):
        self.bot = bot

    # ── Lobby ─────────────────────────────────────────────────────────────────

    @commands.group(name="casino", invoke_without_command=True)
    @commands.guild_only()
    async def casino(self, ctx: commands.Context, member: discord.Member = None):
        """Open the casino lobby (or `casino @member` to see someone's stats)."""
        if member is not None and member != ctx.author:
            await ctx.send(embed=await self.stats_embed(member))
            return
        balance = await bank.get_balance(ctx.author)
        cur = await currency(ctx.guild)
        data = await CONFIG.member(ctx.author).all()
        ready = [k for k, v in (await reward_status(ctx.author)).items() if v["ready"]]
        net = data["total_paid"] - data["total_wagered"]
        title = data.get("equipped_title") or "Ruthless Player"
        embed = discord.Embed(
            title="🎰 Ruthless Dealer Casino",
            description=(
                f"Welcome back, **{ctx.author.display_name}** — *{title}*\n"
                f"💰 **{balance:,} {cur}**"
                + (f"\n🎁 **{len(ready)} reward{'s' if len(ready) != 1 else ''} ready** — tap Daily Rewards!" if ready else "")
                + "\n\nPick a game below. Every result has **Again / 2× / ½** buttons so you can keep playing."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Record", value=f"{data['wins']:,}W · {data['losses']:,}L · {data['pushes']:,}P", inline=True)
        embed.add_field(name="Net", value=f"{net:+,}", inline=True)
        embed.add_field(name="Achievements", value=f"{len(data.get('achievements', []))}/{len(ACHIEVEMENTS)}", inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        p = ctx.clean_prefix
        embed.set_footer(text=f"Shortcuts: {p}bj · {p}cf · {p}slots · {p}roulette · {p}hl · {p}crash · {p}mines · {p}daily")
        view = LobbyView(self, ctx)
        view.message = await ctx.send(embed=embed, view=view)

    async def stats_embed(self, member: discord.Member) -> discord.Embed:
        data = await CONFIG.member(member).all()
        net = data["total_paid"] - data["total_wagered"]
        win_rate = data["wins"] / data["total_games"] * 100 if data["total_games"] else 0
        favorite = max(data["games"].items(), key=lambda item: item[1].get("games", 0))[0] if data["games"] else None
        equipped = data.get("equipped_title") or "Ruthless Player"
        embed = discord.Embed(title=f"📊 {member.display_name} — {equipped}", color=discord.Color.gold())
        embed.add_field(name="Record", value=f"Games: **{data['total_games']:,}**\nWins: **{data['wins']:,}**\nLosses: **{data['losses']:,}**\nPushes: **{data['pushes']:,}**\nWin rate: **{win_rate:.1f}%**")
        embed.add_field(name="Economy", value=f"Wagered: **{data['total_wagered']:,}**\nReturned: **{data['total_paid']:,}**\nNet: **{net:+,}**\nBiggest payout: **{data['biggest_payout']:,}**")
        embed.add_field(name="Favourite game", value=f"**{GAME_LABELS.get(favorite, favorite or 'None')}**", inline=False)
        per_game = []
        for game, g in sorted(data["games"].items(), key=lambda kv: kv[1].get("games", 0), reverse=True)[:7]:
            gnet = g.get("paid", 0) - g.get("wagered", 0)
            per_game.append(f"{GAME_LABELS.get(game, game)} — {g.get('games', 0):,} played · {g.get('wins', 0):,}W · {gnet:+,}")
        if per_game:
            embed.add_field(name="By game", value="\n".join(per_game), inline=False)
        embed.add_field(name="Progress", value=f"Achievements: **{len(data.get('achievements', []))}/{len(ACHIEVEMENTS)}**\nBest High/Low streak: **{data.get('best_highlow_streak', 0)}**\nLongest win streak: **{data.get('longest_win_streak', 0)}**", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    @casino.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context, member: discord.Member = None):
        """Show detailed casino stats for you or another member."""
        await ctx.send(embed=await self.stats_embed(member or ctx.author))

    # ── Daily rewards ─────────────────────────────────────────────────────────

    @commands.command(name="daily", aliases=["rewards", "stipend", "scratch", "scratchticket", "bailout", "cooldowns"])
    @commands.guild_only()
    async def daily(self, ctx: commands.Context):
        """Open your Daily Rewards: stipend (with streak bonus), daily spin, scratch ticket and bailout."""
        await open_panel(ctx)

    @casino.command(name="claim", aliases=["pitboss"], hidden=True)
    @commands.guild_only()
    async def emergency_claim(self, ctx: commands.Context):
        """Open your Daily Rewards (the bailout lives there now)."""
        await open_panel(ctx)

    # ── How to play ───────────────────────────────────────────────────────────

    async def odds_embed(self, ctx: commands.Context) -> discord.Embed:
        p = ctx.clean_prefix
        embed = discord.Embed(title="❓ How to play", color=discord.Color.gold(),
                              description=f"Bets accept numbers like `500` or `2.5k`, or `half`, `all`, `max`.\n"
                                          f"Open everything from `{p}casino`.")
        embed.add_field(name="🃏 Blackjack", value=f"`{p}bj <bet>` · blackjack pays **3:2** · dealer stands on 17 · double or split your first two cards", inline=False)
        embed.add_field(name="🪙 Coin Flip", value=f"`{p}cf <bet> [heads|tails]` · win pays **2×**", inline=False)
        embed.add_field(name="🎰 Slots", value=f"`{p}slots <bet>` · three alike **5×–100×** (7️⃣ 100×, 💎 50×, ⭐ 25×) · any pair **1.5×**", inline=False)
        embed.add_field(name="🎡 Roulette", value=f"`{p}roulette <bet> [red|black|odd|even|low|high|1st12|col1|0-36]` · numbers **35:1** · dozens/columns **2:1** · the rest **1:1**\n`{p}rtable` — open a shared table everyone can bet on", inline=False)
        embed.add_field(name="🂡 High/Low", value=f"`{p}hl <bet>` · up to 7 guesses, cash out any time · 1.2× → 4.75×", inline=False)
        embed.add_field(name="🚀 Crash", value=f"`{p}crash <bet> [auto]` · cash out before the rocket crashes · e.g. `{p}crash 500 2` auto-cashes at 2×", inline=False)
        embed.add_field(name="💣 Mines", value=f"`{p}mines <bet> [1-10 mines]` · each gem raises the multiplier · hit a mine and lose", inline=False)
        embed.add_field(name="🎁 Free credits", value=f"`{p}daily` — stipend (streak bonus up to +70%), daily spin, scratch ticket and bailout", inline=False)
        return embed

    @casino.command(name="odds", aliases=["howto", "help", "payouts", "games"])
    @commands.guild_only()
    async def odds(self, ctx: commands.Context):
        """How every game works and what it pays."""
        await ctx.send(embed=await self.odds_embed(ctx))

    async def board_embed(self, guild: discord.Guild, viewer: discord.Member, metric: str = "wagered",
                          game: str = None) -> discord.Embed:
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
            return discord.Embed(description="Category must be: `wagered`, `profit`, `wins`, `losses`, `played`, or `biggestwin`.",
                                 color=discord.Color.red())

        game = game.lower().strip() if game else None
        all_members = await CONFIG.all_members(guild)
        rows = []
        for user_id, data in all_members.items():
            member = guild.get_member(int(user_id))
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
            rows.append((member.display_name, values[metric], values, member.id))

        rows.sort(key=lambda row: row[1], reverse=True)
        title_game = f" — {game.title()}" if game else ""
        display_metric = {"games": "played", "payout": "biggest win"}.get(metric, metric)
        embed = discord.Embed(
            title=f"🎰 Ruthless Dealer Casino Board{title_game}",
            description=f"Top players by **{display_metric}**",
            color=discord.Color.gold(),
        )
        for index, (name, value, values, _) in enumerate(rows[:10], 1):
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
        rank = next((i for i, row in enumerate(rows, 1) if row[3] == viewer.id), None)
        if rank and rank > 10:
            embed.set_footer(text=f"Your rank: #{rank}")
        return embed


    @casino.command(name="achievements", aliases=["ach", "badges"])
    @commands.guild_only()
    async def achievements(self, ctx: commands.Context, member: discord.Member = None):
        """Show all achievements and a member's progress."""
        member = member or ctx.author
        data = await CONFIG.member(member).all()
        unlocked = set(data.get("achievements", []))
        pages = []
        for start in range(0, len(ACHIEVEMENTS), 10):
            lines = []
            for achievement in ACHIEVEMENTS[start:start + 10]:
                value = self._achievement_value(data, achievement["stat"])
                done = achievement["id"] in unlocked
                status = "✅" if done else "⬜"
                title = f" • Title: {achievement['title']}" if achievement.get("title") else ""
                reward = f" • Reward: {achievement['reward']:,}" if achievement.get("reward") else ""
                lines.append(
                    f"{status} {achievement['emoji']} **{achievement['name']}**\n"
                    f"{achievement['description']} `({min(value, achievement['goal']):,}/{achievement['goal']:,})`{reward}{title}"
                )
            pages.append(discord.Embed(
                title=f"🏆 Ruthless Dealer • {member.display_name}'s Achievements ({len(unlocked)}/{len(ACHIEVEMENTS)})",
                description="\n\n".join(lines), color=discord.Color.gold()
            ))
        for embed in pages:
            await ctx.send(embed=embed)

    @staticmethod
    def _achievement_value(data: dict, stat: str) -> int:
        from .progression import stat_value
        return stat_value(data, stat)

    async def challenges_embed(self, member: discord.Member) -> discord.Embed:
        daily_ids, weekly_ids = await ensure_rotations(member.guild)
        data = await CONFIG.member(member).all()
        embed = discord.Embed(title=f"📋 {member.display_name}'s Challenges", color=discord.Color.blurple())
        for label, ids, weekly in (("☀️ Daily", daily_ids, False), ("🗓️ Weekly", weekly_ids, True)):
            state = data.get("weekly_state" if weekly else "daily_state", {})
            progress = state.get("progress", {})
            claimed = set(state.get("claimed", []))
            lines = []
            for definition in challenge_definitions(ids, weekly=weekly):
                current = min(int(progress.get(definition["id"], 0)), definition["goal"])
                status = "✅" if definition["id"] in claimed else "⬜"
                bar_len = 8
                filled = round(current / definition["goal"] * bar_len) if definition["goal"] else bar_len
                bar = "█" * filled + "░" * (bar_len - filled)
                lines.append(f"{status} **{definition['name']}** — {definition['description']}\n`{bar}` {current:,}/{definition['goal']:,} · reward **{definition['reward']:,}**")
            embed.add_field(name=label, value="\n".join(lines) or "No challenges generated.", inline=False)
        embed.set_footer(text="Rewards pay out automatically when you finish. Resets at 00:00 UTC (weekly on Monday).")
        return embed

    @casino.command(name="challenges", aliases=["challenge"])
    @commands.guild_only()
    async def challenges(self, ctx: commands.Context, member: discord.Member = None):
        """Show the current rotating daily and weekly challenges."""
        await ctx.send(embed=await self.challenges_embed(member or ctx.author))

    @casino.group(name="title", aliases=["titles"], invoke_without_command=True)
    @commands.guild_only()
    async def title_group(self, ctx: commands.Context):
        """List unlocked titles or manage the equipped title."""
        data = await CONFIG.member(ctx.author).all()
        unlocked_ids = set(data.get("achievements", []))
        titles = [a["title"] for a in ACHIEVEMENTS if a.get("title") and a["id"] in unlocked_ids]
        equipped = data.get("equipped_title") or "None"
        text = "\n".join(f"`{index}` {title}" for index, title in enumerate(titles, 1)) or "No titles unlocked yet."
        await ctx.send(embed=discord.Embed(title="🎖️ Ruthless Dealer Casino Titles", description=f"**Equipped:** {equipped}\n\n{text}\n\nUse `{ctx.clean_prefix}casino title equip <number>`.", color=discord.Color.gold()))

    @title_group.command(name="equip")
    async def title_equip(self, ctx: commands.Context, selection: int):
        data = await CONFIG.member(ctx.author).all()
        unlocked_ids = set(data.get("achievements", []))
        titles = [a["title"] for a in ACHIEVEMENTS if a.get("title") and a["id"] in unlocked_ids]
        if selection < 1 or selection > len(titles):
            return await ctx.send("Choose a title number from your unlocked title list.")
        chosen = titles[selection - 1]
        await CONFIG.member(ctx.author).equipped_title.set(chosen)
        await ctx.send(f"Ruthless Dealer equipped **{chosen}**.")

    @title_group.command(name="clear")
    async def title_clear(self, ctx: commands.Context):
        await CONFIG.member(ctx.author).equipped_title.set("")
        await ctx.send("Ruthless Dealer cleared your casino title.")

    @casino.command(name="profile")
    @commands.guild_only()
    async def profile_card(self, ctx: commands.Context, member: discord.Member = None):
        """Generate a casino profile card using the member's Discord avatar."""
        member = member or ctx.author
        data = await CONFIG.member(member).all()
        try:
            avatar_bytes = await member.display_avatar.with_size(256).read()
            png = await asyncio.to_thread(_draw_profile_card, avatar_bytes, member.display_name, data)
            await ctx.send(file=discord.File(BytesIO(png), filename="casino-profile.png"))
        except Exception:
            await ctx.send("Ruthless Dealer could not render the profile card, so here are your stats instead.",
                           embed=await self.stats_embed(member))

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

        await ctx.send(embed=await self.board_embed(ctx.guild, ctx.author, metric=category, game=game))

    @casino.command(name="analytics")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def analytics(self, ctx: commands.Context):
        data = await CONFIG.guild(ctx.guild).all()
        house_profit = data["total_wagered"] - data["total_paid"]
        edge = house_profit / data["total_wagered"] * 100 if data["total_wagered"] else 0
        winner = ctx.guild.get_member(data["biggest_payout_user"])
        winner_text = winner.display_name if winner else "Unknown"
        embed = discord.Embed(title="📊 Ruthless Dealer Casino Analytics", color=discord.Color.blurple())
        embed.add_field(name="Volume", value=f"Games: **{data['total_games']:,}**\nWagered: **{data['total_wagered']:,}**\nPaid out: **{data['total_paid']:,}**")
        embed.add_field(name="Ruthless Dealer", value=f"Profit: **{house_profit:+,}**\nObserved edge: **{edge:.2f}%**")
        embed.add_field(name="Results", value=f"Wins: **{data['total_wins']:,}**\nLosses: **{data['total_losses']:,}**\nPushes: **{data['total_pushes']:,}**")
        embed.add_field(name="Largest Return", value=f"**{data['biggest_payout']:,}** to **{winner_text}** via **{data['biggest_payout_game'].title() or 'Unknown'}**", inline=False)
        await ctx.send(embed=embed)

    @casino.command(name="resetprogress", aliases=["freshstart"])
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def resetprogress(self, ctx: commands.Context, confirmation: str = ""):
        """Reset all casino statistics and progression without changing bank balances.

        This also clears casino leaderboards and guild analytics. Free-credit
        cooldowns and casino settings are preserved. Run with CONFIRM to proceed.
        """
        if confirmation.upper() != "CONFIRM":
            return await ctx.send(
                "⚠️ This resets **all casino statistics, achievements, titles, streaks, "
                "challenge progress, leaderboards, and analytics** for this server. "
                "Bank balances, casino settings, and free-credit cooldowns are preserved.\n\n"
                f"Run `{ctx.clean_prefix}casino resetprogress CONFIRM` to continue."
            )

        all_members = await CONFIG.all_members(ctx.guild)
        reset_count = 0
        for user_id in all_members:
            member_conf = CONFIG.member_from_ids(ctx.guild.id, int(user_id))
            async with member_conf.all() as data:
                data.update({
                    "total_wagered": 0,
                    "total_paid": 0,
                    "total_games": 0,
                    "wins": 0,
                    "losses": 0,
                    "pushes": 0,
                    "biggest_bet": 0,
                    "biggest_payout": 0,
                    "games": {},
                    "achievements": [],
                    "equipped_title": "",
                    "current_win_streak": 0,
                    "longest_win_streak": 0,
                    "best_highlow_streak": 0,
                    "daily_completed": 0,
                    "weekly_completed": 0,
                    "daily_state": {},
                    "weekly_state": {},
                })
            reset_count += 1

        guild_conf = CONFIG.guild(ctx.guild)
        async with guild_conf.all() as data:
            data.update({
                "total_wagered": 0,
                "total_paid": 0,
                "total_games": 0,
                "total_wins": 0,
                "total_losses": 0,
                "total_pushes": 0,
                "biggest_payout": 0,
                "biggest_payout_user": 0,
                "biggest_payout_game": "",
            })

        await ctx.send(
            f"✅ Casino progression has been reset for **{reset_count:,}** tracked members. "
            "Everyone now starts at zero; bank balances and free-credit cooldowns were preserved."
        )

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
            payout_cap = setting.get("payout_cap", 0)
            cap_text = f" | Payout cap: {payout_cap:,}" if payout_cap else (" | Payout cap: Unlimited" if "payout_cap" in setting else "")
            lines.append(f"**{game.title()}** — {'Enabled' if setting['enabled'] else 'Disabled'} | Min: {setting['min_bet']:,} | Max: {maximum}{cap_text} | Cooldown: {setting['cooldown']}s")
        free = await freebie_settings(ctx.guild)
        lines.extend([
            "",
            "**Free Credit Systems**",
            f"Daily stipend: {'Enabled' if free['daily_enabled'] else 'Disabled'} | {int(free['daily_min']):,}–{int(free['daily_max']):,} | {int(free['daily_cooldown']) // 3600}h",
            f"Ruthless Dealer: {'Enabled' if free['claim_enabled'] else 'Disabled'} | Below {int(free['claim_threshold']):,} grants {int(free['claim_amount']):,} | {int(free['claim_cooldown']) // 3600}h",
            f"Scratch ticket: {'Enabled' if free['scratch_enabled'] else 'Disabled'} | {int(free['scratch_cooldown']) // 3600}h",
            f"Daily spin: {'Enabled' if free.get('spin_enabled', True) else 'Disabled'}",
            "",
            f"Change with `{ctx.clean_prefix}casino setreward <setting> <value>`",
        ])
        await ctx.send(embed=discord.Embed(title="⚙️ Ruthless Dealer Casino Settings", description="\n".join(lines), color=discord.Color.gold()))

    async def _set_value(self, ctx, game: str, key: str, value):
        game = game.lower()
        if game not in ACTIVE_GAMES:
            return await ctx.send(f"Game must be one of: {', '.join(ACTIVE_GAMES)}")
        async with CONFIG.guild(ctx.guild).games() as games:
            games.setdefault(game, dict(DEFAULT_GAME_SETTINGS[game]))[key] = value
        await ctx.send(f"Updated **{game}** `{key}` to **{value}**.")

    @casino.command(name="setreward")
    @checks.admin_or_permissions(manage_guild=True)
    async def setreward(self, ctx, setting: str = None, value: str = None):
        """Change a free-credit setting. Run with no arguments to list them.

        Times are in hours, toggles take on/off. Example: `casino setreward daily_max 2000`
        """
        if setting is None or setting not in FREEBIE_DEFAULTS:
            names = ", ".join(f"`{k}`" for k in FREEBIE_DEFAULTS)
            return await ctx.send(f"Settings: {names}\nCooldowns are set in hours; `*_enabled` take on/off.")
        default = FREEBIE_DEFAULTS[setting]
        if value is None:
            return await ctx.send(f"Give a value for `{setting}`.")
        if isinstance(default, bool):
            parsed = value.lower() in ("on", "true", "yes", "enable", "enabled", "1")
        else:
            try:
                number = float(value)
            except ValueError:
                return await ctx.send("That needs to be a number.")
            if number < 0:
                return await ctx.send("Values can't be negative.")
            parsed = int(number * 3600) if setting.endswith("_cooldown") else int(number)
        async with CONFIG.guild(ctx.guild).freebies() as free:
            free[setting] = parsed
            if setting in ("daily_min", "daily_max") and free.get("daily_min", 0) > free.get("daily_max", 0):
                free["daily_max"] = free["daily_min"]
        shown = f"{parsed // 3600}h" if setting.endswith("_cooldown") else parsed
        await ctx.send(f"Updated `{setting}` to **{shown}**.")

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


    @casino.command(name="setpayoutcap", aliases=["setcap"])
    @checks.admin_or_permissions(manage_guild=True)
    async def setpayoutcap(self, ctx, game: str, amount: int):
        game = game.lower()
        if game not in ACTIVE_GAMES:
            return await ctx.send(f"Game must be one of: {', '.join(ACTIVE_GAMES)}")
        if "payout_cap" not in DEFAULT_GAME_SETTINGS[game]:
            return await ctx.send(f"**{game.title()}** does not use a configurable payout cap.")
        if amount < 0:
            return await ctx.send("Payout cap cannot be negative. Use 0 for unlimited.")
        await self._set_value(ctx, game, "payout_cap", amount)

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



def _draw_profile_card(avatar_bytes: bytes, display_name: str, data: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    avatar = ImageOps.fit(Image.open(BytesIO(avatar_bytes)).convert("RGB"), (180, 180))
    card = Image.new("RGB", (1000, 420), (25, 27, 34))
    draw = ImageDraw.Draw(card)
    try:
        font_big = ImageFont.truetype("DejaVuSans.ttf", 34)
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font_big = font = ImageFont.load_default()
    card.paste(avatar, (35, 45))
    draw.text((245, 45), display_name, fill="white", font=font_big)
    draw.text((245, 92), data.get("equipped_title") or "Ruthless Player", fill=(255, 207, 64), font=font)
    net = data.get("total_paid", 0) - data.get("total_wagered", 0)
    stats = [
        f"Games Played: {data.get('total_games', 0):,}",
        f"Wins: {data.get('wins', 0):,}",
        f"Total Wagered: {data.get('total_wagered', 0):,}",
        f"Net: {net:+,}",
        f"Achievements: {len(data.get('achievements', []))}/{len(ACHIEVEMENTS)}",
        f"Longest Win Streak: {data.get('longest_win_streak', 0):,}",
        f"Best High/Low Streak: {data.get('best_highlow_streak', 0):,}",
    ]
    for i, line in enumerate(stats):
        x = 245 if i < 4 else 600
        y = 145 + (i if i < 4 else i - 4) * 44
        draw.text((x, y), line, fill=(225, 225, 230), font=font)
    unlocked = [ACHIEVEMENT_MAP[x]["name"] for x in data.get("achievements", []) if x in ACHIEVEMENT_MAP]
    draw.text((35, 260), "Achievements", fill=(255, 207, 64), font=font)
    draw.text((35, 310), ", ".join(unlocked[:5]) or "None yet", fill="white", font=font)
    out = BytesIO()
    card.save(out, format="PNG")
    return out.getvalue()
