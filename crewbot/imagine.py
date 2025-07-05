import discord
from redbot.core import commands
import aiohttp
import asyncio
import json
import uuid
from urllib.parse import urlencode
from io import BytesIO
from pathlib import Path

class Imagine(commands.Cog):
    """Generate images using ComfyUI with websocket progress."""

    def __init__(self, bot):
        self.bot = bot
        # Hardcoded ComfyUI server address
        self.server_address = "127.0.0.1:8000"
        # Hardcoded workflow path
        self.workflow_path = Path(__file__).parent / "flux_schnell-api.json"

    async def generate_image(self, prompt: str):
        client_id = str(uuid.uuid4())

        # Load workflow JSON and replace prompt text
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        if "prompt" not in workflow:
            raise Exception("JSON workflow must have a top-level 'prompt' key")

        for node in workflow["prompt"].values():
            if isinstance(node, dict) and "inputs" in node:
                for k, v in node["inputs"].items():
                    if isinstance(v, str) and "{prompt}" in v:
                        node["inputs"][k] = v.replace("{prompt}", prompt)

        async with aiohttp.ClientSession() as session:
            # Submit prompt
            async with session.post(f"http://{self.server_address}/prompt", json=workflow) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Failed to submit prompt: {resp.status}\n{text}")
                data = await resp.json()
                prompt_id = data.get("prompt_id")

            # Connect to websocket
            ws_url = f"ws://{self.server_address}/ws?clientId={client_id}"
            async with session.ws_connect(ws_url) as ws:
                executing_prompt = None
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        message = json.loads(msg.data)
                        if message.get("type") == "execution_start":
                            executing_prompt = message["data"]["prompt_id"]
                        if (message.get("type") == "executing" and
                            message["data"].get("prompt_id") == prompt_id and
                            message["data"].get("node") is None):
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            # Get output images
            async with session.get(f"http://{self.server_address}/history/{prompt_id}") as hist_resp:
                history = await hist_resp.json()

            outputs = history.get(prompt_id, {}).get("outputs", {})
            for node_output in outputs.values():
                images = node_output.get("images")
                if images:
                    for image in images:
                        filename = image.get("filename")
                        subfolder = image.get("subfolder")
                        folder_type = image.get("type")

                        params = {
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": folder_type
                        }
                        url_params = urlencode(params)
                        async with session.get(f"http://{self.server_address}/view?{url_params}") as img_resp:
                            if img_resp.status == 200:
                                image_bytes = await img_resp.read()
                                return image_bytes, filename

            raise Exception("No generated image found")

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image from a prompt using ComfyUI."""
        loading = await ctx.send(f"🧠 Generating image for: `{prompt}`")
        try:
            image_bytes, filename = await self.generate_image(prompt)
            file = discord.File(fp=BytesIO(image_bytes), filename=filename)
            await loading.edit(content=f"✅ Image generated for prompt: `{prompt}`")
            await ctx.send(file=file)
        except Exception as e:
            await loading.edit(content=f"❌ Error: {e}")
