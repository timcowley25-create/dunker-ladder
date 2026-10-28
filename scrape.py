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
DARLINGTON_TEAM = "Darlington Dunkers"
OUTPUT_DIR = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)


async def scrape_round(page, round_num):
    """Visit a round page and extract game results."""
    url = f"{BASE_URL}/R{round_num}"
    print(f"  Fetching Round {round_num}: {url}")

    try:
        response = await page.goto(url, timeout=30000)
        if response and response.status == 404:
            return None

        # Wait for JS to render game content
        await page.wait_for_load_state("networkidle", timeout=20000)

        # Give extra time for dynamic content
        await page.wait_for_timeout(2000)

    except Exception as e:
        print(f"  Could not load Round {round_num}: {e}")
        return None

    # Check if this round has any content
    content = await page.content()
    if "No games" in content or "no games" in content:
        print(f"  Round {round_num}: no games found, stopping.")
        return None

    games = []

    # Try to extract game cards — PlayHQ renders each game as a card/row
    # Strategy: find all game result elements
    try:
        # Wait for game elements to appear
        await page.wait_for_selector("[class*='GameSummary'], [class*='game-summary'], [class*='FixtureRow'], [class*='fixture'], article, [data-testid*='game']", timeout=10000)
    except Exception:
        print(f"  Round {round_num}: no game elements found.")
        # Try screenshot for debugging on first round
        if round_num == 1:
            await page.screenshot(path=str(OUTPUT_DIR / "debug_r1.png"))
            print("  Debug screenshot saved to docs/debug_r1.png")
        return None

    # Get all text content and try to parse game data
    # PlayHQ structure: team name | score | team name | score
    game_elements = await page.query_selector_all("[class*='GameSummary'], [class*='game-summary'], [class*='FixtureRow'], article[class*='game'], [class*='GameCard']")

    if not game_elements:
        # Fallback: get all elements that look like score cards
        game_elements = await page.query_selector_all("article, [class*='Card'], [class*='card']")

    print(f"  Found {len(game_elements)} potential game elements")

    for el in game_elements:
        try:
            text = await el.inner_text()
            game = parse_game_text(text, round_num)
            if game:
                games.append(game)
                print(f"    Game: {game['home_team']} {game['home_score']} - {game['away_score']} {game['away_team']}")
        except Exception as e:
            continue

    # If element-level parsing found nothing, try full-page text parsing
    if not games:
        print(f"  Falling back to full-page text parsing for Round {round_num}")
        full_text = await page.inner_text("body")
        games = parse_page_text_fallback(full_text, round_num)
        for g in games:
            print(f"    Game: {g['home_team']} {g['home_score']} - {g['away_score']} {g['away_team']}")

    return games if games else None


def parse_game_text(text, round_num):
    """
    Parse a game element's text to extract teams and scores.
    PlayHQ typically renders: TeamA \n Score \n TeamB \n Score
    or: TeamA Score - Score TeamB
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # Filter out noise
    lines = [l for l in lines if l not in ["vs", "VS", "v", "-", "FINAL", "Final", "Completed"]]

    # Pattern: lines are [TeamA, scoreA, TeamB, scoreB] or similar
    if len(lines) >= 4:
        # Try to find two team names and two scores
        scores = []
        teams = []
        for line in lines:
            if re.match(r"^\d+$", line):
                scores.append(int(line))
            elif len(line) > 2 and not re.match(r"^\d", line):
                teams.append(line)

        if len(teams) >= 2 and len(scores) >= 2:
            return {
                "round": round_num,
                "home_team": teams[0],
                "away_team": teams[1],
                "home_score": scores[0],
                "away_score": scores[1],
            }

    # Pattern: "TeamA 24 - 18 TeamB"
    match = re.search(r"(.+?)\s+(\d+)\s*[-–]\s*(\d+)\s+(.+)", text)
    if match:
        return {
            "round": round_num,
            "home_team": match.group(1).strip(),
            "home_score": int(match.group(2)),
            "away_score": int(match.group(3)),
            "away_team": match.group(4).strip(),
        }

    return None


def parse_page_text_fallback(full_text, round_num):
    """Fallback: scan full page text for score patterns."""
    games = []
    # Look for "X def Y" or "X d. Y" patterns, or score lines
    patterns = [
        r"([A-Za-z ]+?)\s+(\d{1,3})\s*[-–v]\s*(\d{1,3})\s+([A-Za-z ]+)",
        r"([A-Za-z ]+?)\s+def\.?\s+([A-Za-z ]+?)\s+(\d{1,3})\s*[-–]\s*(\d{1,3})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, full_text):
            g = match.groups()
            if len(g) == 4:
                try:
                    game = {
                        "round": round_num,
                        "home_team": g[0].strip(),
                        "home_score": int(g[1]),
                        "away_score": int(g[2]),
                        "away_team": g[3].strip(),
                    }
                    games.append(game)
                except Exception:
                    continue
    return games


def calculate_ladder(all_games):
    """Build ladder from all game results."""
    teams = {}

    for game in all_games:
        home = game["home_team"]
        away = game["away_team"]
        hs = game["home_score"]
        as_ = game["away_score"]

        # Skip games with no scores (not yet played)
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

    # Calculate percentage
    ladder = []
    for team, stats in teams.items():
        pct = (stats["PF"] / stats["PA"] * 100) if stats["PA"] > 0 else 0
        ladder.append({
            "team": team,
            **stats,
            "PCT": round(pct, 1),
        })

    # Sort: Pts desc, PCT desc
    ladder.sort(key=lambda x: (-x["Pts"], -x["PCT"]))
    return ladder


def generate_html(ladder, all_games, last_updated):
    """Generate the ladder HTML page."""
    darlington_highlight = DARLINGTON_TEAM.lower()

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

    # Recent results
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

  <p class="meta">Updated {last_updated} AWST · Auto-updated every Saturday at 3:30pm<br>Data sourced from <a href="https://www.playhq.com/basketball-wa" style="color:var(--orange)">PlayHQ Basketball WA</a></p>
</main>
</body>
</html>"""


async def main():
    print("🏀 PlayHQ Ladder Scraper — Darlington Dunkers U10 Mixed Orange")
    print("=" * 60)

    all_games = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # Loop through rounds until we get no data
        for round_num in range(1, 30):  # Max 30 rounds
            print(f"\nRound {round_num}...")
            games = await scrape_round(page, round_num)

            if games is None:
                print(f"  No more data after Round {round_num - 1}. Done.")
                break

            all_games.extend(games)
            print(f"  ✓ {len(games)} game(s) found")

        await browser.close()

    print(f"\n📊 Total games scraped: {len(all_games)}")

    # Save raw data
    with open(OUTPUT_DIR / "games.json", "w") as f:
        json.dump(all_games, f, indent=2)
    print("✓ Raw data saved to docs/games.json")

    # Build ladder
    ladder = calculate_ladder(all_games)
    print(f"\n🏆 Ladder ({len(ladder)} teams):")
    for i, t in enumerate(ladder, 1):
        print(f"  {i}. {t['team']:40s} {t['Pts']}pts  {t['W']}W {t['L']}L  {t['PCT']}%")

    # Generate HTML
    last_updated = datetime.now().strftime("%d %b %Y, %-I:%M%p").lower()
    html = generate_html(ladder, all_games, last_updated)
    with open(OUTPUT_DIR / "index.html", "w") as f:
        f.write(html)
    print(f"\n✓ Ladder page written to docs/index.html")
    print("Done! 🎉")


if __name__ == "__main__":
    asyncio.run(main())
