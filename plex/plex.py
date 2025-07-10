import aiohttp
import discord
from redbot.core import commands
from xml.etree import ElementTree
from urllib.parse import quote

class PlexDirect(commands.Cog):
    """Search Plex and send direct stream URLs."""

    def __init__(self, bot):
        self.bot = bot
        self.plex_url = "http://127.0.0.1:32400"  # Plex server URL, adjust if needed
        self.token = "DQZ_nsR-y5zEwhsyV2-5"             # Your Plex token here
        self.session = aiohttp.ClientSession()

    def cog_unload(self):
        self.bot.loop.create_task(self.session.close())

    async def plex_request(self, endpoint):
        url = f"{self.plex_url}{endpoint}"
        headers = {"X-Plex-Token": self.token}
        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
            return ElementTree.fromstring(text)

    @commands.command()
    async def plex(self, ctx, *, search: str):
        """
        Search for a movie and get a direct stream URL (tokenized).
        Users can watch immediately without a Plex account.
        """
        # Search movies (type=1)
        root = await self.plex_request(f"/library/sections/1/search?type=1&query={quote(search)}")
        if root is None:
            await ctx.send("Failed to connect to Plex server.")
            return

        videos = root.findall(".//Video")
        if not videos:
            await ctx.send("No movies found with that name.")
            return

        video = videos[0]  # Take first result
        title = video.get("title")
        year = video.get("year")
        summary = video.find("Summary").text if video.find("Summary") is not None else "No summary available."
        thumb = f"{self.plex_url}{video.get('thumb')}?X-Plex-Token={self.token}"

        # Find media part ID for direct streaming
        media = video.find("Media")
        if media is None:
            await ctx.send("No playable media found.")
            return

        part = media.find("Part")
        if part is None:
            await ctx.send("No playable media part found.")
            return

        part_key = part.get("key")  # e.g. "/library/parts/12345/file.mp4"

        # Construct direct stream URL with token
        stream_url = f"{self.plex_url}{part_key}?X-Plex-Token={self.token}"

        embed = discord.Embed(title=f"{title} ({year})", description=summary, color=0x00aaff)
        embed.set_thumbnail(url=thumb)
        embed.add_field(name="Direct Stream Link", value=f"[Click here to watch]({stream_url})", inline=False)
        embed.set_footer(text="No Plex account needed, watch instantly if your Plex server allows it.")

        await ctx.send(embed=embed)
