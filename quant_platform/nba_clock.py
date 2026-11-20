"""NBA 比赛时钟：ESPN 公开记分牌 API（无需 key，只读）。

验证记录（2026-11-20，本机实测）：
- GET https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261120
  -> 200，9 场比赛。events[].name 如 "Charlotte Hornets at Atlanta Hawks"，
  shortName "CHA @ ATL"，date "2026-11-21T00:00Z"（UTC，7:00 PM EST 开球）。
  status = {clock: 0.0, displayClock: "0.0", period: 0,
            type: {id: "1", name: "STATUS_SCHEDULED", state: "pre",
                   completed: false, detail: "Fri, November 20th at 7:00 PM EST"}}。
  competitors[].team = {displayName: "Atlanta Hawks", abbreviation: "ATL"}，
  homeAway: "home"/"away"，score: "0"。
- 进行中比赛的字段当时没有 live 比赛可直接观测，按 ESPN 的常规结构处理（假设，
  写进注释）：status.clock = 本节剩余秒数（float），displayClock "MM:SS"，
  period 1-4（5+ 为加时），type.state == "in"；终场 type.state == "post"，
  completed == true，period 为最后一节。

Gamma 比赛盘形态（同日实测，见 feed.py 注释）：
- 事件标题 "Suns vs. Rockets"（vs. 带点），slug nba-phx-hou-2026-11-25；
  每事件 1 个市场，question == 标题；outcomes ["Suns", "Rockets"]，
  outcomePrices 如 ["0.265", "0.735"]，和约 1.00；clobTokenIds 2 个；
  fee rate 0.05（体育）。市场 startDate 是上架时间（11-11 上架 11-25 的比赛），
  真正开球时间在事件的 startTime（2026-11-26T01:30:00Z）/ eventDate。
  所以 T0 只能用 startTime，绝不能用 startDate。

缩写注意（2026-11-20 回测时实测）：ESPN 记分牌的 abbreviation 是
GS/NY/NO/SA/UTAH/WSH，Gamma/Polymarket 用 GSW/NYK/NOP/SAS/UTA/WAS；
_load_date 里用 ESPN_TO_CANON 归一到规范形式再建索引，否则勇士/尼克斯/
鹈鹕/马刺/爵士/奇才这 6 队的比赛 get_game 永远查不到（时钟缺失，
策略 fail-closed 不新开仓）。
"""
import time

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - 极老 Python 才走这里
    ZoneInfo = None

import requests

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
                    "nba/scoreboard")
ET = ZoneInfo("America/New_York") if ZoneInfo else None

# Polymarket 比赛盘 outcomes 用的简称 -> ESPN abbreviation。
# 键是 Gamma outcomes 里的写法（实测：Suns / Trail Blazers / 76ers 这类简称）。
TEAM_ALIASES = {
    "Hawks": "ATL", "Celtics": "BOS", "Nets": "BKN", "Hornets": "CHA",
    "Bulls": "CHI", "Cavaliers": "CLE", "Mavericks": "DAL", "Nuggets": "DEN",
    "Pistons": "DET", "Warriors": "GSW", "Rockets": "HOU", "Pacers": "IND",
    "Clippers": "LAC", "Lakers": "LAL", "Grizzlies": "MEM", "Heat": "MIA",
    "Bucks": "MIL", "Timberwolves": "MIN", "Pelicans": "NOP", "Knicks": "NYK",
    "Thunder": "OKC", "Magic": "ORL", "76ers": "PHI", "Suns": "PHX",
    "Trail Blazers": "POR", "Kings": "SAC", "Spurs": "SAS", "Raptors": "TOR",
    "Jazz": "UTA", "Wizards": "WAS",
    # 容错：全名 / 缩写本身也能进来
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU",
    "Indiana Pacers": "IND", "LA Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}
for _abbr in ("ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET",
              "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN",
              "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS",
              "TOR", "UTA", "WAS"):
    TEAM_ALIASES.setdefault(_abbr, _abbr)

# ESPN 记分牌 abbreviation -> 规范缩写（实测 2026-11-19/20：
# ESPN 用 GS、NY、NO、SA、UTAH、WSH；Gamma/Polymarket 用
# GSW、NYK、NOP、SAS、UTA、WAS）。_load_date 里归一到规范形式，
# 否则 get_game("GSW", ...) 查不到勇士的比赛，时钟一直缺失。
ESPN_TO_CANON = {"GS": "GSW", "NY": "NYK", "NO": "NOP",
                 "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}


def parse_teams(question):
    """把比赛盘 question 解析成两队 abbreviation。
    实测形态："Suns vs. Rockets"（vs. 带点）；也容 " vs "（不带点）。
    解析失败返回 None。"""
    if not question:
        return None
    sep = " vs. " if " vs. " in question else (" vs " if " vs " in question else None)
    if sep is None:
        return None
    a, b = question.split(sep, 1)
    aa = TEAM_ALIASES.get(a.strip())
    bb = TEAM_ALIASES.get(b.strip())
    if not aa or not bb:
        return None
    return (aa, bb)


def et_date_str(ts):
    """epoch 秒（UTC）-> 开球当地（美东）日期 YYYYMMDD。
    ESPN 记分牌按美东日期查（19:00 EST 开球 = 次日 00:00 UTC，别用 UTC 日期）。"""
    import datetime
    if ET is not None:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).astimezone(ET)
    else:  # pragma: no cover - 没 zoneinfo 时按 EST(UTC-5) 粗算，11 月常规赛适用
        dt = datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc) - datetime.timedelta(hours=5)
    return dt.strftime("%Y%m%d")


def _default_fetcher(url, params=None, timeout=10):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


class NbaClock:
    """ESPN 记分牌只读客户端。任何失败返回 None（调用方按“时钟缺失”处理）。
    fetcher 可注入：fetcher(url, params=None, timeout=10) -> 已解析的 JSON dict，
    供单测用假数据。两次拉取同一日期至少间隔 min_interval 秒（默认 15 秒）。"""

    def __init__(self, fetcher=None, min_interval=15):
        self.fetcher = fetcher or _default_fetcher
        self.min_interval = float(min_interval)
        self._cache = {}       # date_str -> (fetched_ts, {frozenset({abbr, abbr}): game})
        self._last_fetch_ts = 0.0

    def _load_date(self, date_str):
        now = time.time()
        hit = self._cache.get(date_str)
        if hit and now - hit[0] < self.min_interval:
            return hit[1]
        if now - self._last_fetch_ts < self.min_interval and hit:
            return hit[1]
        try:
            data = self.fetcher(ESPN_SCOREBOARD, params={"dates": date_str}, timeout=10)
        except Exception:
            # 拉取失败也记时间：最多每 min_interval 秒重试一次，别每 tick 打 ESPN
            self._last_fetch_ts = now
            return None
        self._last_fetch_ts = now
        games = {}
        for ev in (data or {}).get("events") or []:
            try:
                comps = (ev.get("competitions") or [{}])[0]
                teams = {}
                for c in comps.get("competitors") or []:
                    tm = (c.get("team") or {})
                    abbr = tm.get("abbreviation")
                    if not abbr:
                        continue
                    # ESPN 缩写归一到规范形式（见 ESPN_TO_CANON），
                    # 否则缓存键和 get_game/parse_teams 的规范缩写对不上。
                    abbr = ESPN_TO_CANON.get(abbr, abbr)
                    try:
                        score = int(c.get("score"))
                    except (TypeError, ValueError):
                        score = 0
                    teams[c.get("homeAway") or ""] = {"abbr": abbr, "score": score}
                home = teams.get("home")
                away = teams.get("away")
                if not home or not away:
                    continue
                st = ev.get("status") or {}
                typ = st.get("type") or {}
                state = typ.get("state")  # pre / in / post
                try:
                    period = int(st.get("period") or 0)
                except (TypeError, ValueError):
                    period = 0
                try:
                    clock_s = float(st.get("clock"))
                except (TypeError, ValueError):
                    clock_s = None
                if state == "pre":
                    clock_s = None
                winner = None
                if state == "post" or typ.get("completed"):
                    state = "final"
                    if home["score"] != away["score"]:
                        winner = home["abbr"] if home["score"] > away["score"] else away["abbr"]
                games[frozenset((home["abbr"], away["abbr"]))] = {
                    "state": state, "period": period, "clock_s": clock_s,
                    "home_abbr": home["abbr"], "away_abbr": away["abbr"],
                    "home_score": home["score"], "away_score": away["score"],
                    "winner": winner, "date": date_str,
                }
            except Exception:
                continue
        self._cache[date_str] = (now, games)
        return games

    def get_game(self, abbr_a, abbr_b, date_str):
        """查一场比赛的时钟快照。失败 / 查不到返回 None。
        返回 {"state": "pre"|"in"|"final", "period": int,
              "clock_s": float|None（本节剩余秒）, "home_abbr", "away_abbr",
              "home_score", "away_score", "winner": abbr|None, "date": str}。"""
        if not abbr_a or not abbr_b or not date_str:
            return None
        games = self._load_date(date_str)
        if games is None:
            return None
        return games.get(frozenset((abbr_a, abbr_b)))
