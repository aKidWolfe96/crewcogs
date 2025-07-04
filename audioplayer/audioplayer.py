import discord
from redbot.core import commands
import wavelink

class AudioPlayer(commands.Cog):
    """Simple Lavalink-based audio player."""

    def __init__(self, bot):
        self.bot = bot
        bot.loop.create_task(self.start_lavalink())

    async def start_lavalink(self):
        await self.bot.wait_until_ready()
        if not wavelink.NodePool.nodes:
            await wavelink.NodePool.create_node(
                bot=self.bot,
                host='127.0.0.1',
                port=2333,
                password='youshallnotpass',
                region='us_central'
            )

    @commands.command()
    async def join(self, ctx):
        """Bot joins your voice channel."""
        if not ctx.author.voice:
            return await ctx.send("You're not in a voice channel.")
        vc = ctx.author.voice.channel
        await vc.connect(cls=wavelink.Player)

    @commands.command()
    async def play(self, ctx, *, query: str):
        """Search and play a track from YouTube."""
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        player: wavelink.Player = ctx.voice_client
        track = await wavelink.YouTubeTrack.search(query, return_first=True)
        await player.play(track)
        await ctx.send(f"🎵 Now playing: **{track.title}**")

    @commands.command()
    async def leaveaudio(self, ctx):
        """Disconnect from the voice channel."""
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("Disconnected.")
