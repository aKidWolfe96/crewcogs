# KrewFantasy

A Sleeper fantasy-football cog for Red-DiscordBot/Krusty.

## What it does

- Sleeper league setup with no API key
- Discord member ↔ Sleeper roster mapping
- Scores and projected matchup previews
- Official standings
- Immediate trade announcements
- Weekly waiver report + FAAB balances
- Sunday starter injury / bye / empty-slot warnings
- 10 weekly trophies
- Legal optimal-lineup solver and manager efficiency
- Points-left-on-bench tracking
- All-play / Win Matrix standings
- Krusty Luck Index
- Season power rankings
- Trophy Case
- Weekly generated recap
- Championship + consolation playoff brackets
- Sleeper trending adds/drops
- PNG history charts
- Automatic weekly schedule
- Persistent season archive using Red Config

## Install through your CrewCogs repo

Once this folder is committed to `aKidWolfe96/crewcogs`:

```text
[p]repo update crewcogs
[p]cog install crewcogs krewfantasy
[p]load krewfantasy
```

For a manual local install, place the `krewfantasy` folder inside a Red cog path and run:

```text
[p]load krewfantasy
```

## First-time setup

```text
!ff setup https://sleeper.com/leagues/YOUR_LEAGUE_ID
!ff channel #fantasy-football
!ff timezone America/New_York

!ff link @Aaron Certified Ball Fondlers
!ff link @Mike Mike's Team
```

`!ff setup` validates the league and archives completed weeks automatically. It also marks existing current-week trades as already seen so the bot does not spam old trades into the channel.

## Main commands

```text
!ff
!ff scores [week]
!ff matchup [week]
!ff standings
!ff power
!ff matrix
!ff luck
!ff management
!ff trophies [week]
!ff trophycase
!ff waivers [week]
!ff trades [week]
!ff faab
!ff lineup [week]
!ff team [@member]
!ff recap [week]
!ff playoff
!ff trending [add|drop]
!ff chart [power|scores|standings|luck|efficiency]
!ff schedule
!ff status
```

Admin commands:

```text
!ff setup <league URL/ID>
!ff channel [#channel]
!ff timezone <IANA timezone>
!ff link @member <Sleeper team/owner>
!ff unlink @member
!ff toggle <report> [true|false]
!ff roast <off|mild|ruthless>
!ff sync
!ff finalize [week]
!ff refreshplayers
!ff test <report>
```

## Automatic schedule

Default timezone is `America/New_York` and is configurable per Discord server.

- Tuesday 9:00 AM — final scores, awards, recap
- Tuesday 7:00 PM — power rankings
- Wednesday 9:00 AM — standings + all-play matrix
- Wednesday 11:00 AM — waiver report
- Thursday 7:00 PM — matchup preview
- Sunday 8:30 AM — starting-lineup alerts
- Sunday 4:15 PM and 8:15 PM — score updates
- Monday 9:00 AM and 7:00 PM — score updates
- Trades — checked every 5 minutes and posted when newly completed

Every automated report can be turned on/off individually with `!ff toggle`.

## Data sources / reliability

The core features use Sleeper's documented read-only v1 API. Sleeper does not require an API key. The player map is cached locally for 24 hours.

Projected matchup totals and NFL schedule/bye detection use Sleeper feeds that are not part of the documented v1 surface. Those features fail gracefully: if those feeds change, the documented score, standings, roster, transaction, playoff and historical-analysis features continue working.

## Notes on weekly awards

The 10 awards are:

1. King of the Week — highest score
2. Certified Bum — lowest score
3. Murder Scene — biggest win
4. Barely Survived — closest win
5. Lucky Bastard — win result most above all-play expectation
6. Robbed — result most below all-play expectation
7. Overachiever — most above projected score (league-average fallback if projections unavailable)
8. Underachiever — most below projected score (league-average fallback if projections unavailable)
9. Best Manager — best legal-lineup efficiency
10. Bench Coach — most legal points left on the bench

The optimal-lineup calculation honors Sleeper lineup slots including FLEX, SUPER_FLEX, REC_FLEX, WRRB_FLEX and common IDP slots.

## License

This implementation is original code designed around public Sleeper API behavior. It does not copy GameDayBot source code.
