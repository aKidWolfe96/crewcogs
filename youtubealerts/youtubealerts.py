import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.youtubealerts")

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

DEFAULT_LIVE_MESSAGE = "🔴 **{channel}** is now live on YouTube!"
DEFAULT_UPLOAD_MESSAGE = "📤 **{channel}** just uploaded: **{title}**"
DEFAULT_COLOR = 0xFF0000  # YouTube red


class YouTubeAlerts(commands.Cog):
    """Custom YouTube live and upload notifications with rich embeds."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=987654321, force_registration=True)

        self.config.register_global(
            api_key=None,
            interval=300,  # seconds
        )
        self.config.register_guild(
            channel=None,
            channels=[],           # list of YouTube channel IDs
            live_message=DEFAULT_LIVE_MESSAGE,
            upload_message=DEFAULT_UPLOAD_MESSAGE,
            mention=None,
            color=DEFAULT_COLOR,
            show_thumbnail=True,
        )

        self._seen_live = {}      # channel_id -> video_id
        self._seen_uploads = {}   # channel_id -> video_id
        self._seeded = False
        self.check_youtube.start()

    def cog_unload(self):
        self.check_youtube.cancel()
        asyncio.create_task(self.session.close())

    # ------------------------------------------------------------------ #
    # API Helpers
    # ------------------------------------------------------------------ #
    async def _api_get(self, endpoint: str, params: dict) -> Optional[dict]:
        api_key = await self.config.api_key()
        if not api_key:
            return None
        params["key"] = api_key
        url = f"{YOUTUBE_API_BASE}/{endpoint}"
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("YouTube API %s returned %s", endpoint, resp.status)
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            log.error("YouTube API error: %s", e)
            return None

    async def _get_channel_id(self, identifier: str) -> Optional[str]:
        identifier = identifier.strip()
        if identifier.startswith("UC") and len(identifier) > 20:  # likely channel ID
            return identifier
        # Search by handle or name
        data = await self._api_get("search", {
            "part": "snippet",
            "q": identifier.replace("@", ""),
            "type": "channel",
            "maxResults": 1
        })
        if data and data.get("items"):
            return data["items"][0]["snippet"]["channelId"]
        return None

    async def _get_live_streams(self, channel_ids):
        if not channel_ids:
            return {}
        live = {}
        for cid in channel_ids:
            data = await self._api_get("search", {
                "part": "snippet",
                "channelId": cid,
                "eventType": "live",
                "type": "video",
                "maxResults": 1
            })
            if data and data.get("items"):
                item = data["items"][0]
                live[cid] = {
                    "video_id": item["id"]["videoId"],
                    "title": item["snippet"]["title"],
                    "channel_title": item["snippet"]["channelTitle"],
                    "thumbnail": item["snippet"]["thumbnails"]["high"]["url"]
                }
        return live

    async def _get_latest_upload(self, channel_id: str) -> Optional[dict]:
        data = await self._api_get("channels", {"part": "contentDetails", "id": channel_id})
        if not data or not data.get("items"):
            return None

        playlist_id = data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        data = await self._api_get("playlistItems", {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 1
        })
        if data and data.get("items"):
            item = data["items"][0]
            return {
                "video_id": item["snippet"]["resourceId"]["videoId"],
                "title": item["snippet"]["title"],
                "channel_title": item["snippet"]["channelTitle"],
                "thumbnail": item["snippet"]["thumbnails"]["high"]["url"]
            }
        return None

    # ------------------------------------------------------------------ #
    # Background Task
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=300)
    async def check_youtube(self):
        all_guilds = await self.config.all_guilds()
        watched = set()
        for gconf in all_guilds.values():
            watched.update(gconf.get("channels", []))

        if not watched:
            return

        channel_ids = []
        for c in watched:
            cid = await self._get_channel_id(c) or c
            if cid:
                channel_ids.append(cid)

        if not channel_ids:
            return

        live = await self._get_live_streams(channel_ids)

        if not self._seeded:
            for cid, info in live.items():
                self._seen_live[cid] = info["video_id"]
            self._seeded = True
            return

        # Live announcements
        for cid, info in live.items():
            if self._seen_live.get(cid) == info["video_id"]:
                continue
            self._seen_live[cid] = info["video_id"]
            await self._announce(cid, info, is_live=True)

        # Upload announcements
        for cid in channel_ids:
            upload = await self._get_latest_upload(cid)
            if not upload or self._seen_uploads.get(cid) == upload["video_id"]:
                continue
            self._seen_uploads[cid] = upload["video_id"]
            await self._announce(cid, upload, is_live=False)

    @check_youtube.before_loop
    async def _before_check(self):
        await self.bot.wait_until_red_ready()
        interval = await self.config.interval()
        self.check_youtube.change_interval(seconds=max(180, interval))

    # ------------------------------------------------------------------ #
    # Announcement
    # ------------------------------------------------------------------ #
    def _format(self, text: str, data: dict) -> str:
        try:
            return text.format(
                channel=data.get("channel_title", "Unknown Channel"),
                title=data.get("title", ""),
                url=f"https://youtu.be/{data.get('video_id', '')}"
            )
        except:
            return text

    async def _announce(self, channel_id: str, data: dict, is_live: bool):
        all_guilds = await self.config.all_guilds()
        for gid, gconf in all_guilds.items():
            if channel_id not in gconf.get("channels", []):
                continue
            ch_id = gconf.get("channel")
            if not ch_id:
                continue
            guild = self.bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(ch_id)
            if not channel:
                continue

            template = gconf.get("live_message" if is_live else "upload_message")
            content = self._format(template, data)

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

            embed = discord.Embed(
                title=data.get("title"),
                url=f"https://youtu.be/{data['video_id']}",
                color=discord.Color(gconf.get("color", DEFAULT_COLOR)),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(
                name=f"{data.get('channel_title')} {'is LIVE!' if is_live else 'uploaded a video'}",
                url=f"https://youtube.com/channel/{channel_id}"
            )
            if gconf.get("show_thumbnail", True) and data.get("thumbnail"):
                embed.set_image(url=data["thumbnail"])

            embed.add_field(name="Type", value="🔴 LIVE" if is_live else "📤 Upload", inline=True)

            try:
                await channel.send(
                    content=prefix + content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(everyone=True, roles=True)
                )
            except Exception as e:
                log.error("Failed to send YouTube alert: %s", e)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.group(aliases=["yalerts", "ytalerts"])
    async def youtubset(self, ctx: commands.Context):
        """Configure YouTube alerts."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @youtubset.command(name="key")
    @commands.is_owner()
    async def _key(self, ctx, *, api_key: str):
        """Set YouTube Data API v3 key (owner only)."""
        await self.config.api_key.set(api_key.strip())
        await ctx.send("✅ YouTube API key saved.")
        if ctx.guild:
            try:
                await ctx.message.delete()
            except:
                pass

    @youtubset.command(name="interval")
    @commands.is_owner()
    async def _interval(self, ctx, seconds: int):
        """Set polling interval (minimum 180 seconds)."""
        seconds = max(180, seconds)
        await self.config.interval.set(seconds)
        self.check_youtube.change_interval(seconds=seconds)
        await ctx.send(f"✅ Polling every {seconds} seconds.")

    @youtubset.command(name="channel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _channel(self, ctx, channel: discord.TextChannel = None):
        """Set alert channel."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"✅ Alerts will be sent to {channel.mention}.")

    @youtubset.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _add(self, ctx, *, identifier: str):
        """Add a channel by ID or @handle."""
        if not await self.config.api_key():
            return await ctx.send("Set API key first with `[p]youtubset key`.")

        cid = await self._get_channel_id(identifier)
        if not cid:
            return await ctx.send("❌ Could not find that YouTube channel.")

        async with self.config.guild(ctx.guild).channels() as chans:
            if cid in chans:
                return await ctx.send("Already watching that channel.")
            chans.append(cid)

        self._seeded = False
        await ctx.send(f"✅ Now watching **{cid}**.")

    @youtubset.command(name="remove", aliases=["del"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _remove(self, ctx, channel_id: str):
        """Remove a watched channel."""
        async with self.config.guild(ctx.guild).channels() as chans:
            if channel_id not in chans:
                return await ctx.send("Not watching that channel.")
            chans.remove(channel_id)
        await ctx.send(f"✅ Stopped watching **{channel_id}**.")

    @youtubset.command(name="list")
    @commands.guild_only()
    async def _list(self, ctx):
        """List watched channels."""
        chans = await self.config.guild(ctx.guild).channels()
        if not chans:
            return await ctx.send("Not watching any channels yet.")
        await ctx.send("Watching:\n" + "\n".join(f"• `{c}`" for c in chans))

    @youtubset.command(name="livemessage")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _livemessage(self, ctx, *, text: str):
        """Set live stream message. Placeholders: {channel} {title} {url}"""
        await self.config.guild(ctx.guild).live_message.set(text)
        await ctx.send("✅ Live message updated.")

    @youtubset.command(name="uploadmessage")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _uploadmessage(self, ctx, *, text: str):
        """Set upload message."""
        await self.config.guild(ctx.guild).upload_message.set(text)
        await ctx.send("✅ Upload message updated.")

    @youtubset.command(name="mention")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _mention(self, ctx, value: str = "none"):
        """Set mention: everyone, here, role, or none."""
        value = value.lower()
        if value == "none":
            await self.config.guild(ctx.guild).mention.set(None)
            return await ctx.send("Ping disabled.")
        if value in ("everyone", "here"):
            await self.config.guild(ctx.guild).mention.set(value)
            return await ctx.send(f"Will ping @{value}.")
        try:
            role = await commands.RoleConverter().convert(ctx, value)
            await self.config.guild(ctx.guild).mention.set(role.id)
            await ctx.send(f"Will ping {role.name}.")
        except commands.BadArgument:
            await ctx.send("Invalid input.")

    @youtubset.command(name="color", aliases=["colour"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _color(self, ctx, hex_color: str):
        """Set embed color (e.g. #FF0000)."""
        try:
            value = int(hex_color.lstrip("#"), 16)
            await self.config.guild(ctx.guild).color.set(value)
            await ctx.send(embed=discord.Embed(description="✅ Embed color updated.", color=value))
        except ValueError:
            await ctx.send("Invalid hex color.")

    @youtubset.command(name="test")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def _test(self, ctx, channel: str = None):
        """Send a test alert."""
        gconf = await self.config.guild(ctx.guild).all()
        if not gconf["channels"]:
            return await ctx.send("Add a channel first.")

        test_data = {
            "video_id": "dQw4w9wgxcq",
            "title": "Test Video / Live Stream",
            "channel_title": "Test Channel",
            "thumbnail": "https://i.ytimg.com/vi/dQw4w9wgxcq/maxresdefault.jpg"
        }

        await ctx.send("**YouTubeAlerts Test** (Live Style)")
        await self._announce(gconf["channels"][0], test_data, is_live=True)

        await ctx.send("**YouTubeAlerts Test** (Upload Style)")
        await self._announce(gconf["channels"][0], test_data, is_live=False)

    @youtubset.command(name="settings", aliases=["show"])
    @commands.guild_only()
    async def _settings(self, ctx):
        """Show current configuration."""
        g = await self.config.guild(ctx.guild).all()
        key_set = "✅ Set" if await self.config.api_key() else "❌ Not set"
        ch = ctx.guild.get_channel(g["channel"])
        mention = g["mention"]
        if isinstance(mention, int):
            role = ctx.guild.get_role(mention)
            mention = role.name if role else "deleted"

        embed = discord.Embed(title="YouTubeAlerts Settings", color=g["color"])
        embed.add_field(name="API Key", value=key_set, inline=True)
        embed.add_field(name="Alert Channel", value=ch.mention if ch else "Not set", inline=True)
        embed.add_field(name="Mention", value=mention or "None", inline=True)
        embed.add_field(name="Watching", value=", ".join(g["channels"]) or "None", inline=False)
        embed.add_field(name="Live Message", value=g["live_message"][:200] + "..." if len(g["live_message"]) > 200 else g["live_message"], inline=False)
        embed.add_field(name="Upload Message", value=g["upload_message"][:200] + "..." if len(g["upload_message"]) > 200 else g["upload_message"], inline=False)
        await ctx.send(embed=embed)
