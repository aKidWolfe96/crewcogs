import discord
from redbot.core import commands, Config
from plexapi.server import PlexServer

class PlexStream(commands.Cog):
    """Stream and control your Plex media via Plex clients."""

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
        settings = await self.config.all()
        client_name = settings["plex_client_name"]
        if not client_name:
            return None
        for client in self.plex.clients():
            if client.title == client_name:
                return client
        return None

    @commands.command()
    async def plexconfig(self, ctx, url: str, token: str):
        """Set Plex URL and token."""
        await self.config.plex_url.set(url)
        await self.config.plex_token.set(token)
        await ctx.send("✅ Plex URL and token saved. Run `[p]plexinit` to connect.")

    @commands.command()
    async def plexinit(self, ctx):
        """Initialize connection to Plex server."""
        try:
            await self.initialize_plex()
            await ctx.send("✅ Connected to Plex server.")
        except Exception as e:
            await ctx.send(f"❌ Could not connect to Plex server: `{e}`")

    @commands.command()
    async def plexclients(self, ctx):
        """List available Plex clients."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        clients = [client.title for client in self.plex.clients()]
        if not clients:
            await ctx.send("No Plex clients currently found.")
            return
        await ctx.send("Available Plex clients:\n" + "\n".join(clients))

    @commands.command()
    async def plexselect(self, ctx, *, client_name: str):
        """Select a Plex client to control."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        clients = [client.title for client in self.plex.clients()]
        if client_name not in clients:
            await ctx.send(f"Client `{client_name}` not found. Use `[p]plexclients` to see available clients.")
            return
        await self.config.plex_client_name.set(client_name)
        await ctx.send(f"✅ Plex client set to `{client_name}`")

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
        """Play a movie on the selected Plex client."""
        if not self.plex:
            await ctx.send("Plex not initialized. Run `[p]plexinit` first.")
            return
        client = await self.get_client()
        if not client:
            await ctx.send("No Plex client selected. Use `[p]plexselect` to select one.")
            return
        try:
            section = self.plex.library.section("Movies")
            movie = section.get(title)
            client.proxyThroughServer()
            client.playMedia(movie)
            await ctx.send(f"▶️ Now playing **{movie.title}** on client `{client.title}`")
        except Exception as e:
            await ctx.send(f"❌ Could not play movie: `{e}`")

    # Optional: Add pause, resume, stop commands similarly
