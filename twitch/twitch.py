from redbot.core import commands, Config
import aiohttp
import asyncio
import logging

log = logging.getLogger("red.twitch")

class Twitch(commands.Cog):
    """Basic Twitch OAuth app authentication"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        self.config.register_global(client_id=None, client_secret=None, access_token=None, token_expiry=0)
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        await self.session.close()

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
            await self.config.token_expiry.set(ctx.bot.loop.time() + expires_in)

            if ctx:
                await ctx.send("Access token fetched and saved.")
            log.info("Twitch access token fetched.")
            return access_token
