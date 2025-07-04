import discord
from redbot.core import commands
import wavelink


class AudioPlayer(commands.Cog):
    """Simple Lavalink-based audio player."""

    def __init__(self, bot):
        self.bot = bot
        self.node_ready = False
        self.bot.loop.create_task(self.start_lavalink())

    async def start_lavalink(self):
        await self.bot.wait_until_ready()
        try:
            if not wavelink.NodePool.nodes:
                await wavelink.NodePool.create_node(
                    bot=self.bot,
                    host="127.0.0.1",
                    port=2333,
                    password="youshallnotpass",
                    region="us_central",
                )
            self.node_ready = True
            print("[AudioPlayer] Lavalink node connected successfully.")
        except Exception as e:
            print(f"[AudioPlayer] Lavalink connection failed: {e}")

    @commands.command()
    async def join(self, ctx):
        """Join your voice channel."""
        if not self.node_ready:
            return await ctx.send("❌ Lavalink node is not ready. Try again shortly.")

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You're not in a voice channel.")

        if ctx.voice_client:
            return await ctx.send("I'm already connected to a channel.")

        channel = ctx.author.voice.channel
        await channel.connect(cls=wavelink.Player)

    @commands.command()
    async def play(self, ctx, *, query: str):
        """Play a song from YouTube."""
        if not self.node_ready:
            return await ctx.send("❌ Lavalink node is not ready. Try again shortly.")

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You're not in a voice channel.")

        if not ctx.voice_client:
            await ctx.invoke(self.join)

        player: wavelink.Player = ctx.voice_client

        try:
            track = await wavelink.YouTubeTrack.search(query, return_first=True)
        except Exception as e:
            return await ctx.send(f"Search failed: {e}")

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
