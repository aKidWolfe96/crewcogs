import discord
import json
import os
from redbot.core import commands, Config
from plexapi.server import PlexServer

class PlexStream(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=733019384, force_registration=True)
        default_user = {
            "baseurl": "",
            "token": "",
        }
        self.config.register_global(**default_user)
        self.plex = None

    @commands.command()
    async def plexconfig(self, ctx, baseurl: str, token: str):
        """Set Plex server config."""
        await self.config.baseurl.set(baseurl)
        await self.config.token.set(token)
        await ctx.send("✅ Plex configuration saved. Use `[p]plexinit` to connect.")

    @commands.command()
    async def plexinit(self, ctx):
        """Initialize Plex connection."""
        baseurl = await self.config.baseurl()
        token = await self.config.token()
        try:
            self.plex = PlexServer(baseurl, token)
            await ctx.send(f"✅ Connected to Plex: `{self.plex.friendlyName}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to connect: `{e}`")

    @commands.command()
    async def plexplay(self, ctx, *, title: str):
        """Send movie playback command to StreamBot."""
        if not self.plex:
            await ctx.send("⚠️ Plex not initialized. Run `[p]plexinit` first.")
            return
        try:
            section = self.plex.library.section("Movies")
            movie = section.get(title)
            data = {
                "title": movie.title,
                "key": movie.key,
                "duration": movie.duration,
                "summary": movie.summary,
                "stream_url": f"{self.plex._baseurl}{movie.key}?X-Plex-Token={self.plex._token}"
            }

            # Save to shared file (for streambot.py to read)
            with open("stream_command.json", "w") as f:
                json.dump(data, f)

            await ctx.send(f"🎬 `{movie.title}` sent to StreamBot for playback.")
        except Exception as e:
            await ctx.send(f"❌ Could not find or send movie: `{e}`")
