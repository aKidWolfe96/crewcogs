from redbot.core import commands, Config
import aiohttp
import urllib.parse
import discord
from datetime import datetime

class JellyfinLaunchView(discord.ui.View):
    """View containing a button to launch the Discord Activity"""
    def __init__(self, app_id: str, item_id: str):
        super().__init__(timeout=None)
        # The Activity URL format for Discord
        activity_url = f"https://discord.com/activities/{app_id}"
        
        self.add_item(discord.ui.Button(
            label="Watch in Discord",
            url=activity_url,
            style=discord.ButtonStyle.link,
            emoji="🎬"
        ))

class JellyfinSearch(commands.Cog):
    """Jellyfin search commands with Discord Activity support"""

    def __init__(self, bot):
        self.bot = bot
        self.app_id = "1385831372643373119" # Your provided App ID
        self.config = Config.get_conf(self, identifier=856712356)
        default_global = {
            "base_url": None,
            "api_key": None
        }
        self.config.register_global(**default_global)

    async def get_base_url(self):
        return await self.config.base_url()

    async def get_api_key(self):
        return await self.config.api_key()

    @commands.command()
    @commands.is_owner()
    async def setjellyfinurl(self, ctx, url: str):
        """Set the Jellyfin server URL"""
        url = url.rstrip('/')
        await self.config.base_url.set(url)
        await ctx.send(f"Jellyfin server URL has been set to: {url}")

    @commands.command()
    @commands.is_owner()
    async def setjellyfinapi(self, ctx, api_key: str):
        """Set the Jellyfin API key"""
        await self.config.api_key.set(api_key)
        await ctx.send("Jellyfin API key has been set.")
        await ctx.message.delete()

    def format_runtime(self, runtime_ticks):
        if not runtime_ticks:
            return "N/A"
        minutes = int(runtime_ticks / (10000000 * 60))
        hours = minutes // 60
        remaining_minutes = minutes % 60
        return f"{hours}h {remaining_minutes}m" if hours > 0 else f"{remaining_minutes}m"

    @commands.command(name="searchj")
    async def searchj(self, ctx, *, query: str):
        """Search and watch content directly in Discord"""
        base_url = await self.get_base_url()
        api_key = await self.get_api_key()
        
        if not base_url or not api_key:
            return await ctx.send("Please set the URL and API key first.")

        encoded_query = urllib.parse.quote(query)
        # Added Items/Images access to the search
        search_url = f"{base_url}/Items?searchTerm={encoded_query}&IncludeItemTypes=Movie,Series&Recursive=true&SearchType=String&Limit=5&api_key={api_key}"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(search_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        items = data.get('Items', [])

                        if not items:
                            return await ctx.send("No results found.")

                        # Create the primary embed for the first result
                        top_item = items[0]
                        item_id = top_item.get('Id')
                        
                        embed = discord.Embed(
                            title=f"Search Results: {query}",
                            description="Click the button below to launch the theater in Discord!",
                            color=discord.Color.dark_purple()
                        )

                        # Set the poster image for the first result
                        image_url = f"{base_url}/Items/{item_id}/Images/Primary?api_key={api_key}"
                        embed.set_thumbnail(url=image_url)

                        for item in items:
                            name = item.get('Name')
                            year = item.get('ProductionYear', 'N/A')
                            i_id = item.get('Id')
                            item_type = item.get('Type')
                            
                            embed.add_field(
                                name=f"{name} ({year})",
                                value=f"Type: {item_type} | [Web Link]({base_url}/web/index.html#!/details?id={i_id})",
                                inline=False
                            )

                        # We use the top result for the Activity Button
                        view = JellyfinLaunchView(self.app_id, item_id)
                        await ctx.send(embed=embed, view=view)
                    else:
                        await ctx.send(f"Server returned error code: {response.status}")
            except Exception as e:
                await ctx.send(f"Connection error: {str(e)}")
