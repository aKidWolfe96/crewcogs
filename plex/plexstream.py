import discord
import random
import requests
from redbot.core import commands, Config
from plexapi.server import PlexServer
from plexapi.mixins import PosterUrlMixin

class PlexStream(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_guild = {
            "base_url": None,
            "plex_token": None,
            "machine_id": None,
            "voice_channel": None
        }
        self.config.register_guild(**default_guild)
        self.plex = None
        self.client = None
        self.savemovietitle = None

    async def initialize_plex(self, guild):
        settings = await self.config.guild(guild).all()
        if not all([settings["base_url"], settings["plex_token"], settings["machine_id"]]):
            return False
        self.plex = PlexServer(settings["base_url"], settings["plex_token"])
        for c in self.plex.clients():
            if c.machineIdentifier == settings["machine_id"]:
                self.client = c
                break
        return self.client is not None

    @commands.command()
    async def plexconfig(self, ctx, base_url: str, plex_token: str, machine_id: str, voice_channel_id: int):
        await self.config.guild(ctx.guild).base_url.set(base_url)
        await self.config.guild(ctx.guild).plex_token.set(plex_token)
        await self.config.guild(ctx.guild).machine_id.set(machine_id)
        await self.config.guild(ctx.guild).voice_channel.set(voice_channel_id)
        initialized = await self.initialize_plex(ctx.guild)
        if initialized:
            await ctx.send("✅ Plex configuration saved and Plex initialized.")
        else:
            await ctx.send("⚠️ Plex configuration saved, but could not find the specified Plex client.")

    @commands.command()
    async def plexsearch(self, ctx, *, keyword):
        await self.initialize_plex(ctx.guild)
        try:
            movies = self.plex.library.section('Movies').search(keyword)
            titles = [m.title for m in movies]
            if not titles:
                await ctx.send("❌ No results found.")
                return
            results = "\n".join(titles)
            await ctx.send(f"""🎬 Search results for {keyword}:
{results}""")
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.command()
    async def plexplay(self, ctx, *, movie_title):
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized correctly.")
            return
        try:
            self.savemovietitle = movie_title
            play = self.plex.library.section('Movies').get(movie_title)
            self.client.proxyThroughServer()
            self.client.playMedia(play)
            self.client.setParameters(volume=100, shuffle=0, repeat=0)
            duration = int(play.duration / 60000)
            image = PosterUrlMixin.thumbUrl.fget(play)
            img_data = requests.get(image).content
            with open('/tmp/movie.jpg', 'wb') as handler:
                handler.write(img_data)
            file = discord.File('/tmp/movie.jpg', filename='movie.jpg')
            embed = discord.Embed(title=f"Playing: {movie_title}", description=play.summary, color=0xf5dd03)
            embed.set_image(url="attachment://movie.jpg")
            embed.set_footer(text=f"{play.year} - {play.studio} - {duration} Minutes")
            await ctx.send(file=file, embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Could not play movie: {str(e)}")

    @commands.command()
    async def plexpause(self, ctx):
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized correctly.")
            return
        try:
            self.client.proxyThroughServer()
            self.client.pause()
            await ctx.send(f"⏸️ Paused: {self.savemovietitle}")
        except Exception as e:
            await ctx.send(f"❌ Could not pause movie: {str(e)}")

    @commands.command()
    async def plexresume(self, ctx):
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized correctly.")
            return
        try:
            self.client.proxyThroughServer()
            self.client.play()
            await ctx.send(f"▶️ Resumed: {self.savemovietitle}")
        except Exception as e:
            await ctx.send(f"❌ Could not resume movie: {str(e)}")

    @commands.command()
    async def plexstop(self, ctx):
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized correctly.")
            return
        try:
            self.client.proxyThroughServer()
            self.client.stop()
            await ctx.send(f"⏹️ Stopped: {self.savemovietitle}")
        except Exception as e:
            await ctx.send(f"❌ Could not stop movie: {str(e)}")

    @commands.command()
    async def plexshuffle(self, ctx):
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized correctly.")
            return
        try:
            section = self.plex.library.section('Movies')
            all_movies = section.all()
            movie = random.choice(all_movies)
            self.savemovietitle = movie.title
            self.client.proxyThroughServer()
            self.client.playMedia(movie)
            self.client.setParameters(volume=100, shuffle=0, repeat=0)
            duration = int(movie.duration / 60000)
            image = PosterUrlMixin.thumbUrl.fget(movie)
            img_data = requests.get(image).content
            with open('/tmp/movie.jpg', 'wb') as handler:
                handler.write(img_data)
            file = discord.File('/tmp/movie.jpg', filename='movie.jpg')
            embed = discord.Embed(title=f"Randomly Playing: {movie.title}", description=movie.summary, color=0xf5dd03)
            embed.set_image(url="attachment://movie.jpg")
            embed.set_footer(text=f"{movie.year} - {movie.studio} - {duration} Minutes")
            await ctx.send(file=file, embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Could not play random movie: {str(e)}")
