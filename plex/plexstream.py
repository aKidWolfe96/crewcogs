import discord
import requests
from redbot.core import commands, Config
from plexapi.server import PlexServer
from plexapi.mixins import PosterUrlMixin

class PlexStream(commands.Cog):
    """Search and display information from your Plex library."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_guild = {
            "base_url": None,
            "plex_token": None
        }
        self.config.register_guild(**default_guild)
        self.plex = None

    async def initialize_plex(self, guild):
        settings = await self.config.guild(guild).all()
        if not all([settings["base_url"], settings["plex_token"]]):
            return False
        try:
            self.plex = PlexServer(settings["base_url"], settings["plex_token"])
            return True
        except Exception:
            return False

    @commands.command()
    async def plexconfig(self, ctx, base_url: str, plex_token: str):
        """Configure Plex server connection."""
        await self.config.guild(ctx.guild).base_url.set(base_url)
        await self.config.guild(ctx.guild).plex_token.set(plex_token)
        if await self.initialize_plex(ctx.guild):
            await ctx.send("✅ Plex configuration saved and connection established.")
        else:
            await ctx.send("⚠️ Configuration saved, but connection to Plex failed.")

    @commands.command()
    async def plexsearch(self, ctx, *, keyword):
        """Search for Plex movies by keyword."""
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized.")
            return
        try:
            movies = self.plex.library.section('Movies').search(keyword)
            if not movies:
                await ctx.send("❌ No results found.")
                return
            results = "\n".join([f"• {m.title}" for m in movies[:10]])
            await ctx.send(f"🎬 **Search results for:** `{keyword}`\n{results}")
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.command()
    async def plexsearchinfo(self, ctx, *, keyword):
        """Search Plex and show detailed info about the top result."""
        if not await self.initialize_plex(ctx.guild):
            await ctx.send("❌ Plex not initialized.")
            return
        try:
            results = self.plex.library.section('Movies').search(keyword)
            if not results:
                await ctx.send("❌ No results found.")
                return

            movie = results[0]
            duration = int(movie.duration / 60000) if movie.duration else "N/A"
            poster_url = PosterUrlMixin.thumbUrl.fget(movie)

            embed = discord.Embed(
                title=f"{movie.title} ({movie.year})",
                description=movie.summary or "No description available.",
                color=discord.Color.orange()
            )
            embed.set_footer(text=f"{movie.studio or 'Unknown Studio'} - {duration} min")

            if poster_url:
                img_data = requests.get(poster_url).content
                with open('/tmp/movie.jpg', 'wb') as handler:
                    handler.write(img_data)
                file = discord.File('/tmp/movie.jpg', filename='movie.jpg')
                embed.set_image(url="attachment://movie.jpg")
                await ctx.send(file=file, embed=embed)
            else:
                await ctx.send(embed=embed)

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
