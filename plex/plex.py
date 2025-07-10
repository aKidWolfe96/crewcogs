import aiohttp
import discord
from redbot.core import commands
from xml.etree import ElementTree
from urllib.parse import quote


class Plex(commands.Cog):
    """Plex integration with interactive Discord menu."""

    def __init__(self, bot):
        self.bot = bot
        self.plex_url = "http://YOUR.PUBLIC.IP:32400"
        self.token = "YOUR_PLEX_TOKEN"
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

    async def get_movies(self):
        root = await self.plex_request("/library/sections/1/all?type=1")
        return root.findall(".//Video") if root is not None else []

    async def get_stream_url(self, video):
        media = video.find("Media")
        if not media:
            return None
        part = media.find("Part")
        if not part:
            return None
        key = part.get("key")
        return f"{self.plex_url}{key}?X-Plex-Token={self.token}"

    def build_movie_embed(self, video, page, total):
        title = video.get("title")
        year = video.get("year")
        summary = video.find("Summary").text if video.find("Summary") is not None else "No summary available."
        thumb = f"{self.plex_url}{video.get('thumb')}?X-Plex-Token={self.token}"

        embed = discord.Embed(title=f"{title} ({year})", description=summary, color=0x00aaff)
        embed.set_thumbnail(url=thumb)
        embed.set_footer(text=f"Movie {page+1} of {total}")
        return embed

    @commands.command()
    async def plexstream(self, ctx, *, search: str):
        """Search and get a direct stream URL."""
        root = await self.plex_request(f"/library/sections/1/search?type=1&query={quote(search)}")
        if not root:
            return await ctx.send("Failed to reach Plex.")
        videos = root.findall(".//Video")
        if not videos:
            return await ctx.send("No results.")

        video = videos[0]
        stream_url = await self.get_stream_url(video)
        if not stream_url:
            return await ctx.send("Stream unavailable.")

        embed = self.build_movie_embed(video, 0, 1)
        embed.add_field(name="Watch Now", value=f"[Click here to stream]({stream_url})", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def plexmenu(self, ctx):
        """Interactive menu to browse your Plex movie library."""
        videos = await self.get_movies()
        if not videos:
            return await ctx.send("No movies found.")

        class PlexView(discord.ui.View):
            def __init__(self, cog, videos):
                super().__init__(timeout=300)
                self.cog = cog
                self.videos = videos
                self.index = 0

                # Add buttons
                self.add_item(discord.ui.Button(label="Watch Now", style=discord.ButtonStyle.link, url="https://example.com"))
                self.update_buttons()

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return True

            async def update_embed(self, interaction):
                video = self.videos[self.index]
                embed = self.cog.build_movie_embed(video, self.index, len(self.videos))
                stream_url = await self.cog.get_stream_url(video)

                # Replace URL of first button (Watch Now)
                self.children[0].url = stream_url or "https://example.com"
                await interaction.response.edit_message(embed=embed, view=self)

            def update_buttons(self):
                self.clear_items()

                # Watch Now (link button)
                self.add_item(discord.ui.Button(label="Watch Now", style=discord.ButtonStyle.link, url="https://example.com"))

                # Prev button
                self.add_item(discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.index == 0, custom_id="prev"))

                # Next button
                self.add_item(discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.index == len(self.videos)-1, custom_id="next"))

            @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
            async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.index -= 1
                self.update_buttons()
                await self.update_embed(interaction)

            @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.index += 1
                self.update_buttons()
                await self.update_embed(interaction)

        view = PlexView(self, videos)
        embed = self.build_movie_embed(videos[0], 0, len(videos))
        stream_url = await self.get_stream_url(videos[0])
        view.children[0].url = stream_url or "https://example.com"
        await ctx.send("🎬 Browse your Plex movies:", embed=embed, view=view)
