import discord
from redbot.core import commands
import aiohttp
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import quote
import io

class Imagine(commands.Cog):
    """Generate images using ComfyUI and the flux_schnell workflow."""

    def __init__(self, bot):
        self.bot = bot
        self.api_url = "http://127.0.0.1:8000"
        self.workflow_path = Path(__file__).parent / "flux_schnell-api.json"

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image with ComfyUI from a prompt using flux_schnell-api.json"""

        loading_msg = await ctx.send("🧠 Preparing your image...")

        try:
            with open(self.workflow_path, "r", encoding="utf-8") as f:
                prompt_data = json.load(f)

            if "prompt" not in prompt_data:
                return await ctx.send("❌ flux_schnell-api.json must wrap nodes in a top-level 'prompt' key.")

            for node_id, node in prompt_data["prompt"].items():
                if isinstance(node, dict) and "inputs" in node:
                    for key, val in node["inputs"].items():
                        if isinstance(val, str) and "{prompt}" in val:
                            node["inputs"][key] = val.replace("{prompt}", prompt)

            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.api_url}/prompt", json=prompt_data) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return await ctx.send(f"❌ Failed to submit prompt: `{resp.status}`\n```{error_text}```")
                    data = await resp.json()
                    prompt_id = data.get("prompt_id")

            start_time = time.time()
            timeout = 180  # 3 minutes max wait
            poll_interval = 3
            dots = ["⏳", "🔄", "🌀", "🔃", "🔁", "♻️", "💫"]
            dot_index = 0

            async with aiohttp.ClientSession() as session:
                while True:
                    if time.time() - start_time > timeout:
                        await loading_msg.edit(content="❌ Image generation timed out.")
                        return

                    async with session.get(f"{self.api_url}/history/{prompt_id}") as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            outputs = result.get("outputs", {})
                            image_path = None
                            for node_output in outputs.values():
                                images = node_output.get("images")
                                if images:
                                    image_path = images[0].get("filename")
                                    break
                            if image_path:
                                await loading_msg.edit(content=f"✅ Image generated for prompt: `{prompt}`")

                                # Download image bytes
                                async with session.get(f"{self.api_url}/view?filename={quote(image_path)}") as img_resp:
                                    if img_resp.status == 200:
                                        img_bytes = await img_resp.read()
                                        file = discord.File(fp=io.BytesIO(img_bytes), filename=image_path)
                                        await ctx.send(file=file)
                                        return
                                    else:
                                        await ctx.send("❌ Failed to download generated image.")
                                        return

                    await loading_msg.edit(content=f"{dots[dot_index]} Generating image... `{prompt}`")
                    dot_index = (dot_index + 1) % len(dots)
                    await asyncio.sleep(poll_interval)

        except Exception as e:
            await ctx.send(f"⚠️ Error: `{e}`")
