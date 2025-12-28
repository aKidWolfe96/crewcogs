from redbot.core import commands, Config
import aiohttp
import urllib.parse
import discord
import re

class JellyfinLaunchView(discord.ui.View):
    """View containing a button to launch the Discord Activity"""
    def __init__(self, app_id: str):
        super().__init__(timeout=None)
        activity_url = f"https://discord.com/activities/{app_id}"
        
        self.add_item(discord.ui.Button(
            label="Watch in Discord",
            url=activity_url,
            style=discord.ButtonStyle.link,
            emoji="🎬"
        ))

class JellyfinSearch(commands.Cog):
    """Jellyfin search commands with automatic URL sanitization"""

    def __init__(self, bot):
        self.bot = bot
        self.app_id = "1385831372643373119"
        self.config = Config.get_conf(self, identifier=856712356)
        default_global = {"base_url": None, "api_key": None}
        self.config.register_global(**default_global)

    @commands.command()
    @commands.is_owner()
    async def setjellyfinurl(self, ctx, url: str):
        """Set the Jellyfin server URL (it will automatically clean up trailing slashes)"""
        # Clean the URL: Remove trailing slashes and common web UI paths
        clean_url = url.rstrip('/')
        clean_url = re.sub(r'/(web|home|index\.html).*$', '', clean_url)
        
        if not clean_url.startswith(('http://', 'https://')):
            return await ctx.send("❌ Error: URL must start with http:// or https://")

        await self.config.base_url.set(clean_url)
        await ctx.send(f"✅ Jellyfin server URL set and cleaned: `{clean_url}`")

    @commands.command()
    @commands.is_owner()
    async def setjellyfinapi(self, ctx, api_key: str):
        """Set the Jellyfin API key"""
        await self.config.api_key.set(api_key)
        await ctx.send("✅ Jellyfin API key has been set.")
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

    @commands.command(name="searchj")
    async def searchj(self, ctx, *, query: str):
        """Search and watch content directly in Discord"""
        base_url = await self.config.base_url()
        api_key = await self.config.api_key()
        
        if not base_url or not api_key:
            return await ctx.send("Please set the URL and API key first.")

        # Ensure we are calling the API path, not the web path
        search_url = f"{base_url}/Items"
        params = {
            "searchTerm": query,
            "IncludeItemTypes": "Movie,Series",
            "Recursive": "true",
            "Limit": 5,
            "api_key": api_key
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(search_url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        items = data.get('Items', [])

                        if not items:
                            return await ctx.send(f"No results found for '{query}'.")

                        top_item = items[0]
                        item_id = top_item.get('Id')
                        
                        embed = discord.Embed(
                            title=f"Results for: {query}",
                            description="Click the button to watch inside Discord!",
                            color=discord.Color.dark_purple()
                        )

                        # Fetch the poster for the main result
                        image_url = f"{base_url}/Items/{item_id}/Images/Primary?api_key={api_key}"
                        embed.set_thumbnail(url=image_url)

                        for item in items[:5]:
                            name = item.get('Name')
                            year = item.get('ProductionYear', 'N/A')
                            i_id = item.get('Id')
                            embed.add_field(
                                name=f"{name} ({year})",
                                value=f"[Open in Browser]({base_url}/web/index.html#!/details?id={i_id})",
                                inline=False
                            )

                        view = JellyfinLaunchView(self.app_id)
                        await ctx.send(embed=embed, view=view)
                    else:
                        await ctx.send(f"❌ Server Error: {response.status}. Verify your API key.")
            except Exception as e:
                await ctx.send(f"❌ Connection error: {str(e)}")
