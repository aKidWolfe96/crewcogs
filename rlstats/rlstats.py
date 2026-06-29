import asyncio
import logging
from typing import Any, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import commands, Config

log = logging.getLogger("red.rlstats")

RAPIDAPI_HOST = "rocket-league1.p.rapidapi.com"

PLAYLIST_LABELS = {
    "Duel (Ranked)": "Ranked 1v1",
    "Doubles (Ranked)": "Ranked 2v2",
    "Standard (Ranked)": "Ranked 3v3",
    "Hoops": "Hoops",
    "Rumble": "Rumble",
    "Dropshot": "Dropshot",
    "Snow Day": "Snow Day",
}

DIVISIONS = {1: "I", 2: "II", 3: "III", 4: "IV"}

PRESENCE = {
    "Online": "🟢 Online",
    "Offline": "⚫ Offline",
    "Away": "🌙 Away",
    "In Game": "🎮 In Game",
}

# Lifetime stats fetched for `[p]rlstats full`, in display order.
LIFETIME_STATS = ["wins", "goals", "assists", "saves", "shots", "mvps"]


def _streak_str(streak: Any) -> str:
    if not isinstance(streak, int) or streak == 0:
        return ""
    return f"W{streak}" if streak > 0 else f"L{abs(streak)}"


def _fmt(v: Any) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


class RLStats(commands.Cog):
    """Rocket League ranks and lifetime stats via RapidAPI."""

    def __init__(self, bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=921348675, force_registration=True)
        self.config.register_user(rl_id=None)

    def cog_unload(self):
        self.bot.loop.create_task(self.session.close())

    # -- key / id -----------------------------------------------------------
    async def _get_key(self) -> Optional[str]:
        tokens = await self.bot.get_shared_api_tokens("rocketleague")
        return tokens.get("rapidapi_key")

    async def _resolve_id(self, ctx, rl_id: Optional[str]) -> Optional[str]:
        if rl_id is None:
            rl_id = await self.config.user(ctx.author).rl_id()
        return rl_id

    @property
    def _headers(self):
        return {"x-rapidapi-host": RAPIDAPI_HOST}

    # -- HTTP ---------------------------------------------------------------
    async def _fetch_ranks(self, rl_id: str, key: str) -> Optional[dict]:
        url = f"https://{RAPIDAPI_HOST}/ranks/{rl_id}"
        headers = {**self._headers, "x-rapidapi-key": key}
        async with self.session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()

    async def _safe_ranks(self, rl_id: str, key: str) -> Optional[dict]:
        try:
            return await self._fetch_ranks(rl_id, key)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("RLStats: ranks fetch failed for %s", rl_id)
            return None

    async def _fetch_stat(self, rl_id: str, stat: str, key: str) -> Optional[dict]:
        url = f"https://{RAPIDAPI_HOST}/stat/{rl_id}/{stat}"
        headers = {**self._headers, "x-rapidapi-key": key}
        try:
            async with self.session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _fetch_profile(self, rl_id: str, key: str) -> Optional[dict]:
        url = f"https://{RAPIDAPI_HOST}/profile/{rl_id}"
        headers = {**self._headers, "x-rapidapi-key": key}
        try:
            async with self.session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _fetch_lifetime(self, rl_id: str, key: str) -> List[Tuple[str, Any]]:
        results = await asyncio.gather(
            *(self._fetch_stat(rl_id, s, key) for s in LIFETIME_STATS)
        )
        out: List[Tuple[str, Any]] = []
        for r in results:
            if r and r.get("value") is not None:
                out.append((r.get("name", "?"), r.get("value")))
        return out

    # -- embed --------------------------------------------------------------
    def _build_embed(
        self,
        raw: Optional[dict],
        rl_id: str,
        lifetime: Optional[List[Tuple[str, Any]]] = None,
        profile: Optional[dict] = None,
    ) -> discord.Embed:
        title = "Rocket League Stats"
        desc = None
        if profile:
            title = profile.get("name") or title
            bits = []
            if profile.get("tag"):
                bits.append(profile["tag"])
            if profile.get("presence"):
                bits.append(PRESENCE.get(profile["presence"], profile["presence"]))
            desc = " · ".join(bits) or None
        embed = discord.Embed(title=title, description=desc, color=discord.Color.blue())

        ranks = (raw or {}).get("ranks") or []
        if ranks:
            lines = []
            for pl in ranks:
                api_name = pl.get("playlist", "Unknown")
                label = PLAYLIST_LABELS.get(api_name, api_name)
                rank = pl.get("rank", "N/A")
                mmr = pl.get("mmr", "N/A")
                played = pl.get("played", 0)

                div = ""
                if rank and rank != "Unranked":
                    div_num = pl.get("division")
                    if div_num in DIVISIONS:
                        div = f" Div {DIVISIONS[div_num]}"

                parts = [f"{rank}{div}", f"{mmr} MMR"]
                streak = _streak_str(pl.get("streak"))
                if streak:
                    parts.append(streak)
                if isinstance(played, int) and played > 0:
                    parts.append(f"{played} games")

                lines.append(f"**{label}** — " + " · ".join(parts))
            embed.add_field(name="Ranked Playlists", value="\n".join(lines), inline=False)
        elif raw is None:
            embed.add_field(name="Ranked Playlists", value="Rank data unavailable.", inline=False)
        else:
            embed.add_field(name="Ranked Playlists", value="No rank data found.", inline=False)

        if lifetime:
            life_str = " · ".join(f"{name}: {_fmt(val)}" for name, val in lifetime)
            embed.add_field(name="Lifetime", value=life_str, inline=False)

        reward = (raw or {}).get("reward") or {}
        level = reward.get("level")
        if level and level != "None":
            progress = reward.get("progress")
            prog = f" ({progress}/10)" if isinstance(progress, int) else ""
            embed.add_field(name="Season Reward", value=f"{level}{prog}", inline=False)

        embed.set_footer(text=f"ID: {rl_id}")
        return embed

    # -- commands -----------------------------------------------------------
    @commands.group(invoke_without_command=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def rlstats(self, ctx, rl_id: Optional[str] = None):
        """Show a player's Rocket League ranks.

        Save your ID with `[p]rlstats setid <id>`, then just use `[p]rlstats`.
        For ranks + lifetime totals, use `[p]rlstats full`.
        """
        rl_id = await self._resolve_id(ctx, rl_id)
        if not rl_id:
            return await ctx.send(
                f"No ID saved. Use `{ctx.clean_prefix}rlstats setid <your_id>` "
                f"or pass one directly: `{ctx.clean_prefix}rlstats <id>`."
            )

        key = await self._get_key()
        if not key:
            return await ctx.send(
                "No RapidAPI key set. An owner needs to run:\n"
                f"`{ctx.clean_prefix}set api rocketleague rapidapi_key,YOUR_KEY`"
            )

        async with ctx.typing():
            profile_task = asyncio.ensure_future(self._fetch_profile(rl_id, key))
            try:
                raw = await self._fetch_ranks(rl_id, key)
            except aiohttp.ClientResponseError as e:
                profile_task.cancel()
                if e.status in (401, 403):
                    return await ctx.send("API rejected the key (401/403). Check your subscription.")
                if e.status == 429:
                    return await ctx.send("Rate limited (429). Try again shortly.")
                return await ctx.send(f"API error ({e.status}). Try again later.")
            except asyncio.TimeoutError:
                profile_task.cancel()
                return await ctx.send("The API timed out. Try again in a moment.")

            profile = await profile_task
            if raw is None:
                return await ctx.send(f"No player found for ID `{rl_id}`.")
            await ctx.send(embed=self._build_embed(raw, rl_id, profile=profile))

    @rlstats.command(name="full")
    @commands.cooldown(1, 30, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def rlstats_full(self, ctx, rl_id: Optional[str] = None):
        """Show ranks AND lifetime totals (uses more API calls)."""
        rl_id = await self._resolve_id(ctx, rl_id)
        if not rl_id:
            return await ctx.send(
                f"No ID saved. Use `{ctx.clean_prefix}rlstats setid <your_id>` "
                f"or pass one directly: `{ctx.clean_prefix}rlstats full <id>`."
            )

        key = await self._get_key()
        if not key:
            return await ctx.send(
                "No RapidAPI key set. An owner needs to run:\n"
                f"`{ctx.clean_prefix}set api rocketleague rapidapi_key,YOUR_KEY`"
            )

        async with ctx.typing():
            # ranks + all lifetime stats fire concurrently (~1 request of latency)
            raw, profile, lifetime = await asyncio.gather(
                self._safe_ranks(rl_id, key),
                self._fetch_profile(rl_id, key),
                self._fetch_lifetime(rl_id, key),
            )

        if raw is None and not lifetime:
            return await ctx.send(f"Couldn't fetch any data for ID `{rl_id}`.")
        await ctx.send(embed=self._build_embed(raw, rl_id, lifetime=lifetime, profile=profile))

    @rlstats.command(name="setid")
    async def rlstats_setid(self, ctx, rl_id: str):
        """Save your Rocket League account ID for quick lookups."""
        await self.config.user(ctx.author).rl_id.set(rl_id)
        await ctx.send(f"Saved your Rocket League ID: `{rl_id}`")

    @rlstats.command(name="clearid")
    async def rlstats_clearid(self, ctx):
        """Remove your saved Rocket League account ID."""
        await self.config.user(ctx.author).rl_id.clear()
        await ctx.send("Cleared your saved ID.")
