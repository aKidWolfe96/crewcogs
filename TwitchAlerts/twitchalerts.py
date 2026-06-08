import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.twitchalerts")

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"

DEFAULT_MESSAGE = "\U0001F534 **{name}** is now live on Twitch!"
DEFAULT_COLOR = 0x9146FF  # Twitch purple


class TwitchAlerts(commands.Cog):
    """Custom Twitch live notifications with rich, configurable embeds."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=874512903, force_registration=True)

        self.config.register_global(
            client_id=None,
            client_secret=None,
            access_token=None,
            token_expiry=0,
            interval=60,
        )
        self.config.register_guild(
            channel=None,
            streamers=[],          # list of lowercase twitch logins
            message=DEFAULT_MESSAGE,
            mention=None,          # role id (int), or "everyone"/"here", or None
            color=DEFAULT_COLOR,
            show_avatar=True,
        )

        self._live = {}            # login -> stream id (in-memory transition state)
        self._seeded = False       # first poll only records state, never announces
        self._token_lock = asyncio.Lock()
        self.check_streams.start()

    def cog_unload(self):
        self.check_streams.cancel()
        self.bot.loop.create_task(self.session.close())

    # ------------------------------------------------------------------ #
    # Twitch API helpers
    # ------------------------------------------------------------------ #
    async def _get_token(self, force: bool = False) -> Optional[str]:
        async with self._token_lock:
            cid = await self.config.client_id()
            secret = await self.config.client_secret()
            if not cid or not secret:
                return None

            token = await self.config.access_token()
            expiry = await self.config.token_expiry()
            if token and not force and time.time() < expiry - 300:
                return token

            params = {
                "client_id": cid,
                "client_secret": secret,
                "grant_type": "client_credentials",
            }
            try:
                async with self.session.post(TWITCH_TOKEN_URL, params=params) as resp:
                    if resp.status != 200:
                        log.error("Twitch token request failed (%s)", resp.status)
                        return None
                    data = await resp.json()
            except aiohttp.ClientError as e:
                log.error("Twitch token request error: %s", e)
                return None

            token = data.get("access_token")
            await self.config.access_token.set(token)
            await self.config.token_expiry.set(time.time() + data.get("expires_in", 0))
            return token

    async def _api_get(self, url: str, params) -> Optional[dict]:
        cid = await self.config.client_id()
        token = await self._get_token()
        if not token:
            return None
        headers = {"Client-Id": cid, "Authorization": f"Bearer {token}"}
        try:
            async with self.session.get(url, params=params, headers=headers) as resp:
                if resp.status == 401:  # token expired/invalid -> refresh once
                    token = await self._get_token(force=True)
                    if not token:
                        return None
                    headers["Authorization"] = f"Bearer {token}"
                    async with self.session.get(url, params=params, headers=headers) as r2:
                        return await r2.json() if r2.status == 200 else None
                if resp.status != 200:
                    log.warning("Twitch API %s returned %s", url, resp.status)
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            log.error("Twitch API error: %s", e)
            return None

    async def _get_streams(self, logins) -> Optional[dict]:
        if not logins:
            return {}
        results = {}
        for i in range(0, len(logins), 100):  # Helix caps at 100 per call
            chunk = logins[i:i + 100]
            params = [("user_login", l) for l in chunk]
            data = await self._api_get(TWITCH_STREAMS_URL, params)
            if data is None:
                return None
            for s in data.get("data", []):
                if s.get("type") == "live":
                    results[s["user_login"].lower()] = s
        return results

    async def _get_user(self, login: str) -> Optional[dict]:
        data = await self._api_get(TWITCH_USERS_URL, {"login": login})
        if data and data.get("data"):
            return data["data"][0]
        return None

    # ------------------------------------------------------------------ #
    # Background poll loop
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=60)
    async def check_streams(self):
        all_guilds = await self.config.all_guilds()
        watched = set()
        for gconf in all_guilds.values():
            for s in gconf.get("streamers", []):
                watched.add(s.lower())
        if not watched:
            return

        live = await self._get_streams(list(watched))
        if live is None:  # API/auth failure, try again next cycle
            return

        if not self._seeded:  # first pass: seed state without announcing
            self._live = {k: v["id"] for k, v in live.items()}
            self._seeded = True
            return

        for login, stream in live.items():
            if self._live.get(login) == stream["id"]:
                continue  # same broadcast, already announced
            self._live[login] = stream["id"]
            await self._announce(login, stream)

        for login in list(self._live):  # forget streamers who went offline
            if login not in live:
                del self._live[login]

    @check_streams.before_loop
    async def _before_check(self):
        await self.bot.wait_until_red_ready()
        interval = await self.config.interval()
        self.check_streams.change_interval(seconds=max(30, interval))

    # ------------------------------------------------------------------ #
    # Embed / message building
    # ------------------------------------------------------------------ #
    def _format(self, text: str, stream: dict, user: Optional[dict]) -> str:
        try:
            return text.format(
                name=stream.get("user_name") or (user or {}).get("display_name", ""),
                game=stream.get("game_name") or "Unknown",
                title=stream.get("title") or "",
                viewers=f"{stream.get('viewer_count', 0):,}",
                url=f"https://twitch.tv/{stream.get('user_login', '')}",
            )
        except (KeyError, IndexError):
            return text  # bad placeholder in custom message -> send raw

    def _build_content(self, stream, user, gconf, guild) -> str:
        msg = self._format(gconf.get("message") or DEFAULT_MESSAGE, stream, user)
        mention = gconf.get("mention")
        prefix = ""
        if mention == "everyone":
            prefix = "@everyone "
        elif mention == "here":
            prefix = "@here "
        elif mention:
            role = guild.get_role(int(mention))
            if role:
                prefix = f"{role.mention} "
        return prefix + msg

    def _build_embed(self, stream, user, gconf) -> discord.Embed:
        login = stream.get("user_login", "")
        url = f"https://twitch.tv/{login}"
        embed = discord.Embed(
            title=stream.get("title") or "Live now!",
            url=url,
            color=discord.Color(gconf.get("color", DEFAULT_COLOR)),
            timestamp=datetime.now(timezone.utc),
        )
        name = stream.get("user_name") or (user or {}).get("display_name", login)
        icon = (user or {}).get("profile_image_url")
        embed.set_author(name=f"{name} is live on Twitch!", url=url, icon_url=icon)

        if stream.get("game_name"):
            embed.add_field(name="Playing", value=stream["game_name"], inline=True)
        embed.add_field(name="Viewers", value=f"{stream.get('viewer_count', 0):,}", inline=True)

        thumb = stream.get("thumbnail_url") or ""
        if thumb:
            thumb = thumb.replace("{width}", "1280").replace("{height}", "720")
            thumb += f"?t={int(time.time())}"  # cache-bust the preview image
            embed.set_image(url=thumb)

        if gconf.get("show_avatar", True) and icon:
            embed.set_thumbnail(url=icon)

        embed.set_footer(text="Twitch")
        return embed

    async def _announce(self, login: str, stream: dict):
        user = await self._get_user(login)
        all_guilds = await self.config.all_guilds()
        for gid, gconf in all_guilds.items():
            if login not in [s.lower() for s in gconf.get("streamers", [])]:
                continue
            channel_id = gconf.get("channel")
            if not channel_id:
                continue
            guild = self.bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            embed = self._build_embed(stream, user, gconf)
            content = self._build_content(stream, user, gconf, guild)
            allowed = discord.AllowedMentions(everyone=True, roles=True)
            try:
                await channel.send(content=content, embed=embed, allowed_mentions=allowed)
            except discord.Forbidden:
                log.warning("Missing permissions to post in channel %s", channel_id)
            except discord.HTTPException as e:
                log.error("Failed to send Twitch alert: %s", e)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.group(aliases=["talerts"])
    async def twitchset(self, ctx: commands.Context):
        """Configure Twitch live alerts."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @twitchset.command(name="clientid")
    @commands.is_owner()
    async def _clientid(self, ctx, client_id: str):
        """Set the Twitch app Client ID. DM the bot to keep it private."""
        await self.config.client_id.set(client_id.strip())
        await self._get_token(force=True)
        await ctx.send("Client ID set.")
        if ctx.guild:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    @twitchset.command(name="secret")
    @commands.is_owner()
    async def _secret(self, ctx, client_secret: str):
        """Set the Twitch app Client Secret. DM the bot to keep it private."""
        await self.config.client_secret.set(client_secret.strip())
        token = await self._get_token(force=True)
        await ctx.send("Client secret set." + ("" if token else " (Couldn't get a token \u2014 check the ID/secret.)"))
        if ctx.guild:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    @twitchset.command(name="interval")
    @commands.is_owner()
    async def _interval(self, ctx, seconds: int):
        """Set how often to poll Twitch (minimum 30s)."""
        seconds = max(30, seconds)
        await self.config.interval.set(seconds)
        self.check_streams.change_interval(seconds=seconds)
        await ctx.send(f"Polling every {seconds}s.")

    @twitchset.command(name="channel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _channel(self, ctx, channel: discord.TextChannel = None):
        """Set the channel alerts post in (defaults to the current channel)."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"Alerts will post in {channel.mention}.")

    @twitchset.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _add(self, ctx, streamer: str):
        """Watch a streamer. Accepts a name or a twitch.tv URL."""
        streamer = streamer.lower().strip().lstrip("@")
        if "twitch.tv/" in streamer:
            streamer = streamer.split("twitch.tv/")[-1].strip("/")
        if not await self.config.client_id():
            return await ctx.send("Set the Twitch credentials first (`clientid` / `secret`).")

        user = await self._get_user(streamer)
        if not user:
            return await ctx.send("Couldn't find that Twitch user.")

        async with self.config.guild(ctx.guild).streamers() as s:
            if streamer in s:
                return await ctx.send("Already watching that streamer.")
            s.append(streamer)
        self._seeded = False  # reseed so a currently-live streamer isn't insta-announced
        await ctx.send(f"Now watching **{user.get('display_name', streamer)}**.")

    @twitchset.command(name="remove", aliases=["del"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _remove(self, ctx, streamer: str):
        """Stop watching a streamer."""
        streamer = streamer.lower().strip().lstrip("@")
        async with self.config.guild(ctx.guild).streamers() as s:
            if streamer not in s:
                return await ctx.send("Not watching that streamer.")
            s.remove(streamer)
        self._live.pop(streamer, None)
        await ctx.send(f"Stopped watching **{streamer}**.")

    @twitchset.command(name="list")
    @commands.guild_only()
    async def _list(self, ctx):
        """List watched streamers."""
        streamers = await self.config.guild(ctx.guild).streamers()
        if not streamers:
            return await ctx.send("Not watching anyone yet.")
        await ctx.send("Watching: " + ", ".join(f"**{s}**" for s in streamers))

    @twitchset.command(name="message")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _message(self, ctx, *, text: str):
        """Set the alert text. Placeholders: {name} {game} {title} {viewers} {url}"""
        await self.config.guild(ctx.guild).message.set(text)
        await ctx.send("Message updated.")

    @twitchset.command(name="mention")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _mention(self, ctx, value: str = "none"):
        """Set a ping: a role, `everyone`, `here`, or `none`."""
        value = value.lower()
        if value == "none":
            await self.config.guild(ctx.guild).mention.set(None)
            return await ctx.send("Ping disabled.")
        if value in ("everyone", "here"):
            await self.config.guild(ctx.guild).mention.set(value)
            return await ctx.send(f"Will ping @{value}.")
        try:
            role = await commands.RoleConverter().convert(ctx, value)
        except commands.BadArgument:
            return await ctx.send("Give a role, `everyone`, `here`, or `none`.")
        await self.config.guild(ctx.guild).mention.set(role.id)
        await ctx.send(f"Will ping {role.name}.")

    @twitchset.command(name="color", aliases=["colour"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _color(self, ctx, hex_color: str):
        """Set the embed color, e.g. #9146FF."""
        try:
            value = int(hex_color.lstrip("#"), 16)
        except ValueError:
            return await ctx.send("Give a hex color like `#9146FF`.")
        await self.config.guild(ctx.guild).color.set(value)
        await ctx.send(embed=discord.Embed(description="Embed color set.", color=discord.Color(value)))

    @twitchset.command(name="avatar")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _avatar(self, ctx, on_off: bool):
        """Toggle showing the streamer's avatar in the embed (on/off)."""
        await self.config.guild(ctx.guild).show_avatar.set(on_off)
        await ctx.send(f"Avatar thumbnail {'on' if on_off else 'off'}.")

    @twitchset.command(name="test")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _test(self, ctx, streamer: str = None):
        """Preview the alert. Uses live data if the streamer is live, else a sample."""
        streamers = await self.config.guild(ctx.guild).streamers()
        if streamer:
            streamer = streamer.lower().lstrip("@")
        elif streamers:
            streamer = streamers[0]
        else:
            return await ctx.send("Add a streamer first, or pass one to test.")

        gconf = await self.config.guild(ctx.guild).all()
        user = await self._get_user(streamer)
        live = await self._get_streams([streamer]) or {}

        if streamer in live:
            stream = live[streamer]
            note = "(live preview)"
        else:
            stream = {
                "user_login": streamer,
                "user_name": (user or {}).get("display_name", streamer),
                "title": "Sample stream title",
                "game_name": "Just Chatting",
                "viewer_count": 1234,
                "thumbnail_url": "",
            }
            note = "(offline \u2014 sample preview)"

        embed = self._build_embed(stream, user, gconf)
        content = self._build_content(stream, user, gconf, ctx.guild)
        # Suppress real pings during a test
        await ctx.send(f"**TEST {note}** \u2014 raw text would be:\n> {content}")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @twitchset.command(name="settings", aliases=["show"])
    @commands.guild_only()
    async def _settings(self, ctx):
        """Show the current configuration."""
        g = await self.config.guild(ctx.guild).all()
        creds = "set" if await self.config.client_id() else "**NOT set**"
        channel = ctx.guild.get_channel(g["channel"]) if g["channel"] else None
        mention = g["mention"]
        if isinstance(mention, int):
            role = ctx.guild.get_role(mention)
            mention = role.name if role else "deleted role"
        embed = discord.Embed(title="Twitch Alerts settings", color=discord.Color(g["color"]))
        embed.add_field(name="Credentials", value=creds, inline=True)
        embed.add_field(name="Channel", value=channel.mention if channel else "not set", inline=True)
        embed.add_field(name="Ping", value=str(mention) if mention else "none", inline=True)
        embed.add_field(name="Watching", value=", ".join(g["streamers"]) or "nobody", inline=False)
        embed.add_field(name="Message", value=g["message"], inline=False)
        await ctx.send(embed=embed)
