import discord
from redbot.core import commands
import aiohttp
import asyncio
import json
from pathlib import Path

class Imagine(commands.Cog):
    """Generate images using ComfyUI and the flux_schnell workflow."""

    def __init__(self, bot):
        self.bot = bot
        self.api_url = "http://127.0.0.1:8188"  # Update this if needed
        self.workflow_path = Path(__file__).parent / "flux_schnell-api.json"

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image with ComfyUI from a prompt using flux_schnell-api.json"""

        loading_msg = await ctx.send("🧠 Preparing your image...")

        try:
            # Load and inject prompt
            with open(self.workflow_path, "r", encoding="utf-8") as f:
                prompt_data = json.load(f)

            # Replace `{prompt}` placeholders in the workflow
            for node_id, node in prompt_data.items():
                if isinstance(node, dict) and "inputs" in node:
                    for key, val in node["inputs"].items():
                        if isinstance(val, str) and "{prompt}" in val:
                            node["inputs"][key] = val.replace("{prompt}", prompt)

            # Submit to ComfyUI
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.api_url}/prompt", json=prompt_data) as resp:
                    if resp.status != 200:
                        return await ctx.send("❌ Failed to submit prompt.")
                    data = await resp.json()
                    prompt_id = data.get("prompt_id")

            # Animated loading
            dots = ["⏳", "🔄", "🌀", "🔃", "🔁", "♻️", "💫"]
            for i in range(12):  # ~18 seconds total
                await loading_msg.edit(content=f"{dots[i % len(dots)]} Generating image... `{prompt}`")
                await asyncio.sleep(1.5)

            # Poll results
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_url}/history/{prompt_id}") as resp:
                    if resp.status != 200:
                        return await ctx.send("❌ Failed to retrieve result.")
                    result = await resp.json()

            # Extract image path
            outputs = result.get("outputs", {})
            image_path = None

            for node_output in outputs.values():
                images = node_output.get("images")
                if images:
                    image_path = images[0].get("filename")
                    break

            if not image_path:
                return await ctx.send("❌ No image returned.")

            image_url = f"{self.api_url}/view?filename={image_path}"
            await ctx.send("✅ Done!", embed=discord.Embed(title="Your image").set_image(url=image_url))

        except Exception as e:
            await ctx.send(f"⚠️ Error: `{e}`")
