from __future__ import annotations

from io import BytesIO
import random
import time

import discord
from redbot.core import bank, checks, commands

from .casino_core import ACTIVE_GAMES, CONFIG, DEFAULT_GAME_SETTINGS, safe_deposit
from .progression import ACHIEVEMENTS, ACHIEVEMENT_MAP, challenge_definitions, ensure_rotations


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
        equipped = data.get("equipped_title") or "Ruthless Player"
        embed = discord.Embed(title=f"🎰 Ruthless Dealer Casino • {member.display_name} — {equipped}", color=discord.Color.gold())
        embed.add_field(name="Record", value=f"Games: **{data['total_games']:,}**\nWins: **{data['wins']:,}**\nLosses: **{data['losses']:,}**\nPushes: **{data['pushes']:,}**\nWin rate: **{win_rate:.1f}%**")
        embed.add_field(name="Economy", value=f"Wagered: **{data['total_wagered']:,}**\nReturned: **{data['total_paid']:,}**\nNet: **{net:+,}**\nBiggest payout: **{data['biggest_payout']:,}**")
        embed.add_field(name="Favorite Game", value=f"**{favorite}**", inline=False)
        embed.add_field(name="Progress", value=f"Achievements: **{len(data.get('achievements', []))}/20**\nBest High/Low streak: **{data.get('best_highlow_streak', 0)}**\nLongest win streak: **{data.get('longest_win_streak', 0)}**", inline=False)
        if member == ctx.author:
            free = await self._freebie_settings(ctx.guild)
            now = time.time()
            statuses = []
            for label, key, cooldown in (
                ("Daily Stipend", "daily_stipend_at", free["daily_cooldown"]),
                ("Scratch Ticket", "scratch_claimed_at", free["scratch_cooldown"]),
            ):
                ready = now - float(data.get(key, 0)) >= int(cooldown)
                statuses.append(f"{'✅' if ready else '⏳'} {label}")
            balance = await bank.get_balance(member)
            claim_ready = balance < int(free["claim_threshold"]) and now - float(data.get("bailout_claimed_at", 0)) >= int(free["claim_cooldown"])
            statuses.append(f"{'✅' if claim_ready else '⏳'} Ruthless Dealer Bailout")
            embed.add_field(name="Free Credits", value="\n".join(statuses), inline=False)
        embed.set_footer(text="Unified tracking begins when this update is installed; legacy per-game stats remain available.")
        await ctx.send(embed=embed)



    @staticmethod
    def _cooldown_text(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    @staticmethod
    async def _freebie_settings(guild: discord.Guild) -> dict:
        defaults = {
            "daily_enabled": True, "daily_min": 750, "daily_max": 1250,
            "daily_cooldown": 86400, "claim_enabled": True,
            "claim_threshold": 250, "claim_amount": 500,
            "claim_cooldown": 43200, "scratch_enabled": True,
            "scratch_cooldown": 86400,
        }
        stored = await CONFIG.guild(guild).freebies()
        defaults.update(stored or {})
        return defaults

    @commands.command(name="daily", aliases=["stipend"])
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def daily_stipend(self, ctx: commands.Context):
        """Collect a modest daily casino stipend."""
        settings = await self._freebie_settings(ctx.guild)
        if not settings["daily_enabled"]:
            return await ctx.send("Ruthless Dealer’s daily stipend is currently disabled.")
        now = time.time()
        last = float(await CONFIG.member(ctx.author).daily_stipend_at())
        remaining = int(settings["daily_cooldown"] - (now - last))
        if remaining > 0:
            return await ctx.send(f"Ruthless Dealer already paid your stipend. Try again in **{self._cooldown_text(remaining)}**.")
        low = max(0, int(settings["daily_min"]))
        high = max(low, int(settings["daily_max"]))
        requested = random.randint(low, high)
        deposited = await safe_deposit(ctx.author, requested)
        await CONFIG.member(ctx.author).daily_stipend_at.set(now)
        currency = await bank.get_currency_name(ctx.guild)
        if deposited < requested:
            return await ctx.send(f"🎁 **Ruthless Dealer Daily Stipend**\nYou collected **{deposited:,} {currency}** before reaching the bank limit.")
        await ctx.send(f"🎁 **Ruthless Dealer Daily Stipend**\nYou collected **{deposited:,} {currency}**. Come back tomorrow for another stipend!")

    @casino.command(name="claim", aliases=["bailout", "pitboss"])
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def emergency_claim(self, ctx: commands.Context):
        """Claim a small emergency bailout when your balance is very low."""
        settings = await self._freebie_settings(ctx.guild)
        if not settings["claim_enabled"]:
            return await ctx.send("Ruthless Dealer bailouts are currently disabled.")
        balance = await bank.get_balance(ctx.author)
        threshold = max(0, int(settings["claim_threshold"]))
        if balance >= threshold:
            currency = await bank.get_currency_name(ctx.guild)
            return await ctx.send(f"Ruthless Dealer only helps players below **{threshold:,} {currency}**. Your balance is **{balance:,}**.")
        now = time.time()
        last = float(await CONFIG.member(ctx.author).bailout_claimed_at())
        remaining = int(settings["claim_cooldown"] - (now - last))
        if remaining > 0:
            return await ctx.send(f"Ruthless Dealer already helped you recently. Try again in **{self._cooldown_text(remaining)}**.")
        requested = max(0, int(settings["claim_amount"]))
        deposited = await safe_deposit(ctx.author, requested)
        await CONFIG.member(ctx.author).bailout_claimed_at.set(now)
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(f"🎩 **Ruthless Dealer Favor**\nRuthless Dealer slides you **{deposited:,} {currency}** in chips.\n*“Good luck... you'll need it.”*")

    @commands.command(name="scratch", aliases=["scratchticket"])
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def scratch_ticket(self, ctx: commands.Context):
        """Use one conservative free scratch ticket each day."""
        settings = await self._freebie_settings(ctx.guild)
        if not settings["scratch_enabled"]:
            return await ctx.send("Ruthless Dealer’s daily scratch tickets are currently disabled.")
        now = time.time()
        last = float(await CONFIG.member(ctx.author).scratch_claimed_at())
        remaining = int(settings["scratch_cooldown"] - (now - last))
        if remaining > 0:
            return await ctx.send(f"Ruthless Dealer already gave you today’s scratch ticket. Try again in **{self._cooldown_text(remaining)}**.")
        prizes = [0, 250, 500, 1000, 2500, 5000, 10000]
        weights = [3500, 3200, 2000, 900, 300, 90, 10]
        requested = random.choices(prizes, weights=weights, k=1)[0]
        deposited = await safe_deposit(ctx.author, requested) if requested else 0
        await CONFIG.member(ctx.author).scratch_claimed_at.set(now)
        currency = await bank.get_currency_name(ctx.guild)
        if requested == 0:
            return await ctx.send("🎟️ **Ruthless Dealer Daily Scratch Ticket**\nNo prize this time. Better luck tomorrow!")
        if requested >= 5000:
            await ctx.send(f"🎟️ **Ruthless Dealer Daily Scratch Ticket — Big Winner!**\nYou scratched off **{deposited:,} {currency}**!")
        else:
            await ctx.send(f"🎟️ **Ruthless Dealer Daily Scratch Ticket**\nYou won **{deposited:,} {currency}**!")


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
            title=f"🎰 Ruthless Dealer Casino Board{title_game}",
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
                title=f"🏆 Ruthless Dealer • {member.display_name}'s Achievements ({len(unlocked)}/20)",
                description="\n\n".join(lines), color=discord.Color.gold()
            ))
        for embed in pages:
            await ctx.send(embed=embed)

    @staticmethod
    def _achievement_value(data: dict, stat: str) -> int:
        from .progression import stat_value
        return stat_value(data, stat)

    @casino.command(name="challenges", aliases=["challenge"])
    @commands.guild_only()
    async def challenges(self, ctx: commands.Context, member: discord.Member = None):
        """Show the current rotating daily and weekly challenges."""
        member = member or ctx.author
        daily_ids, weekly_ids = await ensure_rotations(ctx.guild)
        data = await CONFIG.member(member).all()
        embed = discord.Embed(title=f"📋 {member.display_name}'s Ruthless Dealer Challenges", color=discord.Color.blurple())
        for label, ids, weekly in (("☀️ Daily", daily_ids, False), ("🗓️ Weekly", weekly_ids, True)):
            state = data.get("weekly_state" if weekly else "daily_state", {})
            progress = state.get("progress", {})
            claimed = set(state.get("claimed", []))
            lines = []
            for definition in challenge_definitions(ids, weekly=weekly):
                current = min(int(progress.get(definition["id"], 0)), definition["goal"])
                status = "✅" if definition["id"] in claimed else "⬜"
                lines.append(f"{status} **{definition['name']}** — {definition['description']} `({current:,}/{definition['goal']:,})` • **{definition['reward']:,}**")
            embed.add_field(name=label, value="\n".join(lines) or "No challenges generated.", inline=False)
        embed.set_footer(text="Daily and weekly rotations exclude the immediately previous rotation. Resets use UTC.")
        await ctx.send(embed=embed)

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
            from PIL import Image, ImageDraw, ImageFont, ImageOps
            avatar_bytes = await member.display_avatar.with_size(256).read()
            avatar = Image.open(BytesIO(avatar_bytes)).convert("RGB").resize((180, 180))
            avatar = ImageOps.fit(avatar, (180, 180))
            card = Image.new("RGB", (1000, 420), (25, 27, 34))
            draw = ImageDraw.Draw(card)
            font_big = ImageFont.truetype("DejaVuSans.ttf", 34)
            font = ImageFont.truetype("DejaVuSans.ttf", 22)
            card.paste(avatar, (35, 45))
            title = data.get("equipped_title") or "Ruthless Player"
            draw.text((245, 45), member.display_name, fill="white", font=font_big)
            draw.text((245, 92), title, fill=(255, 207, 64), font=font)
            net = data.get("total_paid", 0) - data.get("total_wagered", 0)
            stats = [
                f"Games Played: {data.get('total_games', 0):,}",
                f"Wins: {data.get('wins', 0):,}",
                f"Total Wagered: {data.get('total_wagered', 0):,}",
                f"Net: {net:+,}",
                f"Achievements: {len(data.get('achievements', []))}/20",
                f"Longest Win Streak: {data.get('longest_win_streak', 0):,}",
                f"Best High/Low Streak: {data.get('best_highlow_streak', 0):,}",
            ]
            for i, line in enumerate(stats):
                x = 245 if i < 4 else 600
                y = 145 + (i if i < 4 else i - 4) * 44
                draw.text((x, y), line, fill=(225, 225, 230), font=font)
            badges = [ACHIEVEMENT_MAP[x]["emoji"] for x in data.get("achievements", []) if x in ACHIEVEMENT_MAP]
            draw.text((35, 260), "Badges", fill=(255, 207, 64), font=font)
            draw.text((35, 310), " ".join(badges[:12]) or "None yet", fill="white", font=font)
            output = BytesIO(); card.save(output, format="PNG"); output.seek(0)
            await ctx.send(file=discord.File(output, filename="casino-profile.png"))
        except Exception:
            await ctx.send("Ruthless Dealer could not render the profile card, so here is the standard casino profile instead.")
            await ctx.invoke(self.casino, member=member)

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
        free = await self._freebie_settings(ctx.guild)
        lines.extend([
            "",
            "**Free Credit Systems**",
            f"Daily stipend: {'Enabled' if free['daily_enabled'] else 'Disabled'} | {int(free['daily_min']):,}–{int(free['daily_max']):,} | {int(free['daily_cooldown']) // 3600}h",
            f"Ruthless Dealer: {'Enabled' if free['claim_enabled'] else 'Disabled'} | Below {int(free['claim_threshold']):,} grants {int(free['claim_amount']):,} | {int(free['claim_cooldown']) // 3600}h",
            f"Scratch ticket: {'Enabled' if free['scratch_enabled'] else 'Disabled'} | {int(free['scratch_cooldown']) // 3600}h",
        ])
        await ctx.send(embed=discord.Embed(title="⚙️ Ruthless Dealer Casino Settings", description="\n".join(lines), color=discord.Color.gold()))

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

