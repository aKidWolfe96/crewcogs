from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

import discord
import yaml
from redbot.core import commands
from redbot.core.utils.chat_formatting import box, pagify

__red_end_user_data_statement__ = "This cog does not store any end user data."

CogKey = Union[str, Tuple[str, ...]]

# Sentinel for the casino lobby: the channels the Casino cog (`!casino`) is allowed in.
LOBBY = "__lobby__"


class ChannelGuide(commands.Cog):
    """Posts a friendly "where do I go?" guide built from the permissions YAML."""

    CONFIG_FILE = Path(__file__).with_name("config.yaml")

    # Each section: (title, optional tip, entries).
    # Each entry: (cog key(s) in config.yaml, label, commands to show, text when no channel is set).
    # `{p}` becomes the server's prefix and `{lobby}` the lobby channel(s).
    SECTIONS = (
        (
            "🎰  Krusty Casino",
            "New here? Type `{p}casino` in {lobby} for a menu with every game, your stats and rewards.",
            (
                (LOBBY, "🏛️ **Casino Lobby**", "`{p}casino` · `{p}casino achievements`", None),
                ("Blackjack", "🃏 **Blackjack**", "`{p}bj`", None),
                ("Roulette", "🎡 **Roulette**", "`{p}roulette` · `{p}rtable`", None),
                ("CoinFlip", "🪙 **Coin Flip**", "`{p}cf`", None),
                ("HighLow", "↕️ **High / Low**", "`{p}hl`", None),
                ("Slots", "🎰 **Slots**", "`{p}slots`", "open it from the lobby"),
                ("Crash", "🚀 **Crash**", "`{p}crash`", "open it from the lobby"),
                ("Mines", "💣 **Mines**", "`{p}mines`", "open it from the lobby"),
                ("DailySpin", "🎁 **Daily Rewards**", "`{p}daily` · `{p}spin`", None),
                ("Economy", "💰 **Wallet**", "`{p}payday` · `{p}bank balance`", None),
            ),
        ),
        (
            "🎮  Games & Pokémon",
            None,
            (
                ("PokéBot", "⚡ **PokéBot**", "`{p}start` · catch the wild spawns", None),
                (("FortniteStats", "Overwatch"), "📊 **Fortnite & Overwatch**", "`{p}fn stats` · `{p}fn shop` · `{p}ow profile`", None),
                (("Trivia", "Battleship"), "❓ **Trivia & Battleship**", "`{p}trivia list` · `{p}battleship`", None),
            ),
        ),
        (
            "🏈  Sports & Bets",
            None,
            (("UFC", "🥊 **UFC / Fight Night**", "`{p}ufc card` · `{p}ufc pick` · `{p}ufc bet`", None),),
        ),
        (
            "🔔  Alerts",
            None,
            (("TwitchAlerts", "📺 **Twitch Live**", "posts automatically", None),),
        ),
    )

    def __init__(self, bot):
        self.bot = bot

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_rules(self) -> Dict[str, List[int]]:
        """Read enabled channel IDs from the bundled Red Permissions YAML."""
        try:
            raw = yaml.safe_load(self.CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(f"Could not read {self.CONFIG_FILE.name}: {exc}") from exc

        cog_rules = raw.get("COG", {})
        if not isinstance(cog_rules, dict):
            raise RuntimeError("The permissions YAML must contain a top-level COG mapping.")

        parsed: Dict[str, List[int]] = {}
        for cog_name, rules in cog_rules.items():
            if not isinstance(rules, dict):
                continue
            ids: List[int] = []
            for target, allowed in rules.items():
                if str(target).lower() == "default" or allowed is not True:
                    continue
                try:
                    ids.append(int(target))
                except (TypeError, ValueError):
                    continue
            parsed[str(cog_name)] = ids

        if parsed.get("Casino"):
            parsed[LOBBY] = parsed["Casino"]
        return parsed

    @staticmethod
    def _channels(rules: Dict[str, List[int]], key: CogKey) -> Optional[List[int]]:
        """Channel IDs for a key (or several), or None if the cog isn't in the YAML."""
        keys: Iterable[str] = key if isinstance(key, tuple) else (key,)
        present = [k for k in keys if k in rules]
        if not present:
            return None
        combined: List[int] = []
        for k in present:
            for cid in rules[k]:
                if cid not in combined:
                    combined.append(cid)
        return combined

    # ── Embed ─────────────────────────────────────────────────────────────────

    def build_embed(self, prefix: str = "!", guild: Optional[discord.Guild] = None) -> discord.Embed:
        rules = self._load_rules()

        embed = discord.Embed(
            title="🗺️  Where Do I Go?",
            description=(
                "Every bot feature has its own home channel.\n"
                "**1.** Find what you want below\n"
                "**2.** Tap the channel to jump there\n"
                "**3.** Type the command shown\n"
                "​"
            ),
            color=discord.Color.from_rgb(88, 101, 242),
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        # Every channel used by the casino section, so a game allowed in all of
        # them can say "any casino channel" in place of a long row of links.
        casino_all = {
            cid for key, *_ in self.SECTIONS[0][2] for cid in (self._channels(rules, key) or [])
        }
        lobby_ids = rules.get(LOBBY, [])
        lobby = f"<#{lobby_ids[0]}>" if lobby_ids else "any casino channel"

        for title, tip, entries in self.SECTIONS:
            lines = []
            for key, label, cmds, fallback in entries:
                ids = self._channels(rules, key)
                if ids and len(ids) > 2 and set(ids) == casino_all:
                    where = "*any casino channel*"
                elif ids:
                    where = " ".join(f"<#{cid}>" for cid in ids)
                elif fallback:
                    where = f"*{fallback}*"
                else:
                    continue
                lines.append(f"{label} ➜ {where}\n╰ {cmds.format(p=prefix)}")
            if not lines:
                continue
            if tip:
                lines.insert(0, f"> 💡 {tip.format(p=prefix, lobby=lobby)}\n")
            # Trailing blank line gives each section room to breathe.
            embed.add_field(name=f"━━━  {title}  ━━━", value="\n".join(lines) + "\n\u200b", inline=False)

        embed.set_footer(text="Typed it in the wrong channel? The bot just ignores it. No harm done!")
        return embed

    # ── Command ───────────────────────────────────────────────────────────────

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.command()
    async def channelguide(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Post the channel guide here, or in another channel."""
        target = channel or ctx.channel
        try:
            embed = self.build_embed(ctx.clean_prefix, ctx.guild)
        except RuntimeError as exc:
            await ctx.send(f"❌ ChannelGuide configuration error: {exc}")
            return

        await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        if channel is not None:
            await ctx.tick()

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.command(aliases=["channelid", "chids"])
    async def channelids(self, ctx: commands.Context):
        """List every channel in this server with its ID, grouped by category."""
        icons = {
            discord.ChannelType.text: "#",
            discord.ChannelType.news: "📢",
            discord.ChannelType.voice: "🔊",
            discord.ChannelType.stage_voice: "🎙️",
            discord.ChannelType.forum: "💬",
        }
        lines = []
        for category, channels in ctx.guild.by_category():
            lines.append(f"\n[{category.name}]  {category.id}" if category else "[No category]")
            for ch in channels:
                lines.append(f"  {icons.get(ch.type, '•')} {ch.name}  {ch.id}")

        for page in pagify("\n".join(lines).strip(), page_length=1900):
            await ctx.send(box(page, lang="ini"))

