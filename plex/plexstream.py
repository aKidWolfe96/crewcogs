import discord
from redbot.core import commands, Config
from plexapi.server import PlexServer

import requests
import os

class PlexStream(commands.Cog):
    """Stream and control your Plex media directly from Discord."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xBEEFAB1E, force_registration=True)
        default_global = {
            "plex_url": "http://localhost:32400",
            "plex_token": "CHANGEME",
            "plex_client_name": "CHANGEME"
        }
        self.config.register_global(**default_global)

        # Placeholder until ready
        self.plex = None
        self.client = None

    async def initialize_plex(self):
        settings = await self.config.all()
        self.plex = PlexServer(settings["plex_url"], settings["plex_token"])
        for c in self.plex.clients():
            if c.title == settings["plex_client_name"]:
                self.client = c
                break

    @commands.command()
    async def plexconfig(self, ctx, url: str, token: str, client_name: str):
        """Set Plex URL, token, and target client name."""
        await self.config.plex_url.set(url)
        await self.config.plex_token.set(token)
        await self.config.plex_client_name.set(client_name)
        await ctx.send("✅ Plex configuration saved. Use `[p]plexinit` to connect.")

    @commands.command()
    async def plexinit(self, ctx):
        """Manually initialize Plex connection and load client."""
        try:
            await self.initialize_plex()
            if self.client:
                await ctx.send(f"✅ Connected to Plex client: **{self.client.title}**")
            else:
                await ctx.send("⚠️ Could not find the specified Plex client.")
        except Exception as e:
            await ctx.send(f"❌ Failed to connect: `{e}`")

    @commands.command()
    async def plexsearch(self, ctx, *, keyword: str):
        """Search Plex for movie titles containing a keyword."""
        if not self.plex:
            await ctx.send("❌ Plex not initialized. Run `[p]plexinit` first.")
            return
        section = self.plex.library.section("Movies")
        results = section.search(keyword)
        if not results:
            await ctx.send("No movies found.")
            return
        titles = "\n".join([f"- {movie.title}" for movie in results])
        await ctx.send(f"🎬 **Results for** `{keyword}`:\n{titles}")

    @commands.command()
    async def plexplay(self, ctx, *, title: str):
        """Play a movie by title."""
        if not self.plex or not self.client:
            await ctx.send("❌ Plex not initialized. Run `[p]plexinit` first.")
            return
        try:
            section = self.plex.library.section("Movies")
            movie = section.get(title)
            self.client.proxyThroughServer()
            self.client.playMedia(movie)
            await ctx.send(f"▶️ Now playing: **{movie.title}**")
        except Exception as e:
            await ctx.send(f"❌ Could not play movie: `{e}`")
