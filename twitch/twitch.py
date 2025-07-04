import discord
from redbot.core import commands, Config
from redbot.core.utils import tasks
import aiohttp
import logging

log = logging.getLogger("red.twitch")

class Twitch(commands.Cog):
    """Twitch integration with live stream notifications."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        self.config.register_global(
            client_id=None,
            client_secret=None,
            access_token=None,
            token_expiry=0,
            streamer=None,
            notify_channel=None,
            is_live=False,
        )
        self.session = aiohttp.ClientSession()
        self.live_check.start()

    def cog_unload(self):
        self.live_check.cancel()
        self.bot.loop.create_task(self.session.close())

    @commands.group()
    @commands.is_owner()
    async def twitch(self, ctx):
        """Twitch related commands."""
        pass

    @twitch.command()
    async def setcredentials(self, ctx, client_id: str, client_secret: str):
        """Set your Twitch Client ID and Client Secret."""
        await self.config.client_id.set(client_id)
        await self.config.client_secret.set(client_secret)
        await ctx.send("Twitch credentials saved. Fetching access token...")
        await self.fetch_access_token(ctx)

    @twitch.command()
    async def setnotify(self, ctx, streamer: str, channel: discord.TextChannel = None):
        """Set streamer to watch and channel for live notifications."""
        channel = channel or ctx.channel
        await self.config.streamer.set(streamer.lower())
        await self.config.notify_channel.set(channel.id)
        await self.config.is_live.set(False)  # Reset live status
        await ctx.send(f"Notifications set for `{streamer}` in {channel.mention}.")

    async def fetch_access_token(self, ctx=None):
        client_id = await self.config.client_id()
        client_secret = await self.config.client_secret()
        if not client_id or not client_secret:
            if ctx:
                await ctx.send("Set your client ID and secret first using `[p]twitch setcredentials`.")
            return None

        url = (
            "https://id.twitch.tv/oauth2/token"
            f"?client_id={client_id}"
            f"&client_secret={client_secret}"
            "&grant_type=client_credentials"
        )

        async with self.session.post(url) as resp:
            if resp.status != 200:
                msg = f"Failed to get token: HTTP {resp.status}"
                log.warning(msg)
                if ctx:
                    await ctx.send(msg)
                return None

            data = await resp.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in", 0)

            await self.config.access_token.set(access_token)
            await self.config.token_expiry.set(self.bot.loop.time() + expires_in)

            if ctx:
                await ctx.send("Access token fetched and saved.")
            log.info("Twitch access token fetched.")
            return access_token

    @tasks.loop(seconds=60)
    async def live_check(self):
        streamer = await self.config.streamer()
        channel_id = await self.config.notify_channel()
        if not streamer or not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        access_token = await self.config.access_token()
        client_id = await self.config.client_id()

        # Refresh token if expired (simple logic)
        expiry = await self.config.token_expiry()
        if expiry is None or self.bot.loop.time() > expiry - 60:
            log.info("Access token expired or near expiry, refreshing...")
            await self.fetch_access_token()

        headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {access_token}"
        }

        url = f"https://api.twitch.tv/helix/streams?user_login={streamer}"
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    log.warning(f"Twitch API returned {resp.status} for live check")
                    return
                data = await resp.json()
        except Exception as e:
            log.error(f"Error fetching Twitch stream data: {e}")
            return

        is_live = bool(data.get("data"))
        was_live = await self.config.is_live()

        if is_live and not was_live:
            stream = data["data"][0]
            embed = discord.Embed(
                title=f"{streamer} is LIVE on Twitch!",
                url=f"https://twitch.tv/{streamer}",
                description=stream.get("title", "No title"),
                color=discord.Color.purple()
            )
            embed.add_field(name="Game", value=stream.get("game_name", "Unknown"))
            thumbnail_url = stream.get("thumbnail_url", "").format(width=640, height=360)
            embed.set_image(url=thumbnail_url)
            await channel.send(embed=embed)
            await self.config.is_live.set(True)

        elif not is_live and was_live:
            await self.config.is_live.set(False)

    @live_check.before_loop
    async def before_live_check(self):
        await self.bot.wait_until_ready()
