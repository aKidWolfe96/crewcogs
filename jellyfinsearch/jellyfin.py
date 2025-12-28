from redbot.core import commands, Config
import aiohttp
import urllib.parse
import discord

class JellyfinMultiView(discord.ui.View):
    """View that generates a unique button for every search result"""
    def __init__(self, app_id: str, items: list):
        super().__init__(timeout=180)
        # Create a button for each result (up to 5)
        for i, item in enumerate(items, 1):
            name = item.get('Name')
            # The label is "Watch [Movie Name]" or just the number to keep it clean
            self.add_item(discord.ui.Button(
                label=f"Watch #{i}",
                url=f"https://discord.com/activities/{app_id}",
                style=discord.ButtonStyle.link,
                emoji=f"{i}\N{COMBINING ENCLOSING KEYCAP}"
            ))

class JellyfinSearch(commands.Cog):
    """Customized Jellyfin search with individual launch buttons"""

    def __init__(self, bot):
        self.bot = bot
        self.app_id = "1385831372643373119"
        self.config = Config.get_conf(self, identifier=856712356)
        default_global = {"base_url": None, "api_key": None}
        self.config.register_global(**default_global)

    def format_runtime(self, ticks):
        if not ticks: return "N/A"
        minutes = int(ticks / 600000000)
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if h > 0 else f"{m}m"

    @commands.command(name="searchj")
    async def searchj(self, ctx, *, query: str):
        """Search with unique buttons and extra metadata"""
        base_url = await self.config.base_url()
        api_key = await self.config.api_key()
        
        if not base_url or not api_key:
            return await ctx.send("Please set the URL and API key first.")

        # Clean URL and set params
        clean_url = base_url.rstrip('/')
        search_url = f"{clean_url}/Items"
        params = {
            "searchTerm": query,
            "IncludeItemTypes": "Movie,Series",
            "Recursive": "true",
            "Limit": 5,
            "Fields": "Genres,CommunityRating,RunTimeTicks", # Request extra data
            "api_key": api_key
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, params=params) as resp:
                if resp.status != 200:
                    return await ctx.send(f"Server Error: {resp.status}")
                
                data = await resp.json()
                items = data.get('Items', [])
                if not items:
                    return await ctx.send("Nothing found.")

                embed = discord.Embed(
                    title=f"🎬 Results for: {query}",
                    description="Select a movie to launch the Discord Activity theater.",
                    color=discord.Color.from_rgb(0, 164, 220) # Jellyfin Blue
                )

                # Use the first result's poster as the main image
                top_id = items[0].get('Id')
                embed.set_thumbnail(url=f"{clean_url}/Items/{top_id}/Images/Primary?api_key={api_key}")

                for i, item in enumerate(items, 1):
                    name = item.get('Name')
                    year = item.get('ProductionYear', 'N/A')
                    rating = item.get('CommunityRating', 'N/A')
                    runtime = self.format_runtime(item.get('RunTimeTicks'))
                    genres = ", ".join(item.get('Genres', [])[:2]) # Only show first 2 genres
                    
                    # Formatting the field value with extra info
                    info = f"⭐ {rating} | ⏳ {runtime} | 🎭 {genres}"
                    embed.add_field(name=f"{i}. {name} ({year})", value=info, inline=False)

                view = JellyfinMultiView(self.app_id, items)
                await ctx.send(embed=embed, view=view)
