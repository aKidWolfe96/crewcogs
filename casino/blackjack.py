"""Blackjack — 3:2 naturals, dealer peek, double down and split."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from contextlib import suppress
from io import BytesIO
from typing import Dict, List, Optional

import discord
from PIL import Image
from redbot.core import bank, commands

from .casino_core import (
    currency, mark_played, refund_wager, render, reply, resolve_bet, settle_game, validate_bet,
)
from .ui import GameOverView

GAME = "blackjack"
LOG = logging.getLogger("red.crewcogs.casino.blackjack")
CARDS_DIR = os.path.join(os.path.dirname(__file__), "cards")
SUIT_EMOJIS = {"H": "♥", "D": "♦", "S": "♠", "C": "♣"}
TURN_TIMEOUT = 90


def format_card(card: str) -> str:
    return f"{card[:-1]}{SUIT_EMOJIS[card[-1]]}"


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(cards: List[str]) -> int:
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_soft(cards: List[str]) -> bool:
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return aces > 0 and total <= 21


def is_blackjack(cards: List[str]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def make_deck() -> List[str]:
    ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    deck = [f"{r}{s}" for r in ranks for s in "SHDC"] * 2  # two-deck shoe
    random.shuffle(deck)
    return deck


def render_table(dealer: List[str], hands: List[List[str]], hide_hole: bool) -> bytes:
    """Dealer row on top, one row per player hand underneath."""
    card_w, card_h, gap = 100, 145, 18
    rows = [dealer] + hands
    width = max(len(r) for r in rows) * card_w
    height = len(rows) * card_h + (len(rows) - 1) * gap
    canvas = Image.new("RGBA", (max(width, card_w * 2), height), (0, 0, 0, 0))
    for row_idx, row in enumerate(rows):
        y = row_idx * (card_h + gap)
        x = (canvas.width - len(row) * card_w) // 2
        for i, card in enumerate(row):
            name = "back.png" if (row_idx == 0 and i == 1 and hide_hole) else f"{card}.png"
            with Image.open(os.path.join(CARDS_DIR, name)) as src:
                img = src.convert("RGBA").resize((card_w, card_h))
            canvas.paste(img, (x + i * card_w, y), img)
            img.close()
    out = BytesIO()
    canvas.save(out, format="PNG")
    canvas.close()
    return out.getvalue()


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "Blackjack", game: dict) -> None:
        super().__init__(timeout=TURN_TIMEOUT)
        self.cog, self.game = cog, game
        self.refresh()

    def refresh(self) -> None:
        hand = self.game["hands"][self.game["active"]]
        two_cards = len(hand["cards"]) == 2 and not hand.get("split_aces")
        self.double.disabled = not two_cards
        can_split = (
            len(self.game["hands"]) == 1 and len(hand["cards"]) == 2
            and card_value(hand["cards"][0]) == card_value(hand["cards"][1])
        )
        self.split.disabled = not can_split
        self.double.label = f"Double (+{hand['bet']:,})"
        self.split.label = f"Split (+{hand['bet']:,})"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game["ctx"].author.id:
            await interaction.response.send_message("This isn't your hand — start your own with `bj <bet>`!", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await self.cog.auto_stand(self.game, self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        LOG.exception("Blackjack interaction failed", exc_info=error)
        with suppress(discord.HTTPException):
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong — your hand is safe, try again.", ephemeral=True)

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.act(interaction, self.game, self, "hit")

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.act(interaction, self.game, self, "stand")

    @discord.ui.button(label="Double", emoji="💰", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.act(interaction, self.game, self, "double")

    @discord.ui.button(label="Split", emoji="✂️", style=discord.ButtonStyle.danger)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.act(interaction, self.game, self, "split")


class Blackjack(commands.Cog):
    """Blackjack with real casino rules."""

    def __init__(self) -> None:
        self.games: Dict[int, dict] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    @commands.command(name="blackjack", aliases=["bj", "21"])
    @commands.guild_only()
    async def blackjack(self, ctx: commands.Context, bet: str) -> None:
        """Play a hand of blackjack. Usage: `bj <bet>`

        Blackjack pays 3:2 · dealer stands on 17 · double down on any two cards ·
        split pairs once (split aces get one card each).
        """
        await self.start(ctx, bet)

    # ── Game flow ─────────────────────────────────────────────────────────────

    async def start(self, ctx: commands.Context, raw_bet, interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        async with self._lock(ctx.author.id):
            existing = self.games.get(ctx.author.id)
            if existing and not existing["settled"]:
                await reply(ctx, interaction, "You already have a hand in play — finish it first!")
                return
            error = await validate_bet(ctx, GAME, bet)
            if error:
                await reply(ctx, interaction, error)
                return
            try:
                await bank.withdraw_credits(ctx.author, bet)
                mark_played(ctx.guild.id, ctx.author.id, GAME)
            except ValueError:
                await reply(ctx, interaction, "Your balance changed — try again.")
                return
            deck = make_deck()
            game = {
                "ctx": ctx, "deck": deck, "base_bet": bet, "wagered": bet, "settled": False,
                "dealer": [deck.pop(), deck.pop()], "hands": [{"cards": [deck.pop(), deck.pop()], "bet": bet, "done": False}],
                "active": 0, "message": None,
            }
            self.games[ctx.author.id] = game
            try:
                player_bj = is_blackjack(game["hands"][0]["cards"])
                dealer_bj = is_blackjack(game["dealer"])
                # Dealer peeks for blackjack; a natural on either side ends the hand at once.
                if player_bj or dealer_bj:
                    game["hands"][0]["done"] = True
                    game["message"] = await self._send_board(game, None, reveal=True)
                    await self._settle(game)
                    return
                view = BlackjackView(self, game)
                game["message"] = await self._send_board(game, view, reveal=False)
            except Exception:
                LOG.exception("Blackjack start failed for %s", ctx.author.id)
                if not game["settled"]:
                    game["settled"] = True
                    await refund_wager(ctx.author, GAME, game["wagered"], reason="failed to start")
                await ctx.send("The hand couldn't be dealt — your wager was refunded.")

    async def act(self, interaction: discord.Interaction, game: dict, view: BlackjackView, action: str) -> None:
        ctx = game["ctx"]
        async with self._lock(ctx.author.id):
            if game["settled"] or self.games.get(ctx.author.id) is not game:
                await interaction.response.send_message("This hand is already over.", ephemeral=True)
                return
            hand = game["hands"][game["active"]]
            deck = game["deck"]
            if action in ("double", "split"):
                extra = hand["bet"]
                if action == "split" and view.split.disabled or action == "double" and view.double.disabled:
                    await interaction.response.send_message("You can't do that right now.", ephemeral=True)
                    return
                try:
                    await bank.withdraw_credits(ctx.author, extra)
                except ValueError:
                    bal = await bank.get_balance(ctx.author)
                    await interaction.response.send_message(f"You need **{extra:,}** more to {action} (you have {bal:,}).", ephemeral=True)
                    return
                game["wagered"] += extra
            await interaction.response.defer()

            if action == "hit":
                hand["cards"].append(deck.pop())
                if hand_value(hand["cards"]) >= 21:
                    hand["done"] = True
            elif action == "stand":
                hand["done"] = True
            elif action == "double":
                hand["bet"] *= 2
                hand["doubled"] = True
                hand["cards"].append(deck.pop())
                hand["done"] = True
            elif action == "split":
                first, second = hand["cards"]
                aces = first[:-1] == "A"
                game["hands"] = [
                    {"cards": [first, deck.pop()], "bet": hand["bet"], "done": aces, "split_aces": aces},
                    {"cards": [second, deck.pop()], "bet": hand["bet"], "done": aces, "split_aces": aces},
                ]
                game["active"] = 0
                for h in game["hands"]:
                    if hand_value(h["cards"]) == 21:
                        h["done"] = True

            await self._advance(game, view)

    async def auto_stand(self, game: dict, view: BlackjackView) -> None:
        async with self._lock(game["ctx"].author.id):
            if game["settled"]:
                return
            for h in game["hands"]:
                h["done"] = True
            game["timed_out"] = True
            await self._advance(game, view)

    async def _advance(self, game: dict, view: BlackjackView) -> None:
        while game["active"] < len(game["hands"]) and game["hands"][game["active"]]["done"]:
            game["active"] += 1
        if game["active"] >= len(game["hands"]):
            view.stop()
            await self._edit_board(game, None, reveal=True)
            await self._settle(game)
            return
        view.refresh()
        await self._edit_board(game, view, reveal=False)

    # ── Display ───────────────────────────────────────────────────────────────

    def _embed(self, game: dict, reveal: bool, footer: Optional[str] = None) -> discord.Embed:
        dealer = game["dealer"]
        embed = discord.Embed(title="🃏 Blackjack", color=0x2C3E50)
        dealer_text = (" ".join(format_card(c) for c in dealer) + f"  **({hand_value(dealer)})**") if reveal \
            else f"{format_card(dealer[0])} 🂠  **({card_value(dealer[0])})**"
        embed.add_field(name="Dealer", value=dealer_text, inline=False)
        multi = len(game["hands"]) > 1
        for i, h in enumerate(game["hands"]):
            value = hand_value(h["cards"])
            tags = []
            if is_blackjack(h["cards"]) and not multi:
                tags.append("**BLACKJACK!**")
            elif value > 21:
                tags.append("💥 bust")
            elif is_soft(h["cards"]) and value < 21:
                tags.append("soft")
            if h.get("doubled"):
                tags.append("doubled")
            pointer = "▶ " if (not reveal and i == game["active"]) else ""
            name = f"{pointer}Your hand{f' {i + 1}' if multi else ''} · bet {h['bet']:,}"
            embed.add_field(name=name, value=" ".join(format_card(c) for c in h["cards"])
                            + f"  **({value})** {' · '.join(tags)}", inline=True)
        embed.set_image(url="attachment://table.png")
        embed.set_footer(text=footer or "Blackjack pays 3:2 · dealer stands on 17 · double or split on your first two cards")
        return embed

    async def _file(self, game: dict, reveal: bool) -> discord.File:
        png = await render(render_table, list(game["dealer"]), [list(h["cards"]) for h in game["hands"]], not reveal)
        return discord.File(BytesIO(png), filename="table.png")

    async def _send_board(self, game: dict, view: Optional[discord.ui.View], *, reveal: bool) -> discord.Message:
        msg = await game["ctx"].send(embed=self._embed(game, reveal), file=await self._file(game, reveal),
                                     view=view or discord.utils.MISSING)
        if view is not None:
            view.message = msg  # type: ignore[attr-defined]
        return msg

    async def _edit_board(self, game: dict, view: Optional[discord.ui.View], *, reveal: bool,
                          footer: Optional[str] = None) -> None:
        msg = game["message"]
        if msg is None:
            return
        with suppress(discord.HTTPException):
            await msg.edit(embed=self._embed(game, reveal, footer), attachments=[await self._file(game, reveal)],
                           view=view)

    # ── Settlement ────────────────────────────────────────────────────────────

    async def _settle(self, game: dict) -> None:
        ctx = game["ctx"]
        if game["settled"]:
            return
        game["settled"] = True
        paid = False
        try:
            dealer, deck = game["dealer"], game["deck"]
            hands = game["hands"]
            single = len(hands) == 1
            player_natural = single and is_blackjack(hands[0]["cards"])
            dealer_natural = is_blackjack(dealer)
            live = [h for h in hands if hand_value(h["cards"]) <= 21]
            if live and not player_natural and not dealer_natural:
                while hand_value(dealer) < 17:
                    dealer.append(deck.pop())
            dv = hand_value(dealer)
            payout, lines = 0, []
            for i, h in enumerate(hands):
                pv = hand_value(h["cards"])
                label = f"Hand {i + 1}: " if not single else ""
                if player_natural and not dealer_natural:
                    win = int(h["bet"] * 2.5)
                    payout += win
                    lines.append(f"🎉 {label}**Blackjack!** Paid 3:2 (+{win - h['bet']:,})")
                elif dealer_natural and player_natural:
                    payout += h["bet"]
                    lines.append(f"🤝 {label}Both have blackjack — push.")
                elif dealer_natural:
                    lines.append(f"💸 {label}Dealer has blackjack.")
                elif pv > 21:
                    lines.append(f"💥 {label}Bust ({pv}).")
                elif dv > 21 or pv > dv:
                    payout += h["bet"] * 2
                    lines.append(f"🎉 {label}You win {pv} vs {dv if dv <= 21 else 'bust'} (+{h['bet']:,})")
                elif pv == dv:
                    payout += h["bet"]
                    lines.append(f"🤝 {label}Push at {pv}.")
                else:
                    lines.append(f"💸 {label}Dealer wins {dv} vs {pv}.")
            wagered = game["wagered"]
            outcome = "win" if payout > wagered else ("push" if payout == wagered else "loss")
            settlement = await settle_game(ctx.author, GAME, wagered, payout, outcome, channel=ctx.channel)
            paid = True
            cur = await currency(ctx.guild)
            net = settlement.deposited - wagered
            balance = await bank.get_balance(ctx.author)
            if game.get("timed_out"):
                lines.insert(0, "⏱️ Time ran out — your hands were stood automatically.")
            if settlement.capped:
                lines.append("_Payout limited by the bank balance cap._")
            summary = "\n".join(lines) + f"\n\n**Net: {net:+,} {cur}** · Balance {balance:,}"
            embed = self._embed(game, True, footer="Tap a button to deal again")
            embed.description = summary
            embed.color = 0x2ECC71 if net > 0 else (0x95A5A6 if net == 0 else 0xE74C3C)
            view = GameOverView(ctx.author.id, game["base_bet"], lambda i, b: self.start(ctx, b, i), again_label="Deal")
            msg = game["message"]
            if msg is not None:
                await msg.edit(embed=embed, attachments=[await self._file(game, True)], view=view)
                view.message = msg
            else:
                view.message = await ctx.send(embed=embed, view=view)
        except Exception:
            LOG.exception("Blackjack settlement failed for %s", ctx.author.id)
            if not paid:
                await refund_wager(ctx.author, GAME, game["wagered"], reason="settlement failure")
                with suppress(discord.HTTPException):
                    await ctx.send("Blackjack hit an error settling your hand — your wager was refunded.")
        finally:
            if self.games.get(ctx.author.id) is game:
                self.games.pop(ctx.author.id, None)


async def setup(bot):
    await bot.add_cog(Blackjack())
