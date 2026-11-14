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

from pathlib import Path

from . import render
from .data import ITEM_TYPE_ALIASES, ITEM_TYPE_LABELS, DataError, GameData, Gear, Perk, norm
from .icons import IconLibrary
from .views import P100BoardView, RerollView, png_file

__version__ = "1.1.0"
log = logging.getLogger("red.deadbydaylight")

ROLE_ALIASES = {"killer": "killer", "k": "killer", "slasher": "killer",
                "survivor": "survivor", "surv": "survivor", "s": "survivor", "survivors": "survivor"}
ROLE_COLORS = {"killer": 0xBE202C, "survivor": 0x2C74CE}
WISHLIST_LIMIT = 20
MEDALS = ("🥇", "🥈", "🥉")




class DeadByDaylight(commands.Cog):
    """Shrine of Secrets, perk lookups, build randomizers and a Prestige 100 tracker."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2026111401, force_registration=True)
        # P100s and wishlists are per person, so they follow you to every server.
        self.config.register_user(p100=[], wishlist=[])
        self.config.register_guild(shrine_channel=None, announce_channel=None, last_shrine=None, shoutouts=True)
        self.data = GameData(f"RedDiscordBot-DBD-Cog/{__version__}")
        self.icons = IconLibrary(Path(__file__).parent)
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
    # ── dict builders for the renderer (icons ride along as bytes) ──────
    def P(self, p: Perk) -> dict:
        return {"name": p.name, "role": p.role, "owner": p.character.name if p.character else None,
                "icon": self.icons.get(p.image)}

    def C(self, c) -> dict:
        return {"name": c.name, "role": c.role, "portrait": self.icons.get(c.image),
                "difficulty": "" if c.difficulty in ("", "none") else c.difficulty}

    def G(self, g: Gear, sub: str = None) -> dict:
        return {"kind": g.kind, "name": g.name, "rarity": g.rarity, "icon": self.icons.get(g.image), "sub": sub}

    async def shrine_post(self) -> tuple[str, discord.File, dict]:
        await self.data.load()
        shrine = await self.data.shrine()
        perks = []
        for entry in shrine.get("perks", []):
            p = self.data.perks.get(entry["id"])
            if p is None:
                continue
            perks.append({**self.P(p), "shards": entry.get("shards"), "bloodpoints": entry.get("bloodpoints"),
                          "owner_portrait": self.icons.get(p.character.image) if p.character else None, "key": p.key})
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
        png = await self._png(render.badge_png, perk.name, perk.role, self.icons.get(perk.image), 256)
        embed.set_thumbnail(url="attachment://perk.png")
        await ctx.send(embed=embed, file=png_file(png, "perk"))

    @dbd.command(name="character", aliases=["char"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_character(self, ctx: commands.Context, *, name: str):
        """A killer or survivor with their three teachable perks. Nicknames work (`myers`, `chucky`)."""
        if not await self._ready(ctx):
            return
        ch = self.data.find_character(name)
        if ch is None:
            return await ctx.send(f"No character found matching **{name}**.")
        perks = [self.P(p) for p in self.data.character_perks(ch)]
        note = "Unlocked at levels 30 / 35 / 40"
        power = self.data.power_of(ch)
        if power:
            note += f" · Power: {power.name}"
        png = await self._png(render.render_character, self.C(ch), perks, note)
        bio = render_text(ch.bio)
        await ctx.send(content=f"-# {bio[:300]}" if bio else None, file=png_file(png, norm(ch.id) or "character"))

    # ------------------------------------------------------------------ #
    # Loadouts (one command does the whole thing)
    # ------------------------------------------------------------------ #
    def roll_loadout(self, role: str, character=None, item_type: str = None, exclude: set = None):
        """Random full loadout: character, 4 perks, item/power + 2 matching add-ons, offering."""
        d = self.data
        if character is None:
            roster = [c for c in d.roster(role) if not exclude or c.id not in exclude]
            character = random.choice(roster or d.roster(role))
        perks = random.sample(d.perks_for_role(role), 4)
        gear = []
        if role == "killer":
            main = d.power_of(character)
            sub = "Power"
        else:
            items = d.survivor_items(item_type) or d.survivor_items()
            main = random.choice(items) if items else None
            sub = None
        if main is not None:
            gear.append(self.G(main, sub))
            pool = d.addons_for(main, randomizable_only=True)
            for a in random.sample(pool, min(2, len(pool))):
                gear.append(self.G(a))
        offers = d.offerings_for(role, character)
        if offers:
            gear.append(self.G(random.choice(offers), "Offering"))
        return character, perks, gear

    @dbd.command(name="loadout", aliases=["build", "roll", "random"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_loadout(self, ctx: commands.Context, *, who: Optional[str] = None):
        """A full random loadout: character, perks, item/power with add-ons, and an offering.

        `[p]dbd loadout` · `[p]dbd loadout killer` · `[p]dbd loadout survivor`
        `[p]dbd loadout nurse` · `[p]dbd loadout flashlight`
        """
        if not await self._ready(ctx):
            return
        role, character, item_type = None, None, None
        if who:
            q = who.lower().strip()
            role = ROLE_ALIASES.get(q)
            if role is None and q in ITEM_TYPE_ALIASES:
                role, item_type = "survivor", ITEM_TYPE_ALIASES[q]
            if role is None:
                character = self.data.find_character(who)
                if character is None:
                    return await ctx.send(
                        f"Say `killer`, `survivor`, a character, or an item type (toolbox, medkit, flashlight, map, key). "
                        f"I don't know **{who}**.")
                role = character.role

        async def render_one():
            r = role or random.choice(("killer", "survivor"))
            ch, perks, gear = self.roll_loadout(r, character, item_type)
            png = await self._png(render.render_loadout, self.C(ch), [self.P(p) for p in perks], gear,
                                  ctx.author.display_name)
            return None, png_file(png, "loadout")

        async with ctx.typing():
            _, file = await render_one()
        view = RerollView(ctx.author.id, render_one)
        view.message = await ctx.send(file=file, view=view)

    @dbd.command(name="squad", aliases=["team", "party"])
    @commands.guild_only()
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def dbd_squad(self, ctx: commands.Context, mode: Optional[str] = None):
        """Full loadouts for everyone in your voice channel.

        `[p]dbd squad`: everyone gets a survivor loadout (no duplicate survivors).
        `[p]dbd squad killer`: one random person is the killer, great for customs.
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
                ch, perks, gear = self.roll_loadout("killer")
                rows.append({"player": killer.display_name, "character": self.C(ch),
                             "perks": [self.P(p) for p in perks], "gear": gear})
            for m in sorted(order, key=lambda m: m.display_name.lower()):
                ch, perks, gear = self.roll_loadout("survivor", exclude=used)
                used.add(ch.id)
                rows.append({"player": m.display_name, "character": self.C(ch),
                             "perks": [self.P(p) for p in perks], "gear": gear})
            png = await self._png(render.render_squad, rows, "Custom game" if killer else "Survivor squad")
            return None, png_file(png, "squad")

        async with ctx.typing():
            _, file = await render_squad()
        view = RerollView(ctx.author.id, render_squad)
        view.message = await ctx.send(file=file, view=view)

    # ------------------------------------------------------------------ #
    # Item / add-on / offering lookups
    # ------------------------------------------------------------------ #
    async def _gear_embed(self, ctx, gear: Gear, others: list, owner: str = None):
        color = render.RARITY_RGB.get(gear.rarity, (120, 120, 130))
        embed = discord.Embed(title=gear.name, description=gear.description[:4000] or None,
                              color=discord.Color.from_rgb(*color))
        if gear.rarity_label:
            embed.add_field(name="Rarity", value=gear.rarity_label, inline=True)
        if owner:
            embed.add_field(name="For", value=owner, inline=True)
        if gear.role:
            embed.add_field(name="Role", value=gear.role.title(), inline=True)
        if not gear.randomizable:
            embed.add_field(name="Note", value="Event or special item, so it's left out of random loadouts.", inline=False)
        if others:
            embed.set_footer(text="Did you mean: " + ", ".join(o.name for o in others))
        png = await self._png(render.gear_png, self.G(gear), 256)
        embed.set_thumbnail(url="attachment://gear.png")
        await ctx.send(embed=embed, file=png_file(png, "gear"))

    @dbd.command(name="item")
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_item(self, ctx: commands.Context, *, name: str):
        """Look up a survivor item."""
        if not await self._ready(ctx):
            return
        item, others = self.data.find_item(name)
        if item is None:
            return await ctx.send(f"No item found matching **{name}**.")
        await self._gear_embed(ctx, item, others, ITEM_TYPE_LABELS.get(item.item_type or "", None))

    @dbd.command(name="addon", aliases=["add-on"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_addon(self, ctx: commands.Context, *, name: str):
        """Look up an add-on (survivor item or killer power)."""
        if not await self._ready(ctx):
            return
        addon, others = self.data.find_addon(name)
        if addon is None:
            return await ctx.send(f"No add-on found matching **{name}**.")
        await self._gear_embed(ctx, addon, others, self.data.addon_owner(addon))

    @dbd.command(name="offering", aliases=["offer"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def dbd_offering(self, ctx: commands.Context, *, name: str):
        """Look up an offering."""
        if not await self._ready(ctx):
            return
        offering, others = self.data.find_offering(name)
        if offering is None:
            return await ctx.send(f"No offering found matching **{name}**.")
        await self._gear_embed(ctx, offering, others, (offering.role or "everyone").title())

    @dbd.command(name="addons", aliases=["add-ons"])
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def dbd_addons(self, ctx: commands.Context, *, killer_or_item: str):
        """Every add-on for a killer's power or a survivor item type, by rarity.

        `[p]dbd addons nurse` · `[p]dbd addons flashlight`
        """
        if not await self._ready(ctx):
            return
        q = killer_or_item.lower().strip()
        if q in ITEM_TYPE_ALIASES:
            item_type = ITEM_TYPE_ALIASES[q]
            parent = next((i for i in self.data.survivor_items(item_type)), None)
            if parent is None:
                return await ctx.send("No items of that type right now.")
            addons = self.data.addons_for(parent)
            title = f"{ITEM_TYPE_LABELS.get(item_type, item_type)} add-ons"
            sub = f"{len(addons)} add-ons"
        else:
            ch = self.data.find_character(killer_or_item, role="killer")
            power = self.data.power_of(ch) if ch else None
            if power is None:
                return await ctx.send(f"Give me a killer or an item type. I don't know **{killer_or_item}**.")
            parent, addons = power, self.data.addons_for(power)
            title = f"{ch.short}'s add-ons"
            sub = f"{power.name} · {len(addons)} add-ons"
        async with ctx.typing():
            png = await self._png(render.render_addons, title, sub, self.G(parent, "Power"),
                                  [self.G(a) for a in addons if not a.name.startswith("@#")])
        await ctx.send(file=png_file(png, "addons"))

    # ------------------------------------------------------------------ #
    # Prestige 100 tracker
    # ------------------------------------------------------------------ #
    async def prestige_card(self, member) -> discord.File:
        done = set(await self.config.user(member).p100())
        killers = [(c.id, c.short, self.icons.get(c.image)) for c in self.data.roster("killer")]
        survivors = [(c.id, c.name, self.icons.get(c.image)) for c in self.data.roster("survivor")]
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

    @dbdset.command(name="assets", aliases=["icons"])
    async def dbdset_assets(self, ctx: commands.Context):
        """Check the exported game icons: what matched and what's missing after a patch."""
        if not await self._ready(ctx):
            return
        self.icons.scan()
        d = self.data
        groups = [
            ("Perks", [(p.name, p.image) for p in d.perks.values()]),
            ("Portraits", [(c.name, c.image) for c in d.characters.values()]),
            ("Items & powers", [(i.name, i.image) for i in d.items.values() if not i.name.startswith("@#")]),
            ("Add-ons", [(a.name, a.image) for a in d.addons.values() if not a.name.startswith("@#")]),
            ("Offerings", [(o.name, o.image) for o in d.offerings.values()]),
        ]
        lines, missing_all = [], []
        for label, entries in groups:
            missing = [n for n, img in entries if not self.icons.locate(img)]
            mark = "✅" if not missing else "⚠️"
            lines.append(f"{mark} **{label}**: {len(entries) - len(missing)}/{len(entries)}")
            missing_all += [f"{label[:-1] if label.endswith('s') else label}: {n}" for n in missing]
        embed = discord.Embed(title="🖼️ Dead by Daylight icons", color=0xBE202C,
                              description="\n".join(lines) + f"\n\n{self.icons.count} image files found in the cog folder.")
        if missing_all:
            embed.add_field(name=f"Missing ({len(missing_all)}): export these from FModel",
                            value="\n".join(missing_all[:25]) + (f"\n…and {len(missing_all) - 25} more" if len(missing_all) > 25 else ""),
                            inline=False)
        else:
            embed.set_footer(text="Everything matched. Missing icons fall back to drawn badges.")
        await ctx.send(embed=embed)

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
