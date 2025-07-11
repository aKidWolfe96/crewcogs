from redbot.core import commands, Config, bank
import random
import os
from discord import File, Embed
from PIL import Image
import tempfile
import discord
from discord.ui import View, Button

CONFIG = Config.get_conf(None, identifier=1234567890)
CONFIG.register_user(total_wins=0, total_losses=0, total_bet=0)

SUIT_EMOJIS = {
    "H": "♥",
    "D": "♦",
    "S": "♠",
    "C": "♣"
}

def format_card(card: str) -> str:
    rank = card[:-1]
    suit = card[-1]
    return f"{rank}{SUIT_EMOJIS[suit]}"

def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ["J", "Q", "K"]:
        return 10
    if rank == "A":
        return 11
    return int(rank)

def hand_value(cards: list[str]) -> int:
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def make_deck():
    return [f"{r}{s}" for r in ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"] for s in "SHDC"]

class BlackjackView(View):
    def __init__(self, cog, ctx, message):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.message = message

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your game!", ephemeral=True)

        await interaction.response.defer()

        g = self.cog.games[self.ctx.author.id]
        g["player"].append(g["deck"].pop())

        await self.cog.show_game(self.ctx, message=self.message)

        if hand_value(g["player"]) > 21:
            await interaction.message.edit(view=None)
            await self.cog.resolve(self.ctx, busted=True)
        elif hand_value(g["player"]) == 21:
            await interaction.message.edit(view=None)
            await self.cog.resolve(self.ctx)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="🛑")
    async def stand(self, interaction: discord.Interaction, button: Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your game!", ephemeral=True)

        await interaction.response.defer()

        await self.cog.resolve(self.ctx)
        await interaction.message.edit(view=None)

class Blackjack(commands.Cog):
    """Blackjack casino using Red economy."""
    def __init__(self):
        self.games = {}

    @commands.command()
    async def blackjack(self, ctx, bet: int):
        """Start a blackjack hand for CrewCoin."""
        bal = await bank.get_balance(ctx.author)
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        if bet > bal:
            return await ctx.send("Not enough CrewCoin.")

        await bank.withdraw_credits(ctx.author, bet)
        deck = make_deck()
        random.shuffle(deck)

        self.games[ctx.author.id] = {
            "deck": deck,
            "player": [deck.pop(), deck.pop()],
            "dealer": [deck.pop(), deck.pop()],
            "bet": bet
        }

        await self.show_game(ctx, start=True)

    async def show_game(self, ctx, start=False, message=None, interaction=None):
        g = self.games[ctx.author.id]
        ph, dh = g["player"], g["dealer"]

        def load_images(hand, reveal_all=True):
            imgs = []
            for idx, card in enumerate(hand):
                if idx == 1 and not reveal_all:
                    path = os.path.join(os.path.dirname(__file__), "cards", "back.png")
                else:
                    path = os.path.join(os.path.dirname(__file__), "cards", f"{card}.png")
                imgs.append(Image.open(path).resize((100, 145)))
            return imgs

        p_imgs = load_images(ph)
        d_imgs = load_images(dh, reveal_all=not start)

        pw = sum(img.width for img in p_imgs)
        dw = sum(img.width for img in d_imgs)
        total_width = max(pw, dw)
        total_height = 145 * 2 + 20
        combo = Image.new("RGBA", (total_width, total_height), (0, 0, 0, 0))

        x = (total_width - pw) // 2
        for img in p_imgs:
            combo.paste(img, (x, 155))
            x += img.width

        x = (total_width - dw) // 2
        for img in d_imgs:
            combo.paste(img, (x, 0))
            x += img.width

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp:
            combo.save(temp.name)
            temp_path = temp.name

        player_hand = " ".join(format_card(c) for c in ph)
        dealer_hand = " ".join(format_card(c) for c in dh) if not start else format_card(dh[0]) + " ??"

        e = Embed(title="Blackjack")
        e.add_field(name="Your Hand", value=f"{player_hand} ({hand_value(ph)})", inline=False)
        e.add_field(name="Dealer Shows", value=f"{dealer_hand}" if start else f"{dealer_hand} ({hand_value(dh)})", inline=False)
        file = File(temp_path, filename="hand.png")
        e.set_image(url="attachment://hand.png")

        if message:
            view = BlackjackView(self, ctx, message)
            await message.edit(embed=e, attachments=[file], view=view)
        else:
            sent_msg = await ctx.send(embed=e, file=file)
            view = BlackjackView(self, ctx, sent_msg)
            await sent_msg.edit(view=view)

    async def resolve(self, ctx, busted=False):
        g = self.games.pop(ctx.author.id)
        ph, dh, deck, bet = g["player"], g["dealer"], g["deck"], g["bet"]

        if not busted:
            while hand_value(dh) < 17:
                dh.append(deck.pop())

        pv, dv = hand_value(ph), hand_value(dh)
        user_cfg = CONFIG.user(ctx.author)

        result_msg = ""
        if busted or pv < dv <= 21:
            result_msg = f"You lose! Dealer: {' '.join(format_card(c) for c in dh)} ({dv})."
            await user_cfg.total_losses.set(await user_cfg.total_losses() + 1)
        elif pv > dv or dv > 21:
            winnings = bet * 2
            result_msg = f"You win! Dealer: {' '.join(format_card(c) for c in dh)} ({dv}). You earned {winnings} CrewCoin."
            await bank.deposit_credits(ctx.author, winnings)
            await user_cfg.total_wins.set(await user_cfg.total_wins() + 1)
        else:
            result_msg = f"Push! Dealer: {' '.join(format_card(c) for c in dh)} ({dv}). Your bet was returned."
            await bank.deposit_credits(ctx.author, bet)

        await user_cfg.total_bet.set(await user_cfg.total_bet() + bet)
        await ctx.send(result_msg)

    @commands.command()
    async def bjstats(self, ctx):
        """Show your blackjack stats."""
        data = await CONFIG.user(ctx.author).all()
        await ctx.send(
            f"Wins: {data['total_wins']}, Losses: {data['total_losses']}, Bet total: {data['total_bet']}"
        )

def setup(bot):
    bot.add_cog(Blackjack())
