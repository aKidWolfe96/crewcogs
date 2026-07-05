import discord
from redbot.core import commands


class ChannelGuide(commands.Cog):
    """Posts a KrustyKrew channel guide showing which commands go where."""

    def __init__(self, bot):
        self.bot = bot

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📍 KrustyKrew Channel Guide",
            description=(
                "Each bot feature is locked to its own channel. "
                "Use commands in the right spot — anywhere else and the bot stays quiet.\n"
                "Tap any channel below to jump straight there."
            ),
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="🎰 Casino & Economy",
            value=(
                "**Blackjack** → <#1480285772849352786>\n"
                "**Coin Flip** → <#1499218723368734831>\n"
                "**Daily Spin** → <#1480285926151160000>\n"
                "**Horse Race** → <#1480285845645955254>\n"
                "**Slots** → <#1493785521317478430>\n"
                "**Balance / Economy** → <#1480285926151160000>\n"
                "**Leaderboard** → works in any casino channel above"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎮 Party Games",
            value=(
                "**Battleship** → <#1496308219172356207>\n"
                "**Trivia** → <#1496308508247855134>"
            ),
            inline=False,
        )

        embed.add_field(
            name="📊 Game Stats",
            value=(
                "**PokéBot** → <#1492801872262725813> <#1480479406626570250>\n"
                "**Fortnite** → <#1493752940912050296> <#1350546941766930576>\n"
                "**Overwatch** → <#1493752940912050296> <#1350546941766930576>\n"
                "**UFC / Fight Night** → <#1515373883849572592> <#1492931006028709948>"
            ),
            inline=False,
        )

        embed.add_field(
            name="🔔 Alerts",
            value="**Twitch** → <#1493752940912050296>",
            inline=False,
        )

        embed.set_footer(text="Wrong channel? The bot will just ignore the command.")
        return embed

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.command()
    async def channelguide(self, ctx, channel: discord.TextChannel = None):
        """Post the channel guide embed.

        Run it plain to post here, or pass a channel: [p]channelguide #help
        """
        target = channel or ctx.channel
        await target.send(embed=self.build_embed())
        if channel is not None:
            await ctx.tick()  # react to confirm when posting elsewhere


async def setup(bot):
    await bot.add_cog(ChannelGuide(bot))
