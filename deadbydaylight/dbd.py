"""Dead by Daylight for Red: Shrine of Secrets, perk lookups, randomizers and a
Prestige 100 tracker. Works for every platform: no accounts or API keys.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

from . import render
from .data import DataError, GameData, Perk
from .views import P100BoardView, RerollView, png_file

__version__ = "1.0.0"
log = logging.getLogger("red.deadbydaylight")

ROLE_ALIASES = {"killer": "killer", "k": "killer", "slasher": "killer",
                "survivor": "survivor", "surv": "survivor", "s": "survivor", "survivors": "survivor"}
ROLE_COLORS = {"killer": 0xBE202C, "survivor": 0x2C74CE}
WISHLIST_LIMIT = 20
MEDALS = ("🥇", "🥈", "🥉")


def perk_dict(p: Perk) -> dict:
    return {"name": p.name, "role": p.role, "owner": p.character.name if p.character else None}


class DeadByDaylight(commands.Cog):
    """Shrine of Secrets, perk lookups, build randomizers and a Prestige 100 tracker."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2026111401, force_registration=True)
        # P100s and wishlists are per person, so they follow you to every server.
        self.config.register_user(p100=[], wishlist=[])
        self.config.register_guild(shrine_channel=None, announce_channel=None, last_shrine=None, shoutouts=True)
        self.data = GameData(f"RedDiscordBot-DBD-Cog/{__version__}")
        self._board_view: P100BoardView | None = None

    async def cog_load(self):
        self._board_view = P100BoardView(self)
        self.bot.add_view(self._board_view)
        self.shrine_watch.start()

    async def cog_unload(self):
        self.shrine_watch.cancel()
        if self._board_view:
            self._board_view.stop()
        await self.data.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        await self.config.user_from_id(user_id).clear()

    def format_help_for_context(self, ctx):
        return f"{super().format_help_for_context(ctx)}\n\nCog Version: {__version__}"

    async def _ready(self, ctx) -> bool:
        try:
            await self.data.load()
            return True
        except DataError:
            await ctx.send("⚠️ Couldn't reach the Dead by Daylight data service. Try again in a bit.")
            return False

    async def _png(self, fn, *args, **kwargs) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    @commands.group(name="dbd")
    async def dbd(self, ctx: commands.Context):
        """Dead by Daylight: shrine, perks, randomizers and Prestige 100s."""

    # ------------------------------------------------------------------ #
    # Shrine of Secrets
    # ------------------------------------------------------------------ #
    async def shrine_post(self) -> tuple[str, discord.File, dict]:
        await self.data.load()
        shrine = await self.data.shrine()
        perks = []
        for entry in shrine.get("perks", []):
            p = self.data.perks.get(entry["id"])
            if p is None:
                continue
            perks.append({**perk_dict(p), "shards": entry.get("shards"), "bloodpoints": entry.get("bloodpoints"), "key": p.key})
        end = int(shrine.get("end") or 0) + 1
        label = dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime("%a, %b %-d") if end > 1 else "soon"
        png = await self._png(render.render_shrine, perks, label)
        names = " · ".join(f"**{p['name']}**" for p in perks)
        content = f"## 🕯️ Shrine of Secrets\n{names}\n-# Refreshes <t:{end}:R> · `dbd perk <name>` for details"
        return content, png_file(png, "shrine"), {"id": shrine.get("id"), "perks": [p["key"] for p in perks]}

    @dbd.command(name="shrine")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def dbd_shrine(self, ctx: commands.Context):
        """This week's Shrine of Secrets."""
        async with ctx.typing():
            try:
                content, file, _ = await self.shrine_post()
            except DataError:
                return await ctx.send("⚠️ Couldn't load the Shrine right now. Try again in a bit.")
        await ctx.send(content, file=file)

    async def _wishlist_pings(self, guild: discord.Guild, perk_keys: list[str]) -> list[str]:
        lines = []
        for uid, data in (await self.config.all_users()).items():
            member = guild.get_member(uid)
            hits = [self.data.perks[k].name for k in data.get("wishlist", []) if k in perk_keys and k in self.data.perks]
            if member and hits:
                lines.append(f"{member.mention} " + ", ".join(f"**{h}**" for h in hits))
        return lines

    async def _post_shrine(self, guild: discord.Guild, channel: discord.TextChannel) -> bool:
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.attach_files):
            return False
        try:
            content, file, meta = await self.shrine_post()
            await channel.send(content, file=file)
            pings = await self._wishlist_pings(guild, meta["perks"])
            if pings:
                for page in pagify("## ⭐ Wishlist alert! These are in the Shrine:\n" + "\n".join(pings), page_length=1900):
                    await channel.send(page, allowed_mentions=discord.AllowedMentions(users=True))
            await self.config.guild(guild).last_shrine.set(meta["id"])
            return True
        except (DataError, discord.HTTPException):
            log.warning("shrine post failed in %s", guild.id, exc_info=True)
            return False

    @tasks.loop(minutes=30)
    async def shrine_watch(self):
        await self.bot.wait_until_red_ready()
        guilds = {gid: g for gid, g in (await self.config.all_guilds()).items() if g.get("shrine_channel")}
        if not guilds:
            return
        try:
            shrine = await self.data.shrine()
        except DataError:
            return
        for gid, g in guilds.items():
            if g.get("last_shrine") == shrine.get("id"):
                continue
            guild = self.bot.get_guild(gid)
            channel = guild.get_channel(g["shrine_channel"]) if guild else None
            if channel is not None:
                await self._post_shrine(guild, channel)

    # ------------------------------------------------------------------ #
    # Perks & characters
    # ------------------------------------------------------------------ #
    @dbd.command(name="perk", aliases=["p"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_perk(self, ctx: commands.Context, *, name: str):
        """Look up a perk. Nicknames work: `dh`, `ds`, `bbq`, `noed`, `pop`, `cob`…"""
        if not await self._ready(ctx):
            return
        perk, others = self.data.find_perk(name)
        if perk is None:
            return await ctx.send(f"No perk found matching **{name}**.")
        embed = discord.Embed(title=perk.name, description=perk.description[:4000], color=ROLE_COLORS.get(perk.role))
        if perk.character:
            teach = f"\nTeachable at level **{perk.teachable}**" if perk.teachable else ""
            embed.add_field(name="From", value=f"{perk.character.name}{teach}", inline=True)
        else:
            embed.add_field(name="From", value="General perk (everyone has it)", inline=True)
        embed.add_field(name="Role", value=perk.role.title(), inline=True)
        try:
            shrine = await self.data.shrine()
            entry = next((e for e in shrine.get("perks", []) if e["id"] == perk.key), None)
        except DataError:
            entry = None
        if entry:
            embed.add_field(name="🕯️ In the Shrine this week!",
                            value=f"{entry.get('shards', 0):,} Iridescent Shards", inline=False)
        if others:
            embed.set_footer(text="Did you mean: " + ", ".join(p.name for p in others))
        png = await self._png(render.badge_png, perk.name, perk.role, 256)
        embed.set_thumbnail(url="attachment://perk.png")
        await ctx.send(embed=embed, file=png_file(png, "perk"))

    @dbd.command(name="character", aliases=["char", "killer", "survivor"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_character(self, ctx: commands.Context, *, name: str):
        """A killer or survivor with their three teachable perks. Nicknames work (`myers`, `chucky`)."""
        if not await self._ready(ctx):
            return
        ch = self.data.find_character(name)
        if ch is None:
            return await ctx.send(f"No character found matching **{name}**.")
        perks = self.data.character_perks(ch)
        png = await self._png(
            render.render_build, ch.name, ch.role, [perk_dict(p) for p in perks],
            note="Teachable perks · unlocked at levels 30 / 35 / 40",
            tag=("KILLER" if ch.role == "killer" else "SURVIVOR") + (f" · {ch.difficulty.upper()}" if ch.difficulty not in ("", "none") else ""),
        )
        bio = render_text(ch.bio)
        await ctx.send(content=f"-# {bio[:300]}" if bio else None, file=png_file(png, ch.id.lower()))

    # ------------------------------------------------------------------ #
    # Randomizers
    # ------------------------------------------------------------------ #
    def _random_build(self, role: str, character=None, exclude: set | None = None):
        roster = [c for c in self.data.roster(role) if not exclude or c.id not in exclude]
        ch = character or random.choice(roster)
        perks = random.sample(self.data.perks_for_role(role), 4)
        return ch, perks

    @dbd.command(name="build", aliases=["random", "roll"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_build(self, ctx: commands.Context, *, who: Optional[str] = None):
        """A random character + 4 perks.

        `[p]dbd build` · `[p]dbd build killer` · `[p]dbd build survivor` · `[p]dbd build nurse`
        """
        if not await self._ready(ctx):
            return
        role, character = None, None
        if who:
            role = ROLE_ALIASES.get(who.lower())
            if role is None:
                character = self.data.find_character(who)
                if character is None:
                    return await ctx.send(f"Say `killer`, `survivor`, or a character name. I don't know **{who}**.")
                role = character.role

        async def render_one():
            r = role or random.choice(("killer", "survivor"))
            ch, perks = self._random_build(r, character)
            png = await self._png(render.render_build, ch.name, r, [perk_dict(p) for p in perks],
                                  who=ctx.author.display_name)
            return None, png_file(png, "build")

        async with ctx.typing():
            _, file = await render_one()
        view = RerollView(ctx.author.id, render_one)
        view.message = await ctx.send(file=file, view=view)

    @dbd.command(name="squad", aliases=["team", "party"])
    @commands.guild_only()
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def dbd_squad(self, ctx: commands.Context, mode: Optional[str] = None):
        """Random builds for everyone in your voice channel.

        `[p]dbd squad` gives everyone a survivor build (no duplicate survivors).
        `[p]dbd squad killer` picks one random person to be the killer, great for customs.
        """
        if not await self._ready(ctx):
            return
        voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if voice is None:
            return await ctx.send("Hop in a voice channel with your squad first.")
        players = [m for m in voice.members if not m.bot][:8]
        with_killer = (mode or "").lower() in ("killer", "custom", "customs", "1v4", "k")

        async def render_squad():
            order = players[:]
            random.shuffle(order)
            killer = order.pop() if with_killer and len(order) > 1 else None
            rows, used = [], set()
            if killer:
                ch, perks = self._random_build("killer")
                rows.append({"player": killer.display_name, "role": "killer", "character": ch.name,
                             "perks": [perk_dict(p) for p in perks]})
            for m in sorted(order, key=lambda m: m.display_name.lower()):
                ch, perks = self._random_build("survivor", exclude=used)
                used.add(ch.id)
                rows.append({"player": m.display_name, "role": "survivor", "character": ch.name,
                             "perks": [perk_dict(p) for p in perks]})
            title = "Custom game" if killer else "Survivor squad"
            png = await self._png(render.render_squad, rows, title)
            return None, png_file(png, "squad")

        async with ctx.typing():
            _, file = await render_squad()
        view = RerollView(ctx.author.id, render_squad)
        view.message = await ctx.send(file=file, view=view)

    # ------------------------------------------------------------------ #
    # Prestige 100 tracker
    # ------------------------------------------------------------------ #
    async def prestige_card(self, member) -> discord.File:
        done = set(await self.config.user(member).p100())
        killers = [(c.id, c.short) for c in self.data.roster("killer")]
        survivors = [(c.id, c.name) for c in self.data.roster("survivor")]
        try:
            avatar = await member.display_avatar.replace(size=128, format="png").read()
        except (discord.HTTPException, AttributeError):
            avatar = None
        png = await self._png(render.render_prestige, member.display_name, avatar, done, killers, survivors)
        return png_file(png, "prestige")

    @dbd.group(name="p100", aliases=["prestige", "p100s"], invoke_without_command=True)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def dbd_p100(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your Prestige 100 board. Tap 🩸 **Update my P100s** to tick characters off."""
        if not await self._ready(ctx):
            return
        member = member or ctx.author
        async with ctx.typing():
            file = await self.prestige_card(member)
        await ctx.send(file=file, view=self._board_view)

    @dbd_p100.command(name="top", aliases=["leaderboard", "lb"])
    @commands.guild_only()
    async def dbd_p100_top(self, ctx: commands.Context):
        """Who has the most Prestige 100s in this server."""
        if not await self._ready(ctx):
            return
        valid = set(self.data.characters)
        rows = []
        for uid, data in (await self.config.all_users()).items():
            member = ctx.guild.get_member(uid)
            ids = [c for c in data.get("p100", []) if c in valid]
            if member and ids:
                k = sum(1 for c in ids if self.data.characters[c].role == "killer")
                rows.append((len(ids), k, len(ids) - k, member))
        if not rows:
            return await ctx.send(f"No P100s yet! Start with `{ctx.clean_prefix}dbd p100`.")
        rows.sort(key=lambda r: (-r[0], r[3].display_name.lower()))
        total = len(valid)
        lines = [
            f"{MEDALS[i] if i < 3 else f'`{i + 1:>2}.`'} **{m.display_name}** — **{n}**/{total}  ·  🔪 {k} · 🏃 {s}"
            for i, (n, k, s, m) in enumerate(rows[:25])
        ]
        embed = discord.Embed(title="🩸 Prestige 100 leaderboard", description="\n".join(lines), color=0xBE202C)
        embed.set_footer(text="🔪 killers · 🏃 survivors · self-reported")
        await ctx.send(embed=embed)

    async def p100_shoutout(self, guild: discord.Guild, user, names: list[str], count: int, fallback=None):
        g = await self.config.guild(guild).all()
        if not g["shoutouts"]:
            return
        channel = guild.get_channel(g["announce_channel"]) if g["announce_channel"] else fallback
        if channel is None or not names:
            return
        who = getattr(user, "display_name", str(user))
        listed = ", ".join(f"**{n}**" for n in names[:5]) + (f" +{len(names) - 5} more" if len(names) > 5 else "")
        text = f"🩸 **{who}** hit **Prestige 100** on {listed}! ({count} total)"
        if count >= len(self.data.characters) > 0:
            text += f"\n👑 **{who} has maxed EVERY character. Absolute legend.**"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ #
    # Perk wishlist (Shrine pings)
    # ------------------------------------------------------------------ #
    @dbd.group(name="wishlist", aliases=["wl", "wish"], invoke_without_command=True)
    async def dbd_wishlist(self, ctx: commands.Context):
        """Perks you want. You get pinged when they show up in the Shrine."""
        if not await self._ready(ctx):
            return
        wl = [self.data.perks[k] for k in await self.config.user(ctx.author).wishlist() if k in self.data.perks]
        if not wl:
            return await ctx.send(f"Your perk wishlist is empty. Add one: `{ctx.clean_prefix}dbd wishlist add dead hard`.")
        try:
            shrine_keys = {e["id"] for e in (await self.data.shrine()).get("perks", [])}
        except DataError:
            shrine_keys = set()
        lines = [f"{'🕯️' if p.key in shrine_keys else '⭐'} {p.name} · *{p.character.name if p.character else 'General'}*" for p in wl]
        embed = discord.Embed(title=f"⭐ {ctx.author.display_name}'s perk wishlist", description="\n".join(lines), color=0xBE202C)
        embed.set_footer(text=f"{len(wl)}/{WISHLIST_LIMIT} · 🕯️ = in the Shrine right now")
        await ctx.send(embed=embed)

    @dbd_wishlist.command(name="add")
    async def dbd_wishlist_add(self, ctx: commands.Context, *, perk: str):
        """Add a perk to your wishlist."""
        if not await self._ready(ctx):
            return
        p, _ = self.data.find_perk(perk)
        if p is None:
            return await ctx.send(f"No perk found matching **{perk}**.")
        async with self.config.user(ctx.author).wishlist() as wl:
            if p.key in wl:
                return await ctx.send(f"**{p.name}** is already on your wishlist.")
            if len(wl) >= WISHLIST_LIMIT:
                return await ctx.send(f"Your wishlist is full ({WISHLIST_LIMIT}). Remove one first.")
            wl.append(p.key)
        await ctx.send(f"⭐ Added **{p.name}**. I'll ping you when it's in the Shrine of Secrets.")

    @dbd_wishlist.command(name="remove", aliases=["del", "delete"])
    async def dbd_wishlist_remove(self, ctx: commands.Context, *, perk: str):
        """Remove a perk from your wishlist."""
        if not await self._ready(ctx):
            return
        p, _ = self.data.find_perk(perk)
        async with self.config.user(ctx.author).wishlist() as wl:
            if p is None or p.key not in wl:
                return await ctx.send(f"**{perk}** isn't on your wishlist.")
            wl.remove(p.key)
        await ctx.send(f"Removed **{p.name}**.")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    @commands.group(name="dbdset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def dbdset(self, ctx: commands.Context):
        """Dead by Daylight settings."""

    @dbdset.command(name="shrine")
    async def dbdset_shrine(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Auto-post the Shrine of Secrets here every week (with wishlist pings). Leave empty to stop."""
        await self.config.guild(ctx.guild).shrine_channel.set(channel.id if channel else None)
        if channel is None:
            return await ctx.send("Weekly Shrine posts turned off.")
        await ctx.send(f"🕯️ The Shrine of Secrets will post in {channel.mention} whenever it refreshes. "
                       f"Run `{ctx.clean_prefix}dbdset postshrine` to post this week's now.")

    @dbdset.command(name="postshrine")
    async def dbdset_postshrine(self, ctx: commands.Context):
        """Post this week's Shrine to the Shrine channel now."""
        cid = await self.config.guild(ctx.guild).shrine_channel()
        channel = ctx.guild.get_channel(cid) if cid else None
        if channel is None:
            return await ctx.send(f"Set a channel first: `{ctx.clean_prefix}dbdset shrine #channel`.")
        async with ctx.typing():
            ok = await self._post_shrine(ctx.guild, channel)
        await ctx.send(f"Posted in {channel.mention}." if ok else "Couldn't post there. Check my permissions.")

    @dbdset.command(name="announce")
    async def dbdset_announce(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Where Prestige 100 shout-outs go. Leave empty to post them wherever people update."""
        await self.config.guild(ctx.guild).announce_channel.set(channel.id if channel else None)
        await ctx.send(f"P100 shout-outs go to {channel.mention}." if channel else
                       "P100 shout-outs will post in the channel where someone updates.")

    @dbdset.command(name="shoutouts")
    async def dbdset_shoutouts(self, ctx: commands.Context, on: bool):
        """Turn P100 shout-outs on or off."""
        await self.config.guild(ctx.guild).shoutouts.set(on)
        await ctx.send(f"P100 shout-outs {'on' if on else 'off'}.")

    @dbdset.command(name="settings")
    async def dbdset_settings(self, ctx: commands.Context):
        """Show this server's settings."""
        g = await self.config.guild(ctx.guild).all()
        sh = ctx.guild.get_channel(g["shrine_channel"]) if g["shrine_channel"] else None
        an = ctx.guild.get_channel(g["announce_channel"]) if g["announce_channel"] else None
        await ctx.send(box(
            f"Shrine posts : {'#' + sh.name if sh else 'off'}\n"
            f"P100 shouts  : {'off' if not g['shoutouts'] else ('#' + an.name if an else 'where people update')}",
            lang="yaml",
        ))


def render_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", html or "")
    text = re.sub(r"</?b>", "**", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()
