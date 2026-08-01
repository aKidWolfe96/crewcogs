from pathlib import Path
from typing import Dict, List

import discord
import yaml
from redbot.core import commands

__red_end_user_data_statement__ = "This cog does not store any end user data."


class ChannelGuide(commands.Cog):
    """Posts a compact channel-location guide from the permissions YAML."""

    CONFIG_FILE = Path(__file__).with_name("config.yaml")

    # The generic Casino permission entry is intentionally omitted because it
    # is a parent rule spanning all casino channels, not a separate destination.
    SECTIONS = (
        (
            "━━ 🎰  CASINO & ECONOMY  ━━",
            (
                ("Blackjack", "🃏 Blackjack"),
                ("CoinFlip", "🪙 Coin Flip"),
                ("DailySpin", "🎁 Daily Spin"),
                ("Slots", "🎰 Slots"),
                ("Roulette", "🎡 Roulette"),
                ("HighLow", "♥️ High / Low"),
                ("Economy", "💰 Balance / Economy"),
            ),
        ),
        (
            "━━ 🎮  PARTY GAMES  ━━",
            (
                ("Battleship", "🚢 Battleship"),
                ("Trivia", "❓ Trivia"),
            ),
        ),
        (
            "━━ 📊  GAME STATS  ━━",
            (
                ("PokéBot", "⚡ PokéBot"),
                (("FortniteStats", "Overwatch"), "🎮 Fortnite / Overwatch"),
                ("UFC", "🥊 UFC / Fight Night"),
            ),
        ),
        (
            "━━ 🔔  ALERTS  ━━",
            (("TwitchAlerts", "📺 Twitch Alerts"),),
        ),
    )

    def __init__(self, bot):
        self.bot = bot

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

            channel_ids: List[int] = []
            for target, allowed in rules.items():
                if str(target).lower() == "default" or allowed is not True:
                    continue
                try:
                    channel_ids.append(int(target))
                except (TypeError, ValueError):
                    continue

            parsed[str(cog_name)] = channel_ids

        return parsed

    @staticmethod
    def _channel_mentions(channel_ids: List[int]) -> str:
        if not channel_ids:
            return "*No channel configured*"
        return " ".join(f"<#{channel_id}>" for channel_id in channel_ids)

    @staticmethod
    def _combined_channels(rules: Dict[str, List[int]], cog_names) -> List[int]:
        """Combine channels for grouped cogs while preserving YAML order."""
        combined: List[int] = []
        for cog_name in cog_names:
            for channel_id in rules.get(cog_name, []):
                if channel_id not in combined:
                    combined.append(channel_id)
        return combined

    def build_embed(self) -> discord.Embed:
        rules = self._load_rules()

        embed = discord.Embed(
            title="📍 KrustyKrew Channel Guide",
            description=(
                "Each bot feature is locked to its own channel.\n"
                "Tap a channel below to jump straight there."
            ),
            color=discord.Color.blurple(),
        )

        for section_title, entries in self.SECTIONS:
            lines = []
            for cog_key, label in entries:
                if isinstance(cog_key, tuple):
                    present = [name for name in cog_key if name in rules]
                    if not present:
                        continue
                    channel_ids = self._combined_channels(rules, present)
                else:
                    if cog_key not in rules:
                        continue
                    channel_ids = rules[cog_key]

                lines.append(f"**{label}** → {self._channel_mentions(channel_ids)}")

            if lines:
                embed.add_field(
                    name=section_title,
                    value="\n".join(lines),
                    inline=False,
                )

        embed.set_footer(text="Wrong channel? The bot will simply ignore the command.")
        return embed

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.command()
    async def channelguide(self, ctx, channel: discord.TextChannel = None):
        """Post the compact channel guide here or in another channel."""
        target = channel or ctx.channel
        try:
            embed = self.build_embed()
        except RuntimeError as exc:
            await ctx.send(f"❌ ChannelGuide configuration error: {exc}")
            return

        await target.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if channel is not None:
            await ctx.tick()
