import discord
from redbot.core import commands, Config
from plexapi.server import PlexServer

class PlexStream(commands.Cog):
    """Control Plex playback via active Plex sessions."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xBEEFAB1E, force_registration=True)
        default_global = {
            "plex_url": "",
            "plex_token": "",
            "plex_client_name": None
        }
        self.config.register_global(**default_global)
        self.plex = None

    async def initialize_plex(self):
        settings = await self.config.all()
        if not settings["plex_url"] or not settings["plex_token"]:
            raise ValueError("Plex URL or token not set.")
        self.plex = PlexServer(settings["plex_url"], settings["plex_token"])

    async def get_client(self):
        client_name = await self.config.plex_client_name()
        if not client_name or not self.plex:
            return None
        # Search active sessions for matching player title
        for session in self.plex.sessions():
            if session.player.title == client_name:
                return session.player
        return None

    @commands.command()
    async def plexconfig(self, ctx, url: str, token: str):
        """Set Plex URL and token."""
        await self.config.plex_url.set(url)
        await self.config.plex_token.set(token)
        await ctx.send("✅ Plex URL and token saved. Run `[p]plexinit` to connect.")

    @commands.command()
    async def plexinit(self, ctx):
        """Initialize Plex connection."""
        try:
            await self.initialize_plex()
            await ctx.send("✅ Connected to Plex server.")
        except Exception as e:
            await ctx.send(f"❌ Could not connect to Plex server: `{e}`")

    @commands.command()
    async def plexsessions(self, ctx):
        """List active Plex sessions (players currently streaming)."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        sessions = self.plex.sessions()
        if not sessions:
            await ctx.send("No active Plex sessions detected.")
            return
        msg = "\n".join(
            f"{s.player.title} playing **{s.title}** (User: {', '.join(s.usernames)})"
            for s in sessions
        )
        await ctx.send(f"Active Plex sessions:\n{msg}")

    @commands.command()
    async def plexselect(self, ctx, *, client_name: str):
        """Select a Plex client/player to control (must be active session)."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        sessions = self.plex.sessions()
        valid_clients = [s.player.title for s in sessions]
        if client_name not in valid_clients:
            await ctx.send(f"Client `{client_name}` not found among active sessions. Use `[p]plexsessions` to see active clients.")
            return
        await self.config.plex_client_name.set(client_name)
        await ctx.send(f"✅ Plex client/player set to `{client_name}`")

    @commands.command()
    async def plexsearch(self, ctx, *, keyword: str):
        """Search Plex movies by keyword."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        try:
            section = self.plex.library.section("Movies")
            results = section.search(keyword)
            if not results:
                await ctx.send("No movies found.")
                return
            titles = "\n".join(f"- {movie.title}" for movie in results)
            await ctx.send(f"🎬 Search results for `{keyword}`:\n{titles}")
        except Exception as e:
            await ctx.send(f"❌ Search failed: `{e}`")

    @commands.command()
    async def plexplay(self, ctx, *, title: str):
        """Play a movie on the selected Plex client/player."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        client = await self.get_client()
        if not client:
            await ctx.send("No Plex client/player selected or active. Use `[p]plexselect` after confirming active sessions.")
            return
        try:
            section = self.plex.library.section("Movies")
            movie = section.get(title)
            client.proxyThroughServer()
            client.playMedia(movie)
            await ctx.send(f"▶️ Now playing **{movie.title}** on client `{client.title}`")
        except Exception as e:
            await ctx.send(f"❌ Could not play movie: `{e}`")

    @commands.command()
    async def plexpause(self, ctx):
        """Pause playback on selected client/player."""
        client = await self.get_client()
        if not client:
            await ctx.send("No Plex client/player selected or active.")
            return
        try:
            client.pause()
            await ctx.send("⏸️ Playback paused.")
        except Exception as e:
            await ctx.send(f"❌ Could not pause playback: `{e}`")

    @commands.command()
    async def plexresume(self, ctx):
        """Resume playback on selected client/player."""
        client = await self.get_client()
        if not client:
            await ctx.send("No Plex client/player selected or active.")
            return
        try:
            client.play()
            await ctx.send("▶️ Playback resumed.")
        except Exception as e:
            await ctx.send(f"❌ Could not resume playback: `{e}`")

    @commands.command()
    async def plexstop(self, ctx):
        """Stop playback on selected client/player."""
        client = await self.get_client()
        if not client:
            await ctx.send("No Plex client/player selected or active.")
            return
        try:
            client.stop()
            await ctx.send("⏹️ Playback stopped.")
        except Exception as e:
            await ctx.send(f"❌ Could not stop playback: `{e}`")
