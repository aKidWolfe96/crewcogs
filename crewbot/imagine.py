import discord
from redbot.core import commands
import aiohttp
import asyncio
import json
import uuid
from pathlib import Path

class Imagine(commands.Cog):
    """Generate images using ComfyUI and send local output file."""

    def __init__(self, bot):
        self.bot = bot
        self.server_address = "127.0.0.1:8000"
        self.workflow_path = Path(__file__).parent / "flux_schnell-api.json"
        self.output_folder = Path(r"C:\Users\SERVER\Documents\ComfyUI\output")  # Hardcoded ComfyUI output path

    async def generate_image(self, prompt: str):
        client_id = str(uuid.uuid4())

        # Load and inject prompt into workflow
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        if "prompt" not in workflow:
            raise Exception("Workflow must contain a top-level 'prompt' key.")

        for node in workflow["prompt"].values():
            if isinstance(node, dict) and "inputs" in node:
                for k, v in node["inputs"].items():
                    if isinstance(v, str) and "{prompt}" in v:
                        node["inputs"][k] = v.replace("{prompt}", prompt)

        async with aiohttp.ClientSession() as session:
            # Submit prompt to ComfyUI
            async with session.post(f"http://{self.server_address}/prompt", json=workflow) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Prompt submission failed: {resp.status} | {text}")
                data = await resp.json()
                prompt_id = data.get("prompt_id")
                print(f"[IMAGINE] Submitted prompt_id: {prompt_id}")

            # Wait for generation via websocket
            ws_url = f"ws://{self.server_address}/ws?clientId={client_id}"
            async with session.ws_connect(ws_url) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        message = json.loads(msg.data)
                        if message.get("type") == "execution_start":
                            print(f"[IMAGINE] Generation started.")
                        if message.get("type") == "executing" and message["data"].get("prompt_id") == prompt_id and message["data"].get("node") is None:
                            print(f"[IMAGINE] Generation completed.")
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        print("[IMAGINE] WebSocket closed or errored.")
                        break

            # Fetch generated image filename
            async with session.get(f"http://{self.server_address}/history/{prompt_id}") as hist_resp:
                history = await hist_resp.json()

            outputs = history.get(prompt_id, {}).get("outputs", {})
            for node_output in outputs.values():
                for image in node_output.get("images", []):
                    filename = image.get("filename")
                    image_path = self.output_folder / filename
                    print(f"[IMAGINE] Looking for image at: {image_path}")
                    if image_path.exists():
                        print(f"[IMAGINE] ✅ Found image: {image_path}")
                        return image_path
                    else:
                        print(f"[IMAGINE] ❌ Image not found on disk: {image_path}")

            raise Exception("No valid image file found in output folder.")

    @commands.command()
    async def imagine(self, ctx, *, prompt: str):
        """Generate an image from a prompt and send it to Discord."""
        loading = await ctx.send(f"🧠 Generating image for: `{prompt}`")
        try:
            image_path = await self.generate_image(prompt)
            try:
                file = discord.File(str(image_path))
                await loading.edit(content=f"✅ Image generated for prompt: `{prompt}`")
                await ctx.send(file=file)
            except Exception as send_error:
                print(f"[IMAGINE] ❌ Discord send error: {send_error}")
                await loading.edit(content="⚠️ Image generated but could not be sent.")
        except Exception as e:
            print(f"[IMAGINE] ❌ Error: {e}")
            await loading.edit(content=f"❌ Error: {e}")
