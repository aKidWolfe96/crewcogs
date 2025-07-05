import discord
from redbot.core import commands
import aiohttp
import asyncio

class Imagine(commands.Cog):
    """Generate images using ComfyUI."""

    def __init__(self, bot):
        self.bot = bot
        self.api_url = "http://127.0.0.1:8188"  # Replace with your actual ComfyUI API endpoint

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image with ComfyUI from a prompt."""

        loading_msg = await ctx.send("🧠 Sending prompt to ComfyUI...")

        try:
            # STEP 1: Submit prompt to ComfyUI
            payload = {
                "prompt": prompt
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.api_url}/prompt", json=payload) as resp:
                    if resp.status != 200:
                        return await ctx.send("❌ Failed to send prompt.")
                    data = await resp.json()
                    prompt_id = data.get("prompt_id")

            # STEP 2: Wait while showing animated loading
            dots = ["⏳", "🔄", "🌀", "🔃", "🔁", "♻️", "💫"]
            for i in range(12):  # ~18 seconds of simulated loading
                await loading_msg.edit(content=f"{dots[i % len(dots)]} Generating image... `{prompt}`")
                await asyncio.sleep(1.5)

            # STEP 3: Get image from history
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_url}/history/{prompt_id}") as resp:
                    if resp.status != 200:
                        return await ctx.send("❌ Failed to retrieve result.")
                    result = await resp.json()

            # STEP 4: Extract image URL (update this if your output is different)
            image_url = result.get("output", {}).get("image_url")

            if not image_url:
                return await ctx.send("❌ No image returned.")

            await ctx.send("✅ Done!", file=discord.File(image_url))

        except Exception as e:
            await ctx.send(f"⚠️ Error: `{e}`")
