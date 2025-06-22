from redbot.core import commands, Config, bank
import random
import os
from discord import File, Embed
from PIL import Image
import tempfile

CONFIG = Config.get_conf(None, identifier=1234567890)
CONFIG.register_user(total_wins=0, total_losses=0, total_bet=0)

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

class Blackjack(commands.Cog):
    """Blackjack casino using Red economy."""
    def __init__(self):
        self.games = {}  # ctx.author.id ➔ game state

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

    async def show_game(self, ctx, start=False):
        g = self.games[ctx.author.id]
        ph, dh = g["player"], g["dealer"]

        images = []
        for card in ph:
            path = os.path.join(os.path.dirname(__file__), "cards", f"{card}.png")
            images.append(Image.open(path))

        total_width = sum(img.width for img in images)
        max_height = max(img.height for img in images)
        combo = Image.new("RGBA", (total_width, max_height))
        x = 0
        for img in images:
            combo.paste(img, (x, 0))
            x += img.width

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp:
            combo.save(temp.name)
            temp_path = temp.name

        e = Embed(title="Blackjack", description=f"Your hand: {ph} ({hand_value(ph)})\nDealer shows: [{dh[0]}]")
        file = File(temp_path, filename="hand.png")
        e.set_image(url="attachment://hand.png")
        await ctx.send(embed=e, file=file)

        if start:
            msg = await ctx.send("React 🔁 to Hit, ⏭️ to Stand.")
            await msg.add_reaction("🔁")
            await msg.add_reaction("⏭️")

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot or reaction.message.author.bot:
            return
        if user.id not in self.games:
            return

        emoji = str(reaction.emoji)
        g = self.games[user.id]
        deck = g["deck"]

        if emoji == "🔁":
            g["player"].append(deck.pop())
            if hand_value(g["player"]) > 21:
                await reaction.message.channel.send("You busted!")
                await self.resolve(await reaction.message.channel.fetch_message(reaction.message.id), busted=True)
            else:
                await self.show_game(await reaction.message.channel.fetch_message(reaction.message.id))

        elif emoji == "⏭️":
            await self.resolve(await reaction.message.channel.fetch_message(reaction.message.id))

    async def resolve(self, ctx, busted=False):
        g = self.games.pop(ctx.author.id)
        ph, dh, deck, bet = g["player"], g["dealer"], g["deck"], g["bet"]
        if not busted:
            while hand_value(dh) < 17:
                dh.append(deck.pop())

        pv, dv = hand_value(ph), hand_value(dh)
        if busted or pv < dv <= 21:
            await ctx.send(f"You lose! Dealer: {dh} ({dv}).")
        elif pv > dv or dv > 21:
            winnings = bet * 2
            await bank.deposit_credits(ctx.author, winnings)
            await ctx.send(f"You win! Dealer: {dh} ({dv}). You earned {winnings} CrewCoin.")
        else:
            await bank.deposit_credits(ctx.author, bet)
            await ctx.send(f"Push! Dealer: {dh} ({dv}). Your bet was returned.")

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
