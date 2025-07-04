import discord
from redbot.core import commands
import wavelink


class AudioPlayer(commands.Cog):
    """Simple Lavalink-based audio player."""

    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.start_lavalink())

    async def start_lavalink(self):
        await self.bot.wait_until_ready()
        if not wavelink.NodePool.nodes:
            await wavelink.NodePool.create_node(
                bot=self.bot,
                host="127.0.0.1",
                port=2333,
                password="youshallnotpass",
                region="us_central",
            )

    @commands.command()
    async def join(self, ctx):
        """Join your voice channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You're not in a voice channel.")
        channel = ctx.author.voice.channel

        if ctx.voice_client:
            return await ctx.send("I'm already connected.")
        await channel.connect(cls=wavelink.Player)

    @commands.command()
    async def play(self, ctx, *, query: str):
        """Play a song from YouTube."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You're not in a voice channel.")

        if not ctx.voice_client:
            await ctx.invoke(self.join)

        player: wavelink.Player = ctx.voice_client
        if not player.is_connected():
            await player.connect(ctx.author.voice.channel)

        track = await wavelink.YouTubeTrack.search(query, return_first=True)

        if not track:
            return await ctx.send("No track found.")

        await player.play(track)
        await ctx.send(f"🎶 Now playing: **{track.title}**")

    @commands.command()
    async def leaveaudio(self, ctx):
        """Leave the voice channel."""
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("Disconnected.")
        else:
            await ctx.send("I'm not connected to a voice channel.")
