from __future__ import annotations

import asyncio
import os
import random
import tempfile
from contextlib import suppress
from typing import Dict, List, Optional, TypedDict

import discord
from PIL import Image
from redbot.core import Config, bank, commands
from .casino_core import mark_played, refund_wager, settle_game, validate_bet


CONFIG = Config.get_conf(None, identifier=1234567890)
CONFIG.register_user(total_wins=0, total_losses=0, total_bet=0)

SUIT_EMOJIS = {
    "H": "♥",
    "D": "♦",
    "S": "♠",
    "C": "♣",
}


class GameState(TypedDict, total=False):
    deck: List[str]
    player: List[str]
    dealer: List[str]
    bet: int
    message: discord.Message
    view: "BlackjackView"
    settled: bool


def format_card(card: str) -> str:
    rank = card[:-1]
    suit = card[-1]
    return f"{rank}{SUIT_EMOJIS[suit]}"


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(cards: List[str]) -> int:
    total = sum(card_value(card) for card in cards)
    aces = sum(1 for card in cards if card[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def make_deck() -> List[str]:
    ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    return [f"{rank}{suit}" for rank in ranks for suit in "SHDC"]


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "Blackjack", ctx: commands.Context):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None
        self.finished = False

    def disable_controls(self) -> None:
        for item in self.children:
            item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.finished:
            return

        self.finished = True
        self.disable_controls()
        await self.cog.expire_game(self.ctx, self)
        self.stop()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        self.cog.log.exception("Blackjack interaction failed", exc_info=error)
        if not interaction.response.is_done():
            with suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Something went wrong while handling that action. Your hand is still protected.",
                    ephemeral=True,
                )

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

        game = self.cog.games.get(self.ctx.author.id)
        if not game or game.get("settled") or game.get("view") is not self:
            self.finished = True
            self.disable_controls()
            with suppress(discord.HTTPException):
                await interaction.message.edit(view=self)
            self.stop()
            return

        game["player"].append(game["deck"].pop())

        player_total = hand_value(game["player"])
        if player_total >= 21:
            await self.cog.resolve(self.ctx, busted=player_total > 21, view=self)
        else:
            await self.cog.show_game(self.ctx, start=True, message=interaction.message, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="🛑")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.cog.resolve(self.ctx, view=self)


class Blackjack(commands.Cog):
    """Blackjack casino using Red's economy."""

    def __init__(self):
        self.games: Dict[int, GameState] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    @property
    def log(self):
        import logging

        return logging.getLogger("red.crewcogs.casino.blackjack")

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _edit_expired_message(self, game: GameState, view: BlackjackView) -> None:
        message = game.get("message") or view.message
        if message is None:
            return

        embed = message.embeds[0].copy() if message.embeds else discord.Embed(title="Blackjack")
        embed.description = "⏱️ This hand expired. The wager was refunded."
        with suppress(discord.HTTPException, discord.NotFound):
            await message.edit(embed=embed, view=view)

    @commands.command()
    async def blackjack(self, ctx: commands.Context, bet: int) -> None:
        """Start a blackjack hand for CrewCoin."""
        user_id = ctx.author.id

        async with self._lock_for(user_id):
            existing = self.games.get(user_id)
            if existing and not existing.get("settled", False):
                await ctx.send("You already have an active blackjack hand. Finish it before starting another.")
                return

            error = await validate_bet(ctx, "blackjack", bet)
            if error:
                await ctx.send(error)
                return

            try:
                await bank.withdraw_credits(ctx.author, bet)
                mark_played(ctx.guild.id, ctx.author.id, "blackjack")
            except ValueError:
                await ctx.send("Your balance changed before the wager could be placed. Try again.")
                return

            deck = make_deck()
            random.shuffle(deck)
            view = BlackjackView(self, ctx)

            game: GameState = {
                "deck": deck,
                "player": [deck.pop(), deck.pop()],
                "dealer": [deck.pop(), deck.pop()],
                "bet": bet,
                "view": view,
                "settled": False,
            }
            self.games[user_id] = game

            try:
                message = await self.show_game(ctx, start=True, view=view)
                if message is None:
                    raise RuntimeError("Blackjack message was not created")
                game["message"] = message
                view.message = message
            except Exception:
                self.games.pop(user_id, None)
                refunded = await refund_wager(ctx.author, "blackjack", bet, reason="failed to create game message")
                self.log.exception("Failed to start blackjack for user %s", user_id)
                await ctx.send(
                    "The hand could not be created. "
                    + ("Your wager was refunded." if refunded == bet else "Your refund was limited by the bank balance cap.")
                )

    def _load_card_images(self, hand: List[str], reveal_all: bool = True) -> List[Image.Image]:
        images: List[Image.Image] = []
        cards_dir = os.path.join(os.path.dirname(__file__), "cards")

        try:
            for index, card in enumerate(hand):
                filename = "back.png" if index == 1 and not reveal_all else f"{card}.png"
                path = os.path.join(cards_dir, filename)
                with Image.open(path) as source:
                    images.append(source.convert("RGBA").resize((100, 145)))
            return images
        except Exception:
            for image in images:
                image.close()
            raise

    async def show_game(
        self,
        ctx: commands.Context,
        *,
        start: bool = False,
        message: Optional[discord.Message] = None,
        view: Optional[BlackjackView] = None,
        game_override: Optional[GameState] = None,
    ) -> Optional[discord.Message]:
        game = game_override or self.games.get(ctx.author.id)
        if not game or (game.get("settled") and game_override is None):
            return None

        player_hand = game["player"]
        dealer_hand = game["dealer"]
        player_images: List[Image.Image] = []
        dealer_images: List[Image.Image] = []
        combo: Optional[Image.Image] = None
        temp_path: Optional[str] = None
        file: Optional[discord.File] = None

        try:
            player_images = self._load_card_images(player_hand)
            dealer_images = self._load_card_images(dealer_hand, reveal_all=not start)

            player_width = sum(image.width for image in player_images)
            dealer_width = sum(image.width for image in dealer_images)
            total_width = max(player_width, dealer_width)
            total_height = 145 * 2 + 20
            combo = Image.new("RGBA", (total_width, total_height), (0, 0, 0, 0))

            x = (total_width - player_width) // 2
            for image in player_images:
                combo.paste(image, (x, 155), image)
                x += image.width

            x = (total_width - dealer_width) // 2
            for image in dealer_images:
                combo.paste(image, (x, 0), image)
                x += image.width

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp:
                temp_path = temp.name
            combo.save(temp_path)

            player_text = " ".join(format_card(card) for card in player_hand)
            dealer_text = (
                " ".join(format_card(card) for card in dealer_hand)
                if not start
                else f"{format_card(dealer_hand[0])} ??"
            )

            embed = discord.Embed(title="Blackjack")
            embed.add_field(
                name="Your Hand",
                value=f"{player_text} ({hand_value(player_hand)})",
                inline=False,
            )
            embed.add_field(
                name="Dealer Shows",
                value=dealer_text if start else f"{dealer_text} ({hand_value(dealer_hand)})",
                inline=False,
            )
            embed.set_image(url="attachment://hand.png")

            file = discord.File(temp_path, filename="hand.png")
            active_view = view or game.get("view")

            if message is not None:
                await message.edit(embed=embed, attachments=[file], view=active_view)
                return message

            sent_message = await ctx.send(embed=embed, file=file, view=active_view)
            return sent_message
        finally:
            if file is not None:
                file.close()
            if temp_path:
                with suppress(OSError):
                    os.remove(temp_path)
            if combo is not None:
                combo.close()
            for image in player_images + dealer_images:
                image.close()

    async def expire_game(self, ctx: commands.Context, view: BlackjackView) -> None:
        user_id = ctx.author.id

        async with self._lock_for(user_id):
            game = self.games.get(user_id)
            if not game or game.get("settled") or game.get("view") is not view:
                return

            game["settled"] = True
            bet = game["bet"]
            deposited = await refund_wager(ctx.author, "blackjack", bet, reason="hand timeout")
            self.games.pop(user_id, None)

        await self._edit_expired_message(game, view)
        if deposited < bet:
            with suppress(discord.HTTPException):
                await ctx.send(
                    f"{ctx.author.mention}, your blackjack hand expired, but only {deposited:,} of "
                    f"the {bet:,} CrewCoin refund fit under the bank balance cap."
                )

    async def resolve(
        self,
        ctx: commands.Context,
        *,
        busted: bool = False,
        view: Optional[BlackjackView] = None,
    ) -> None:
        user_id = ctx.author.id

        async with self._lock_for(user_id):
            game = self.games.get(user_id)
            if not game or game.get("settled"):
                return
            if view is not None and game.get("view") is not view:
                return

            game["settled"] = True
            active_view = game.get("view")
            if active_view is not None:
                active_view.finished = True
                active_view.disable_controls()
                active_view.stop()

            player_hand = game["player"]
            dealer_hand = game["dealer"]
            deck = game["deck"]
            bet = game["bet"]
            message = game.get("message")

            if not busted:
                while hand_value(dealer_hand) < 17:
                    dealer_hand.append(deck.pop())

            player_value = hand_value(player_hand)
            dealer_value = hand_value(dealer_hand)
            user_config = CONFIG.user(ctx.author)

            payout = 0
            outcome = "loss"
            if busted or player_value < dealer_value <= 21:
                result = f"You lose! Dealer: {' '.join(format_card(card) for card in dealer_hand)} ({dealer_value})."
            elif player_value > dealer_value or dealer_value > 21:
                payout = bet * 2
                outcome = "win"
                result = (
                    f"You win! Dealer: {' '.join(format_card(card) for card in dealer_hand)} "
                    f"({dealer_value}). You earned {payout:,} CrewCoin."
                )
            else:
                payout = bet
                outcome = "push"
                result = (
                    f"Push! Dealer: {' '.join(format_card(card) for card in dealer_hand)} "
                    f"({dealer_value}). Your bet was returned."
                )

            settlement = await settle_game(ctx.author, "blackjack", bet, payout, outcome)
            deposited = settlement.deposited
            try:
                await user_config.total_bet.set(await user_config.total_bet() + bet)
                if outcome == "win":
                    await user_config.total_wins.set(await user_config.total_wins() + 1)
                elif outcome == "loss":
                    await user_config.total_losses.set(await user_config.total_losses() + 1)
            except Exception:
                self.log.exception("Failed to update legacy blackjack stats for user %s", user_id)
            self.games.pop(user_id, None)

        try:
            await self.show_final_board(ctx, game, message, active_view)
        except Exception:
            self.log.exception("Failed to display final blackjack board for user %s", user_id)

        if payout and deposited < payout:
            result += f" Bank balance cap allowed only {deposited:,} CrewCoin to be deposited."
        await ctx.send(result)

    async def show_final_board(
        self,
        ctx: commands.Context,
        game: GameState,
        message: Optional[discord.Message],
        view: Optional[BlackjackView],
    ) -> None:
        await self.show_game(
            ctx,
            start=False,
            message=message,
            view=view,
            game_override=game,
        )

    @commands.command()
    async def bjstats(self, ctx: commands.Context) -> None:
        """Show your blackjack stats."""
        data = await CONFIG.user(ctx.author).all()
        await ctx.send(
            f"Wins: {data['total_wins']}, Losses: {data['total_losses']}, "
            f"Bet total: {data['total_bet']:,}"
        )


async def setup(bot):
    await bot.add_cog(Blackjack())
