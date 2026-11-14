"""Discord UI for the UFC cog: tap-to-pick editor behind a persistent button."""

from __future__ import annotations

import io
import logging
import time
from typing import TYPE_CHECKING

import discord

from .scoring import fight_key, fmt_odds, method_choice_label, method_choices

if TYPE_CHECKING:
    from .ufc import UFC

log = logging.getLogger("red.akidwolfe.fightnight")

PICKS_ID = "fightnight:picks:open"
MINE_ID = "fightnight:picks:mine"
PER_PAGE = 2


def png_file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=f"{name}.png")


def is_locked(fight: dict) -> bool:
    if fight.get("completed") or fight.get("state") not in ("pre", "", None):
        return True
    start = fight.get("start")
    return bool(start and time.time() >= start)


class CardView(discord.ui.View):
    """Persistent buttons under every card post (survive restarts)."""

    def __init__(self, cog: "UFC"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Make my picks", emoji="🗳️", style=discord.ButtonStyle.danger, custom_id=PICKS_ID)
    async def picks(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await self.cog.open_pick_editor(interaction)

    @discord.ui.button(label="My picks", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=MINE_ID)
    async def mine(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await self.cog.show_my_picks(interaction)


class PickEditor(discord.ui.View):
    """Private editor: 2 fights per page, winner + optional method/round. Saves on every change."""

    def __init__(self, cog: "UFC", guild: discord.Guild, user, event: dict, mine: dict):
        super().__init__(timeout=1200)
        self.cog, self.guild, self.user, self.event = cog, guild, user, event
        self.open_fights = [f for f in event.get("fights", []) if not is_locked(f)]
        self.mine = mine  # {fight_key: {"pick": name, "method": "ko:2"|"dec"|None}}
        self.page = 0
        self._build()

    @property
    def pages(self) -> int:
        return max(1, (len(self.open_fights) + PER_PAGE - 1) // PER_PAGE)

    def caption(self) -> str:
        total = len(self.open_fights)
        done = sum(1 for f in self.open_fights if (self.mine.get(fight_key(f)) or {}).get("pick"))
        lines = [f"🗳️ **{self.event.get('shortname', 'UFC')} picks** · {done}/{total} picked · "
                 "each fight locks when its part of the card starts."]
        for f in self.open_fights[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]:
            m = self.mine.get(fight_key(f)) or {}
            pick = m.get("pick")
            how = f" · {method_choice_label(m['method'])}" if m.get("method") else ""
            lines.append(f"\n**{f['red']}** vs **{f['blue']}** · *{f.get('weight_class', '')}*\n"
                         f"╰ {'✅ ' + pick + how if pick else '⬜ no pick yet'}")
        lines.append("\n-# 1 point per correct winner · +1 for the right method · +1 for the right round")
        return "\n".join(lines)

    def _build(self):
        self.clear_items()
        fights = self.open_fights[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]
        for i, f in enumerate(fights):
            key = fight_key(f)
            m = self.mine.get(key) or {}
            winner = discord.ui.Select(
                placeholder=f"Who wins? {f['red']} vs {f['blue']}"[:150], row=i * 2,
                options=[
                    discord.SelectOption(
                        label=f[side][:100], value=side, emoji="🔴" if side == "red" else "🔵",
                        description=(f"{f.get(side + '_record') or ''}  ·  odds {fmt_odds(f.get(side + '_odds'))}")[:100],
                        default=m.get("pick") == f[side],
                    ) for side in ("red", "blue")
                ],
            )
            winner.callback = self._winner_cb(f)
            self.add_item(winner)
            how = discord.ui.Select(
                placeholder="How? (optional bonus: method + round)", row=i * 2 + 1,
                options=[discord.SelectOption(label=method_choice_label(v), value=v, default=m.get("method") == v)
                         for v in method_choices(f.get("rounds") or 3)] +
                        [discord.SelectOption(label="No bonus pick", value="none", default=False)],
            )
            how.callback = self._method_cb(f)
            self.add_item(how)
        prev_b = discord.ui.Button(emoji="◀️", row=4, style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        label = discord.ui.Button(label=f"Page {self.page + 1}/{self.pages}", row=4, disabled=True,
                                  style=discord.ButtonStyle.secondary)
        next_b = discord.ui.Button(emoji="▶️", row=4, style=discord.ButtonStyle.secondary,
                                   disabled=self.page >= self.pages - 1)
        prev_b.callback = self._nav(-1)
        next_b.callback = self._nav(1)
        for b in (prev_b, label, next_b):
            self.add_item(b)

    def _nav(self, step):
        async def cb(interaction: discord.Interaction):
            self.page = max(0, min(self.pages - 1, self.page + step))
            self._build()
            await interaction.response.edit_message(content=self.caption(), view=self)
        return cb

    def _winner_cb(self, fight):
        async def cb(interaction: discord.Interaction):
            if is_locked(fight):
                return await interaction.response.send_message("⏱️ That fight has started, picks are locked.", ephemeral=True)
            side = interaction.data["values"][0]
            picked = fight[side]
            await self.cog.save_pick(self.guild, self.event, fight, self.user, picked)
            self.mine.setdefault(fight_key(fight), {})["pick"] = picked
            self._build()
            await interaction.response.edit_message(content=self.caption(), view=self)
        return cb

    def _method_cb(self, fight):
        async def cb(interaction: discord.Interaction):
            if is_locked(fight):
                return await interaction.response.send_message("⏱️ That fight has started, picks are locked.", ephemeral=True)
            value = interaction.data["values"][0]
            entry = self.mine.setdefault(fight_key(fight), {})
            if not entry.get("pick"):
                return await interaction.response.send_message("Pick a winner for that fight first.", ephemeral=True)
            method = None if value == "none" else value
            await self.cog.save_method(self.guild, self.event, fight, self.user, method)
            entry["method"] = method
            self._build()
            await interaction.response.edit_message(content=self.caption(), view=self)
        return cb
