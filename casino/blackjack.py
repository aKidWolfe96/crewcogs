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
    return f"{card[:-1]}{SUIT_EMOJIS[card[-1]]}"

def card_value(card: str) -> int:
    rank = card[0]
    if rank in "TJQK":
        return 10
    if rank == "A":
        return 11
    return int(rank)

def hand_value(cards: list[str]) -> int:
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def make_deck():
    return [f"{r}{s}" for r in "A23456789TJQK" for s in "SHDC"]

class BlackjackView(View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your game!", ephemeral=True)

        g = self.cog.games[self.ctx.author.id]
        g["player"].append(g["deck"].pop())

        if hand_value(g["player"]) > 21:
            await interaction.response.edit_message(content="You busted!", view=None)
            await self.cog.resolve(self.ctx, busted=True)
        else:
            await interaction.response.defer()
            await self.cog.show_game(self.ctx, update=interaction)

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
        ph = [deck.pop(), deck.pop()]
        dh = [deck.pop(), deck.pop()]
        self.games[ctx.author.id] = {"deck": deck, "player": ph, "dealer": dh, "bet": bet}
        await self.show_game(ctx, start=True)

    async def show_game(self, ctx, start=False, update=None):
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
        d_imgs = load_images(dh, reveal_all=False if start else True)

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
        dealer_hand = " ".join(format_card(c) for c in ([dh[0]] if start else dh))
        e = Embed(title="Blackjack")
        e.add_field(name="Your Hand", value=f"{player_hand} ({hand_value(ph)})", inline=False)
        e.add_field(name="Dealer Shows", value=f"{dealer_hand}" if start else f"{dealer_hand} ({hand_value(dh)})", inline=False)
        file = File(temp_path, filename="hand.png")
        e.set_image(url="attachment://hand.png")

        view = BlackjackView(self, ctx)

        if update:
            await update.edit_original_response(embed=e, file=file, view=view)
        else:
            await ctx.send(embed=e, file=file, view=view)

    async def resolve(self, ctx, busted=False):
        g = self.games.pop(ctx.author.id)
        ph, dh, deck, bet = g["player"], g["dealer"], g["deck"], g["bet"]
        if not busted:
            while hand_value(dh) < 17:
                dh.append(deck.pop())

        pv, dv = hand_value(ph), hand_value(dh)
        if busted or pv < dv <= 21:
            await ctx.send(f"You lose! Dealer: {' '.join(format_card(c) for c in dh)} ({dv}).")
        elif pv > dv or dv > 21:
            winnings = bet * 2
            await bank.deposit_credits(ctx.author, winnings)
            await ctx.send(f"You win! Dealer: {' '.join(format_card(c) for c in dh)} ({dv}). You earned {winnings} CrewCoin.")
        else:
            await bank.deposit_credits(ctx.author, bet)
            await ctx.send(f"Push! Dealer: {' '.join(format_card(c) for c in dh)} ({dv}). Your bet was returned.")

        u = ctx.author
        await CONFIG.user(u).total_bet.set(await CONFIG.user(u).total_bet() + bet)
        if busted or pv < dv <= 21:
            await CONFIG.user(u).total_losses.set(await CONFIG.user(u).total_losses() + 1)
        elif pv > dv or dv > 21:
            await CONFIG.user(u).total_wins.set(await CONFIG.user(u).total_wins() + 1)

    @commands.command()
    async def bjstats(self, ctx):
        """Show your blackjack stats."""
        data = await CONFIG.user(ctx.author).all()
        await ctx.send(f"Wins: {data['total_wins']}, Losses: {data['total_losses']}, Bet total: {data['total_bet']}")

def setup(bot):
    bot.add_cog(Blackjack())
