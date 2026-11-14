from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

import discord
import yaml
from redbot.core import commands
from redbot.core.utils.chat_formatting import box, pagify

__red_end_user_data_statement__ = "This cog does not store any end user data."

CogKey = Union[str, Tuple[str, ...]]

# Sentinel for the casino lobby: casino channels that no single game claims.
LOBBY = "__lobby__"


class ChannelGuide(commands.Cog):
    """Posts a friendly "where do I go?" guide built from the permissions YAML."""

    CONFIG_FILE = Path(__file__).with_name("config.yaml")

    # Each entry: (cog key(s) in config.yaml, label, commands to show, text when no channel is set).
    # `{p}` is replaced with the server's prefix.
    SECTIONS = (
        (
            "🎰  Casino",
            "Type `{p}casino` in any casino channel for a menu with every game, your stats and rewards.",
            (
                (LOBBY, "🏛️ **Casino Lobby**", "`{p}casino`", None),
                ("Blackjack", "🃏 **Blackjack**", "`{p}bj`", None),
                ("CoinFlip", "🪙 **Coin Flip**", "`{p}cf`", None),
                ("Slots", "🎰 **Slots**", "`{p}slots`", None),
                ("Roulette", "🎡 **Roulette**", "`{p}roulette` · `{p}rtable`", None),
                ("HighLow", "↕️ **High / Low**", "`{p}hl`", None),
                ("Crash", "🚀 **Crash**", "`{p}crash`", "open it from the lobby"),
                ("Mines", "💣 **Mines**", "`{p}mines`", "open it from the lobby"),
                ("DailySpin", "🎁 **Daily Rewards**", "`{p}daily` · `{p}spin`", None),
                ("Economy", "💰 **Wallet**", "`{p}payday` · `{p}bank balance`", None),
            ),
        ),
        (
            "🎮  Party Games",
            None,
            (
                ("Battleship", "🚢 **Battleship**", "`{p}battleship`", None),
                ("Trivia", "❓ **Trivia**", "`{p}trivia list`", None),
            ),
        ),
        (
            "📊  Games & Stats",
            None,
            (
                ("PokéBot", "⚡ **PokéBot**", "`{p}start` · catch the wild spawns", None),
                (("FortniteStats", "Overwatch"), "🎮 **Fortnite & Overwatch**", "`{p}fn stats` · `{p}fn shop` · `{p}ow profile`", None),
                ("UFC", "🥊 **UFC / Fight Night**", "`{p}ufc card` · `{p}ufc pick`", None),
            ),
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

        # The lobby lives in casino channels that aren't a specific game's home;
        # if every casino channel is claimed, it works in all of them.
        casino = parsed.get("Casino", [])
        if casino:
            claimed = {
                cid
                for key, *_ in self.SECTIONS[0][2]
                if key != LOBBY
                for cid in parsed.get(key, [])
            }
            parsed[LOBBY] = [cid for cid in casino if cid not in claimed] or casino
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

        for title, tip, entries in self.SECTIONS:
            lines = []
            for key, label, cmds, fallback in entries:
                ids = self._channels(rules, key)
                if ids:
                    where = " ".join(f"<#{cid}>" for cid in ids)
                elif fallback:
                    where = f"*{fallback}*"
                else:
                    continue
                lines.append(f"{label} ➜ {where}\n╰ {cmds.format(p=prefix)}")
            if not lines:
                continue
            if tip:
                lines.insert(0, f"> 💡 {tip.format(p=prefix)}\n")
            # Trailing blank line gives each section room to breathe.
            embed.add_field(name=f"━━━  {title}  ━━━", value="\n".join(lines) + "\n​", inline=False)

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

