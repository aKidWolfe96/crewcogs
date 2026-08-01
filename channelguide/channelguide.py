from pathlib import Path
from typing import Dict, List

import discord
import yaml
from redbot.core import commands

__red_end_user_data_statement__ = "This cog does not store any end user data."


class ChannelGuide(commands.Cog):
    """Posts a command guide generated from the server permissions YAML."""

    CONFIG_FILE = Path(__file__).with_name("config.yaml")

    # Only entries present in config.yaml are shown. This catalog controls the
    # friendly names, grouping, and concise public command examples.
    CATALOG = {
        "Casino": {
            "title": "Casino Hub",
            "emoji": "🎰",
            "group": "casino",
            "commands": (
                "`{p}casino` • `{p}casino profile` • `{p}casino achievements`\n"
                "`{p}casino challenges` • `{p}casino title` • `{p}casinoboard`"
            ),
        },
        "Blackjack": {
            "title": "Blackjack",
            "emoji": "🃏",
            "group": "casino",
            "commands": "`{p}blackjack <bet>` • `{p}bjstats`",
        },
        "Battleship": {
            "title": "Battleship",
            "emoji": "🚢",
            "group": "games",
            "commands": "`{p}battleship`",
        },
        "Trivia": {
            "title": "Trivia",
            "emoji": "🧠",
            "group": "games",
            "commands": "`{p}trivia`",
        },
        "CoinFlip": {
            "title": "Coin Flip",
            "emoji": "🪙",
            "group": "casino",
            "commands": "`{p}coinflip <heads|tails> <bet>` • `{p}cfstats`",
        },
        "DailySpin": {
            "title": "Daily Spin",
            "emoji": "🎁",
            "group": "casino",
            "commands": "`{p}dailyspin`",
        },
        "FortniteStats": {
            "title": "Fortnite",
            "emoji": "🟦",
            "group": "stats",
            "commands": (
                "`{p}fn stats [name]` • `{p}fn season [name]`\n"
                "`{p}fn shop` • `{p}fn news` • `{p}fn cosmetic <name>`"
            ),
        },
        "HorseRace": {
            "title": "Horse Race",
            "emoji": "🐎",
            "group": "casino",
            "commands": "`{p}horserace <bet>`",
        },
        "Overwatch": {
            "title": "Overwatch",
            "emoji": "🟧",
            "group": "stats",
            "commands": (
                "`{p}ow profile` • `{p}ow tracker`\n"
                "`{p}ow challenge list` • `{p}ow challenge mine`"
            ),
        },
        "PokéBot": {
            "title": "PokéBot",
            "emoji": "⚡",
            "group": "pokemon",
            "commands": (
                "`{p}start` • `{p}pokehelp` • `{p}pokemon` • `{p}party`\n"
                "`{p}catch` • `{p}battle @user` • `{p}trade @user ...`\n"
                "`{p}inventory` • `{p}shop` • `{p}pokestop` • `{p}raidstatus`"
            ),
        },
        "Slots": {
            "title": "Slots",
            "emoji": "🎰",
            "group": "casino",
            "commands": "`{p}slots <bet>` • `{p}slotstats` • `{p}slotpayouts`",
        },
        "UFC": {
            "title": "UFC / Fight Night",
            "emoji": "🥊",
            "group": "stats",
            "commands": (
                "`{p}ufc card` • `{p}ufc results` • `{p}ufc fighter <name>`\n"
                "`{p}ufc pick <fighter>` • `{p}ufc bet <amount> <fighter>`\n"
                "`{p}ufc bets` • `{p}ufc picks` • `{p}ufc standings`"
            ),
        },
        "TwitchAlerts": {
            "title": "Twitch Alerts",
            "emoji": "🔔",
            "group": "alerts",
            "commands": "Automatic live notifications are posted here.",
        },
        "Economy": {
            "title": "Economy & Free Credits",
            "emoji": "💰",
            "group": "casino",
            "commands": (
                "`{p}balance` • `{p}payday` • `{p}daily`\n"
                "`{p}scratch` • `{p}casino claim`"
            ),
        },
        "Roulette": {
            "title": "Krew Roulette",
            "emoji": "🎡",
            "group": "casino",
            "commands": "`{p}roulette <bet> <choice>` • `{p}roulettebets` • `{p}roulettestats`",
        },
        "HighLow": {
            "title": "High / Low",
            "emoji": "🔺",
            "group": "casino",
            "commands": "`{p}highlow <bet>` • `{p}highlowpayouts`",
        },
    }

    GROUPS = (
        ("casino", "🎰 Ruthless Dealer • Casino & Economy", discord.Color.gold()),
        ("pokemon", "⚡ PokéBot", discord.Color.red()),
        ("stats", "🎮 Game Stats & Fight Night", discord.Color.green()),
        ("games", "🎲 Party Games", discord.Color.blurple()),
        ("alerts", "🔔 Automatic Alerts", discord.Color.purple()),
    )

    def __init__(self, bot):
        self.bot = bot

    def _load_rules(self) -> Dict[str, List[int]]:
        """Read allowed channel IDs from the bundled Red Permissions YAML."""
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

            # Presence in the YAML controls inclusion, even if no channel rule
            # is currently enabled.
            parsed[str(cog_name)] = channel_ids

        return parsed

    @staticmethod
    def _channels(channel_ids: List[int]) -> str:
        if not channel_ids:
            return "*No allowed channel configured*"
        return " ".join(f"<#{channel_id}>" for channel_id in channel_ids)

    def build_embeds(self, prefix: str) -> List[discord.Embed]:
        rules = self._load_rules()
        included = [name for name in rules if name in self.CATALOG]

        overview = discord.Embed(
            title="🧭 KrustyKrew Command Guide",
            description=(
                "This guide is generated from the same YAML used for Red's Permissions cog. "
                "Only configured features are shown. Tap a channel mention to jump there.\n\n"
                f"**Current prefix:** `{prefix}`"
            ),
            color=discord.Color.blurple(),
        )
        overview.set_author(name="KrustyKrew • Command Center")

        for name in included:
            item = self.CATALOG[name]
            overview.add_field(
                name=f"{item['emoji']} {item['title']}",
                value=self._channels(rules[name]),
                inline=True,
            )

        overview.set_footer(text="The command cards below follow your permissions YAML.")
        embeds: List[discord.Embed] = [overview]

        for group_key, title, color in self.GROUPS:
            group_items = [
                (name, self.CATALOG[name])
                for name in included
                if self.CATALOG[name]["group"] == group_key
            ]
            if not group_items:
                continue

            embed = discord.Embed(title=title, color=color)
            for name, item in group_items:
                channels = self._channels(rules[name])
                commands_text = item["commands"].format(p=prefix)
                embed.add_field(
                    name=f"{item['emoji']} {item['title']}  •  {channels}",
                    value=commands_text,
                    inline=False,
                )
            embeds.append(embed)

        return embeds

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.command()
    async def channelguide(self, ctx, channel: discord.TextChannel = None):
        """Post the YAML-powered command guide here or in another channel."""
        target = channel or ctx.channel
        try:
            embeds = self.build_embeds(ctx.clean_prefix)
        except RuntimeError as exc:
            await ctx.send(f"❌ ChannelGuide configuration error: {exc}")
            return

        await target.send(
            embeds=embeds,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if channel is not None:
            await ctx.tick()
