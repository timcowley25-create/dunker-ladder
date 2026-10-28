"""
PlayHQ Ladder Scraper - Darlington Dunkers U10 Mixed Orange
Scrapes all rounds from the division page and builds a ladder.
"""

import asyncio
import json
import re
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

BASE_URL = "https://www.playhq.com/basketball-wa/org/darlington-basketball-club/hills-raiders-domestic-competition-summer-202627/saturday-u10-mixed-orange/3c18840f"
STATS_URL = "https://www.playhq.com/basketball-wa/org/hills-raiders-basketball-association/hills-raiders-domestic-competition-summer-202627/saturday-u10-mixed-orange/3c18840f/statistics?sort=TOTAL_SCORE:DESC"
DARLINGTON_TEAM = "Darlington Dunkers"  # partial match — covers "U10 Mixed Darlington Dunkers"
OUTPUT_DIR = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)


async def scrape_round(page, round_num):
    """Visit a round page and extract game results."""
    url = f"{BASE_URL}/R{round_num}"
    print(f"\n  Fetching Round {round_num}: {url}")

    try:
        response = await page.goto(url, timeout=30000)
        if response and response.status == 404:
            print(f"  Round {round_num}: 404, stopping.")
            return None

        # Wait for JS to render
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(3000)

    except Exception as e:
        print(f"  Could not load Round {round_num}: {e}")
        return None

    # Dump full page text for debugging rounds 1-3
    full_text = await page.inner_text("body")
    if round_num <= 3:
        print(f"  --- PAGE TEXT (first 2000 chars) ---")
        print(full_text[:2000])
        print(f"  --- END PAGE TEXT ---")

    # Check for "no fixture" signals
    lower = full_text.lower()
    no_fixture_signals = ["no fixture", "no games scheduled", "no rounds", "page not found", "404"]
    if any(s in lower for s in no_fixture_signals):
        print(f"  Round {round_num}: no fixture signal found, stopping.")
        return None

    # If the page has no digits at all, probably nothing loaded
    if not re.search(r'\d', full_text):
        print(f"  Round {round_num}: no numeric content found.")
        return None

    # Take screenshot for first round to help debug
    if round_num == 1:
        await page.screenshot(path=str(OUTPUT_DIR / "debug_r1.png"), full_page=True)
        print("  Screenshot saved to docs/debug_r1.png")

    # Try structured element extraction first
    games = await extract_from_elements(page, round_num)

    # Fall back to full-page text parsing
    if not games:
        print(f"  Trying full-page text parsing...")
        games = parse_page_text(full_text, round_num)

    if games:
        for g in games:
            print(f"    ✓ {g['home_team']} {g['home_score']} – {g['away_score']} {g['away_team']}")
        return games
    else:
        print(f"  Round {round_num}: no games parsed.")
        # Don't stop — future rounds might have data (e.g. round 1 is a bye)
        return []


async def extract_from_elements(page, round_num):
    """Try to extract games from DOM elements."""
    games = []

    # PlayHQ uses various class names — try a broad set
    selectors = [
        "[class*='GameSummary']",
        "[class*='game-summary']",
        "[class*='FixtureRow']",
        "[class*='GameCard']",
        "[class*='game-card']",
        "[class*='fixture-row']",
        "[class*='match-card']",
        "[class*='MatchCard']",
        "article",
    ]

    for selector in selectors:
        elements = await page.query_selector_all(selector)
        if not elements:
            continue
        print(f"  Selector '{selector}' found {len(elements)} elements")
        for el in elements:
            try:
                text = await el.inner_text()
                game = parse_game_from_block(text, round_num)
                if game:
                    games.append(game)
            except Exception:
                continue
        if games:
            return games

    return games


def parse_game_from_block(text, round_num):
    """
    Parse a text block for a single game.
    PlayHQ typically renders: TeamA \n Score \n TeamB \n Score
    or inline: TeamA 24 - 18 TeamB
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    # Remove noise words
    noise = {"vs", "VS", "v", "-", "–", "FINAL", "Final", "Completed",
              "forfeit", "Forfeit", "BYE", "Bye", "bye", "TBC", "tbc"}
    lines = [l for l in lines if l not in noise]

    # Pattern 1: blocks with [TeamA, scoreA, TeamB, scoreB]
    scores = []
    teams = []
    for line in lines:
        if re.match(r'^\d{1,3}$', line):
            scores.append(int(line))
        elif len(line) > 2 and not re.match(r'^\d{1,3}$', line) and len(line) < 60:
            teams.append(line)

    if len(teams) >= 2 and len(scores) >= 2:
        return {
            "round": round_num,
            "home_team": teams[0],
            "away_team": teams[1],
            "home_score": scores[0],
            "away_score": scores[1],
        }

    # Pattern 2: inline "TeamA 24 - 18 TeamB" or "TeamA 24 – 18 TeamB"
    match = re.search(r'([A-Za-z][A-Za-z0-9 ]{2,40}?)\s+(\d{1,3})\s*[-–]\s*(\d{1,3})\s+([A-Za-z][A-Za-z0-9 ]{2,40})', text)
    if match:
        return {
            "round": round_num,
            "home_team": match.group(1).strip(),
            "home_score": int(match.group(2)),
            "away_score": int(match.group(3)),
            "away_team": match.group(4).strip(),
        }

    return None


def parse_page_text(full_text, round_num):
    """
    Full-page text fallback. Looks for score patterns across the whole body.
    PlayHQ often renders scores as adjacent lines like:
      Darlington Dunkers
      14
      Hills Raiders
      8
    """
    games = []
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]

    # Team names on PlayHQ often include digits e.g. "U10 Mixed Darlington Dunkers", "Mustangs U10 Mixed 1"
    TEAM_RE = re.compile(r'^[A-Za-z][A-Za-z0-9 ]{2,60}$')

    # Strategy: scan for pairs of (team, score, team, score) in nearby lines
    i = 0
    while i < len(lines) - 3:
        # Look for a score-like number
        if re.match(r'^\d{1,3}$', lines[i]):
            score1 = int(lines[i])
            # Find team before this score
            team1 = None
            for j in range(i - 1, max(i - 5, -1), -1):
                if TEAM_RE.match(lines[j]):
                    team1 = lines[j]
                    break
            if not team1:
                i += 1
                continue
            # Find next score and team
            for k in range(i + 1, min(i + 8, len(lines))):
                if re.match(r'^\d{1,3}$', lines[k]):
                    score2 = int(lines[k])
                    team2 = None
                    for m in range(k - 1, i, -1):
                        if TEAM_RE.match(lines[m]) and lines[m] != team1:
                            team2 = lines[m]
                            break
                    if team2:
                        game = {
                            "round": round_num,
                            "home_team": team1,
                            "away_team": team2,
                            "home_score": score1,
                            "away_score": score2,
                        }
                        # Avoid duplicates
                        if not any(g["home_team"] == team1 and g["away_team"] == team2 for g in games):
                            games.append(game)
                        i = k + 1
                        break
                else:
                    continue
            else:
                i += 1
                continue
        else:
            i += 1

    # Also try inline score patterns
    inline_pattern = re.compile(
        r'([A-Za-z][A-Za-z0-9 ]{2,40}?)\s+(\d{1,3})\s*[-–]\s*(\d{1,3})\s+([A-Za-z][A-Za-z0-9 ]{2,40})'
    )
    for match in inline_pattern.finditer(full_text):
        t1, s1, s2, t2 = match.group(1).strip(), int(match.group(2)), int(match.group(3)), match.group(4).strip()
        if not any(g["home_team"] == t1 and g["away_team"] == t2 for g in games):
            games.append({"round": round_num, "home_team": t1, "away_team": t2,
                          "home_score": s1, "away_score": s2})

    return games


def calculate_ladder(all_games):
    """Build ladder from all game results."""
    teams = {}

    for game in all_games:
        home = game["home_team"]
        away = game["away_team"]
        hs = game["home_score"]
        as_ = game["away_score"]

        if hs is None or as_ is None:
            continue

        for team in [home, away]:
            if team not in teams:
                teams[team] = {"P": 0, "W": 0, "L": 0, "D": 0, "PF": 0, "PA": 0, "Pts": 0}

        teams[home]["P"] += 1
        teams[away]["P"] += 1
        teams[home]["PF"] += hs
        teams[home]["PA"] += as_
        teams[away]["PF"] += as_
        teams[away]["PA"] += hs

        if hs > as_:
            teams[home]["W"] += 1
            teams[away]["L"] += 1
            teams[home]["Pts"] += 2
        elif as_ > hs:
            teams[away]["W"] += 1
            teams[home]["L"] += 1
            teams[away]["Pts"] += 2
        else:
            teams[home]["D"] += 1
            teams[away]["D"] += 1
            teams[home]["Pts"] += 1
            teams[away]["Pts"] += 1

    ladder = []
    for team, stats in teams.items():
        pct = (stats["PF"] / stats["PA"] * 100) if stats["PA"] > 0 else 0
        ladder.append({"team": team, **stats, "PCT": round(pct, 1)})

    ladder.sort(key=lambda x: (-x["Pts"], -x["PCT"]))
    return ladder


async def scrape_stats(context):
    """
    Scrape player stats by reading the rendered DOM table via JavaScript.
    PlayHQ renders a proper <table> for stats — we extract each row's cells directly.
    """
    print(f"\n  Fetching stats page: {STATS_URL}")
    page = await context.new_page()

    try:
        await page.goto(STATS_URL, timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(5000)  # extra wait for React rendering
    except Exception as e:
        print(f"  Stats page load error: {e}")
        await page.close()
        return []

    # Debug: print first 500 chars of page text
    try:
        body_text = await page.inner_text("body")
        print(f"  Page text preview:\n{body_text[:500]}\n---")
    except Exception:
        pass

    # Extract table rows via JavaScript — much more reliable than text parsing
    players = await page.evaluate("""
    () => {
        const players = [];
        // Try every table row on the page
        const rows = document.querySelectorAll('tr');
        for (const row of rows) {
            const cells = Array.from(row.querySelectorAll('td'));
            if (cells.length < 4) continue;
            const texts = cells.map(c => c.innerText.trim());
            // We expect: [name_or_rank, name, team, gp, tp, 1pt, 2pt, fouls]
            // or:        [name, team, gp, tp, 1pt, 2pt, fouls]
            // Find columns: look for a cell that's all digits for GP (small number like 1-30)
            // and another for TP
            let name = '', team = '', gp = 0, tp = 0, pt1 = 0, pt2 = 0, fouls = 0;
            const nums = texts.map(t => ({ t, n: /^\\d+$/.test(t) ? parseInt(t) : null }));
            const numIdxs = nums.map((x,i) => x.n !== null ? i : -1).filter(i => i >= 0);
            if (numIdxs.length < 2) continue;
            // Find GP+TP pair: GP is a small number (1-25), TP >= GP, consecutive numeric cols
            let gpIdx = -1, tpIdx = -1;
            for (let k = 0; k < numIdxs.length - 1; k++) {
                const a = nums[numIdxs[k]].n, b = nums[numIdxs[k+1]].n;
                if (a >= 1 && a <= 25 && b >= a) {
                    gpIdx = numIdxs[k]; tpIdx = numIdxs[k+1]; break;
                }
            }
            if (gpIdx < 0) continue;
            gp = nums[gpIdx].n;
            tp = nums[tpIdx].n;
            // Collect remaining numerics for 1PT, 2PT, fouls
            const restIdxs = numIdxs.filter(i => i > tpIdx);
            pt1 = restIdxs[0] !== undefined ? nums[restIdxs[0]].n : 0;
            pt2 = restIdxs[1] !== undefined ? nums[restIdxs[1]].n : 0;
            fouls = restIdxs[2] !== undefined ? nums[restIdxs[2]].n : 0;
            // Texts before GP that aren't pure numbers = [rank_maybe, player_name, team_name]
            const textsBefore = texts.slice(0, gpIdx).filter(t => !/^\\d+$/.test(t) && t.length > 2);
            if (textsBefore.length === 0) continue;
            // PlayHQ order in cells: player_name | team_name | GP | TP | ...
            team = textsBefore[textsBefore.length - 1];
            name = textsBefore.length > 1 ? textsBefore[textsBefore.length - 2] : textsBefore[0];
            // Sanity checks
            if (tp > 200 || gp > 25) continue;
            if (name.length < 3 || /private/i.test(name)) { name = name || 'Private Player'; }
            players.push({ name, team, gp, tp, pt1, pt2, fouls });
        }
        return players;
    }
    """)

    await page.close()

    # Deduplicate — keep highest TP per player
    seen = {}
    for p in players:
        key = p["name"]
        if key not in seen or p["tp"] > seen[key]["tp"]:
            seen[key] = p
    result = sorted(seen.values(), key=lambda x: -x["tp"])

    print(f"  ✓ {len(result)} players extracted from DOM")
    if result:
        for p in result[:5]:
            print(f"    {p['name']} — GP:{p['gp']} TP:{p['tp']}")
    return result


def generate_html(ladder, all_games, last_updated, players=None):
    """Generate the ladder HTML page."""
    darlington_highlight = DARLINGTON_TEAM.lower()
    players = players or []

    # Build stats table rows (sorted by TP desc)
    sorted_players = sorted(players, key=lambda p: -p.get("tp", 0))
    stat_rows = ""
    for i, p in enumerate(sorted_players[:25], 1):  # top 25
        is_dunker = darlington_highlight in p.get("team", "").lower()
        row_class = "darlington" if is_dunker else ""
        stat_rows += f"""
        <tr class="{row_class}">
            <td class="pos">{i}</td>
            <td class="team">{p['name']}{'  🟠' if is_dunker else ''}</td>
            <td style="font-size:0.75rem;color:#6b7280">{p.get('team','')}</td>
            <td>{p.get('gp',0)}</td>
            <td class="pts">{p.get('tp',0)}</td>
            <td>{p.get('pt1',0)}</td>
            <td>{p.get('pt2',0)}</td>
            <td>{p.get('fouls',0)}</td>
        </tr>"""

    stats_section = ""
    if stat_rows:
        stats_section = f"""
  <div style="margin-top:20px">
    <button id="statsToggle" onclick="toggleStats()" style="
      background:var(--orange);color:white;border:none;border-radius:8px;
      padding:10px 18px;font-size:0.9rem;font-weight:700;cursor:pointer;
      display:flex;align-items:center;gap:8px;">
      📊 Top Player Stats <span id="statsArrow">▼</span>
    </button>
    <div id="statsSection" style="display:none;margin-top:10px">
      <h2>Top Player Stats (Points)</h2>
      <div class="card">
        <table>
          <thead><tr>
            <th>Pos</th><th class="team">Player</th><th class="team">Team</th>
            <th>GP</th><th>TP</th><th>1PT</th><th>2PT</th><th>Fouls</th>
          </tr></thead>
          <tbody>{stat_rows}</tbody>
        </table>
      </div>
    </div>
  </div>
  <script>
    function toggleStats() {{
      var s = document.getElementById('statsSection');
      var a = document.getElementById('statsArrow');
      if (s.style.display === 'none') {{ s.style.display = 'block'; a.textContent = '▲'; }}
      else {{ s.style.display = 'none'; a.textContent = '▼'; }}
    }}
  </script>"""

    rows = ""
    for i, t in enumerate(ladder, 1):
        is_darlington = darlington_highlight in t["team"].lower()
        row_class = "darlington" if is_darlington else ""
        rows += f"""
        <tr class="{row_class}">
            <td class="pos">{i}</td>
            <td class="team">{t['team']}{'  🟠' if is_darlington else ''}</td>
            <td>{t['P']}</td>
            <td>{t['W']}</td>
            <td>{t['L']}</td>
            <td>{t['D']}</td>
            <td>{t['PF']}</td>
            <td>{t['PA']}</td>
            <td>{t['PCT']}%</td>
            <td class="pts">{t['Pts']}</td>
        </tr>"""

    recent = sorted(all_games, key=lambda g: -g["round"])[:10]
    result_rows = ""
    for g in recent:
        home_win = g["home_score"] > g["away_score"]
        away_win = g["away_score"] > g["home_score"]
        result_rows += f"""
        <tr>
            <td class="rnd">R{g['round']}</td>
            <td class="team {'winner' if home_win else ''}">{g['home_team']}</td>
            <td class="score">{g['home_score']} – {g['away_score']}</td>
            <td class="team {'winner' if away_win else ''}">{g['away_team']}</td>
        </tr>"""

    total_rounds = max((g["round"] for g in all_games), default=0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>U10 Mixed Orange Ladder – Hills Raiders 2026/27</title>
<style>
  :root {{
    --orange: #f47c20;
    --dark: #1a1a2e;
    --card: #ffffff;
    --bg: #f0f2f5;
    --text: #1a1a2e;
    --muted: #6b7280;
    --highlight: #fff3e0;
    --highlight-border: #f47c20;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }}
  header {{ background: var(--dark); color: white; padding: 20px 16px; }}
  header h1 {{ font-size: 1.2rem; font-weight: 700; }}
  header p {{ font-size: 0.8rem; color: #9ca3af; margin-top: 4px; }}
  .badge {{ display: inline-block; background: var(--orange); color: white; font-size: 0.7rem; font-weight: 700; padding: 2px 8px; border-radius: 99px; margin-top: 6px; }}
  main {{ padding: 16px; max-width: 700px; margin: 0 auto; }}
  h2 {{ font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 20px 0 10px; }}
  .card {{ background: var(--card); border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ background: var(--dark); color: white; padding: 10px 8px; text-align: center; font-size: 0.75rem; font-weight: 600; }}
  th.team {{ text-align: left; padding-left: 12px; }}
  td {{ padding: 10px 8px; text-align: center; border-bottom: 1px solid #f3f4f6; }}
  td.team {{ text-align: left; padding-left: 12px; font-weight: 500; }}
  td.pos {{ color: var(--muted); font-weight: 700; }}
  td.pts {{ font-weight: 700; color: var(--orange); }}
  tr.darlington td {{ background: var(--highlight); }}
  tr.darlington td.team {{ font-weight: 700; border-left: 3px solid var(--highlight-border); }}
  tr:last-child td {{ border-bottom: none; }}
  td.rnd {{ color: var(--muted); font-size: 0.75rem; }}
  td.winner {{ font-weight: 700; }}
  td.score {{ font-weight: 700; color: var(--dark); white-space: nowrap; }}
  .meta {{ font-size: 0.75rem; color: var(--muted); text-align: center; margin-top: 20px; padding-bottom: 32px; }}
  .no-data {{ padding: 32px; text-align: center; color: var(--muted); font-size: 0.9rem; }}
</style>
</head>
<body>
<header>
  <h1>U10 Mixed Orange — Hills Raiders 2026/27</h1>
  <p>Saturday Domestic Competition</p>
  <span class="badge">Round {total_rounds} complete</span>
</header>
<main>
  <h2>Ladder</h2>
  <div class="card">
  {'<table><thead><tr><th>Pos</th><th class="team">Team</th><th>P</th><th>W</th><th>L</th><th>D</th><th>PF</th><th>PA</th><th>%</th><th>Pts</th></tr></thead><tbody>' + rows + '</tbody></table>' if ladder else '<div class="no-data">No results yet — check back after Round 1!</div>'}
  </div>

  <h2>Recent Results</h2>
  <div class="card">
  {'<table><thead><tr><th>Rnd</th><th class="team">Home</th><th>Score</th><th class="team">Away</th></tr></thead><tbody>' + result_rows + '</tbody></table>' if all_games else '<div class="no-data">No results yet.</div>'}
  </div>

  {stats_section}

  <p class="meta">Updated {last_updated} AWST · Auto-updated every Saturday at 3:30pm<br>Data sourced from <a href="https://www.playhq.com/basketball-wa" style="color:var(--orange)">PlayHQ Basketball WA</a></p>
</main>
</body>
</html>"""


async def main():
    print("🏀 PlayHQ Ladder Scraper — Darlington Dunkers U10 Mixed Orange")
    print("=" * 60)

    all_games = []
    empty_rounds = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        for round_num in range(1, 30):
            print(f"\nRound {round_num}...")
            games = await scrape_round(page, round_num)

            if games is None:
                # Hard stop — page not found or explicit no-fixture signal
                print(f"  Stopping at Round {round_num}.")
                break

            if len(games) == 0:
                empty_rounds += 1
                print(f"  Empty round ({empty_rounds} in a row)")
                # Stop after 3 consecutive empty rounds
                if empty_rounds >= 3:
                    print("  3 consecutive empty rounds — done.")
                    break
            else:
                empty_rounds = 0
                all_games.extend(games)
                print(f"  ✓ {len(games)} game(s) added")

        # Scrape player stats (uses a fresh page to intercept API calls)
        print("\n📈 Scraping player stats...")
        players = await scrape_stats(context)

        await browser.close()

    print(f"\n📊 Total games scraped: {len(all_games)}")

    with open(OUTPUT_DIR / "games.json", "w") as f:
        json.dump(all_games, f, indent=2)
    print("✓ Raw data saved to docs/games.json")

    with open(OUTPUT_DIR / "stats.json", "w") as f:
        json.dump(players, f, indent=2)
    print(f"✓ Player stats saved to docs/stats.json ({len(players)} players)")

    ladder = calculate_ladder(all_games)
    print(f"\n🏆 Ladder ({len(ladder)} teams):")
    for i, t in enumerate(ladder, 1):
        print(f"  {i}. {t['team']:40s} {t['Pts']}pts  {t['W']}W {t['L']}L  {t['PCT']}%")

    last_updated = datetime.now().strftime("%d %b %Y, %-I:%M%p").lower()
    html = generate_html(ladder, all_games, last_updated, players=players)
    with open(OUTPUT_DIR / "index.html", "w") as f:
        f.write(html)
    print(f"\n✓ Ladder page written to docs/index.html")
    print("Done! 🎉")


if __name__ == "__main__":
    asyncio.run(main())
