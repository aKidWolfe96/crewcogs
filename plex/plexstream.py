from redbot.core import commands, Config, checks
import discord
from plexapi.server import PlexServer
from plexapi.mixins import PosterUrlMixin
import random
import requests
from io import BytesIO

class PlexStream(commands.Cog):
    """Plex control cog with slash commands for Red Discord Bot."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890123456)
        default_global = {
            "baseurl": None,
            "plextoken": None,
            "system": None,
            "voicechannel": None,
        }
        self.config.register_global(**default_global)
        self.plex = None
        self.movielist = []
        self.savemovietitle = None
        self.p = 0
        self.r = 0

    async def initialize_plex(self):
        baseurl = await self.config.baseurl()
        token = await self.config.plextoken()
        if baseurl and token:
            self.plex = PlexServer(baseurl, token)
            movies = self.plex.library.section('Movies')
            self.movielist = [video.title for video in movies.search()]
        else:
            self.plex = None

    @commands.is_owner()
    @commands.command()
    async def plexconfig(self, ctx, baseurl: str, plextoken: str, system: str, voicechannel: int):
        """Configure Plex connection details."""
        await self.config.baseurl.set(baseurl)
        await self.config.plextoken.set(plextoken)
        await self.config.system.set(system)
        await self.config.voicechannel.set(voicechannel)
        await self.initialize_plex()
        await ctx.send("✅ Plex configuration saved and Plex initialized.")

    @commands.command()
    async def plexclients(self, ctx):
        """List available Plex clients."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        clients = self.plex.clients()
        if not clients:
            await ctx.send("No Plex clients found.")
            return
        msg = "**Available Plex Clients:**\n"
        for c in clients:
            msg += f"• {c.title} ({c.product})\n"
        await ctx.send(msg)

    @commands.command()
    async def plexsearch(self, ctx, *, keyword: str):
        """Search for movies with a keyword."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            movies = self.plex.library.section('Movies')
            results = [video.title for video in movies.search(keyword)]
            if not results:
                return await ctx.send("No movies found.")
            await ctx.send(f"🎬 Search results for **{keyword}**:\n" + "\n".join(results))
        except Exception:
            await ctx.send("Error searching movies.")

    @commands.command()
    async def plexinfo(self, ctx, *, movie: str):
        """Get info about a movie."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            play = self.plex.library.section('Movies').get(movie)
            duration = int(play.duration / 60000)
            image_url = PosterUrlMixin.thumbUrl.fget(play)
            embed = discord.Embed(title=f"Info for: {movie}", description=play.summary, color=0xf5dd03)
            embed.add_field(name="Rotten Tomatoes Rating", value=str(play.audienceRating))
            embed.add_field(name="Content Rating", value=str(play.contentRating))
            embed.add_field(name="Duration", value=f"{duration} minutes")
            embed.set_footer(text=f"{play.year} - {play.studio}")
            # Get image bytes
            resp = requests.get(image_url)
            image_bytes = BytesIO(resp.content)
            image_bytes.seek(0)
            file = discord.File(fp=image_bytes, filename="movie.jpg")
            embed.set_image(url="attachment://movie.jpg")
            await ctx.send(file=file, embed=embed)
        except Exception:
            await ctx.send(f"Couldn't find movie: {movie}")

    @commands.command()
    async def plexplay(self, ctx, *, movie: str):
        """Play a movie on the Plex client."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            system = await self.config.system()
            play = self.plex.library.section('Movies').get(movie)
            client = self.plex.client(system)
            client.proxyThroughServer()
            client.playMedia(play)
            client.setParameters(volume=100, shuffle=0, repeat=0)
            self.savemovietitle = movie
            duration = int(play.duration / 60000)
            image_url = PosterUrlMixin.thumbUrl.fget(play)
            embed = discord.Embed(title=f"Playing: {movie}", description=play.summary, color=0xf5dd03)
            embed.add_field(name="Rotten Tomatoes Rating", value=str(play.audienceRating))
            embed.add_field(name="Content Rating", value=str(play.contentRating))
            embed.add_field(name="Duration", value=f"{duration} minutes")
            embed.set_footer(text=f"{play.year} - {play.studio}")
            resp = requests.get(image_url)
            image_bytes = BytesIO(resp.content)
            image_bytes.seek(0)
            file = discord.File(fp=image_bytes, filename="movie.jpg")
            embed.set_image(url="attachment://movie.jpg")
            await ctx.send(file=file, embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Could not play movie: {e}")

    @commands.command()
    async def plexstop(self, ctx):
        """Stop playback on the Plex client."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            system = await self.config.system()
            client = self.plex.client(system)
            client.proxyThroughServer()
            client.stop()
            await ctx.send(f"⏹️ Stopped playing: {self.savemovietitle}")
        except Exception:
            await ctx.send("Error stopping playback.")

    @commands.command()
    async def plexpause(self, ctx):
        """Pause playback."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            system = await self.config.system()
            client = self.plex.client(system)
            client.proxyThroughServer()
            client.pause()
            await ctx.send(f"⏸️ Paused: {self.savemovietitle}")
        except Exception:
            await ctx.send("Error pausing playback.")

    @commands.command()
    async def plexresume(self, ctx):
        """Resume playback."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            system = await self.config.system()
            client = self.plex.client(system)
            client.proxyThroughServer()
            client.play()
            await ctx.send(f"▶️ Resumed: {self.savemovietitle}")
        except Exception:
            await ctx.send("Error resuming playback.")

    @commands.command()
    async def plexshuffle(self, ctx):
        """Play a random movie."""
        if not self.plex:
            await ctx.send("Plex server not configured.")
            return
        try:
            rc = random.choice(self.movielist)
            system = await self.config.system()
            play = self.plex.library.section('Movies').get(rc)
            client = self.plex.client(system)
            client.proxyThroughServer()
            client.playMedia(play)
            client.setParameters(volume=100, shuffle=0, repeat=0)
            self.savemovietitle = rc
            duration = int(play.duration / 60000)
            image_url = PosterUrlMixin.thumbUrl.fget(play)
            embed = discord.Embed(title=f"Playing: {rc}", description=play.summary, color=0xf5dd03)
            embed.add_field(name="Rotten Tomatoes Rating", value=str(play.audienceRating))
            embed.add_field(name="Content Rating", value=str(play.contentRating))
            embed.add_field(name="Duration", value=f"{duration} minutes")
            embed.set_footer(text=f"{play.year} - {play.studio}")
            resp = requests.get(image_url)
            image_bytes = BytesIO(resp.content)
            image_bytes.seek(0)
            file = discord.File(fp=image_bytes, filename="movie.jpg")
            embed.set_image(url="attachment://movie.jpg")
            await ctx.send(file=file, embed=embed)
        except Exception:
            await ctx.send("Error playing random movie.")

def setup(bot):
    bot.add_cog(PlexStream(bot))
