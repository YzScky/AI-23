"""NBA 历史数据：ESPN 记分牌 + Gamma 市场 + CLOB 价格历史 + ESPN summary 时钟线。
只读，不下单。供 backtest_nba.py 用；缓存到 nba_backtest_data/，重跑不再重复拉。

实测记录（2026-11-20，本机）：
- CLOB 价格历史端点是 /prices-history（带连字符），/prices/history 返回 404。
  GET https://clob.polymarket.com/prices-history?market=<token>&interval=<range>&fidelity=<min>
  -> {"history": [{"t": epoch秒, "p": 价格}]}。interval 是回望范围（'1d'/'1w'/'1m'），
  fidelity 是 K 线分钟数，下限随范围变（1d->1，1w->5，1m->10）。
  已打完 Warriors token ("1d",1) 858 点、中位间隔 60 秒；("1w",5) 1831 点、300 秒。
  选窗按开球时间算：开球 <24h 前用 ("1d",1)，<7 天用 ("1w",5)，更老用 ("1m",10)。
- ESPN 记分牌按美东日期查；ESPN 缩写与 Gamma slug 缩写有 6 处不同：
  GS->GSW，NY->NYK，NO->NOP，SA->SAS，UTAH->UTA，WSH->WAS（其余相同）。
  注意：quant_platform/nba_clock.py 的 TEAM_ALIASES 用的是 GSW/NYK/... 形式，
  与 ESPN 记分牌返回的 GS/NY/... 对不上，勇士/尼克斯/鹈鹕/马刺/爵士/奇才这 6 队
  的 live 时钟会查不到（时钟缺失，策略 fail-closed 不新开仓）——待修。
  本模块用下面的 ESPN_TO_CANON 做映射。
- Gamma slug：nba-<客队缩写小写>-<主队缩写小写>-<美东开球日期>，客队在前
  （顺序反了会 404）。事件 startTime 是真正开球时间；市场 startDate 是上架时间，
  不能当 T0。moneyline 是 question == 事件标题的那个市场，outcomes 顺序对应
  clobTokenIds 顺序。
- ESPN summary 的 plays[]：wallclock（ISO）、clock.displayValue（"MM:SS" 本节剩余）、
  period.number（1-4，5+ 加时）、homeScore/awayScore；wallclockAvailable 为 true。
  独立的 playbyplay 端点返回 {}，不要用。
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - 极老 Python 才走这里
    ZoneInfo = None

import requests

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
                    "nba/scoreboard")
ESPN_SUMMARY = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
                 "nba/summary")
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_PRICES_HISTORY = "https://clob.polymarket.com/prices-history"

ET = ZoneInfo("America/New_York") if ZoneInfo else None

# ESPN 记分牌缩写 -> 策略/Polymarket 用的规范缩写（只有这 6 个不一样）。
ESPN_TO_CANON = {"GS": "GSW", "NY": "NYK", "NO": "NOP",
                 "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}


def canon(abbr):
    return ESPN_TO_CANON.get(abbr, abbr)


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_iso(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _parse_clock(s):
    """"12:00" -> 720.0，"0.0" -> 0.0，"11:33" -> 693.0；解析失败返回 None。"""
    if s is None:
        return None
    s = str(s).strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except Exception:
        return None


def past_et_dates(days):
    """过去 days 个美东日期（到昨天为止），YYYYMMDD 升序。
    今天的比赛还没打完，取到昨天为止才能保证每个日期都有已打完的比赛。"""
    if ET is not None:
        today = datetime.now(ET).date()
    else:  # pragma: no cover - 没 zoneinfo 时按 UTC-5 粗算
        today = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
    end = today - timedelta(days=1)
    return [(end - timedelta(days=i)).strftime("%Y%m%d")
            for i in range(days - 1, -1, -1)]


def list_completed_games(date_str, fetcher=None):
    """ESPN 记分牌某美东日期的已打完比赛。
    返回 [{espn_event_id, et_date, away, home, away_espn, home_espn,
            away_score, home_score, winner, date_utc}]，away/home 是规范缩写。"""
    fn = fetcher or _get
    data = fn(ESPN_SCOREBOARD, params={"dates": date_str})
    out = []
    for ev in (data or {}).get("events") or []:
        st = (ev.get("status") or {}).get("type") or {}
        if st.get("state") != "post" and not st.get("completed"):
            continue
        comps = (ev.get("competitions") or [{}])[0]
        teams = {}
        for c in comps.get("competitors") or []:
            abbr = (c.get("team") or {}).get("abbreviation")
            if not abbr:
                continue
            try:
                score = int(c.get("score"))
            except (TypeError, ValueError):
                score = 0
            teams[c.get("homeAway")] = {"espn": abbr, "canon": canon(abbr),
                                        "score": score}
        home, away = teams.get("home"), teams.get("away")
        if not home or not away:
            continue
        winner = None
        if home["score"] != away["score"]:
            winner = home["canon"] if home["score"] > away["score"] else away["canon"]
        out.append({
            "espn_event_id": str(ev.get("id")),
            "et_date": date_str,
            "away": away["canon"], "home": home["canon"],
            "away_espn": away["espn"], "home_espn": home["espn"],
            "away_score": away["score"], "home_score": home["score"],
            "winner": winner,
            "date_utc": ev.get("date"),
        })
    return out


def fetch_gamma_game(away_canon, home_canon, et_date, fetcher=None):
    """Gamma 按 slug 找比赛事件，取 moneyline 市场。
    返回 (gamma_dict, None) 或 (None, 原因)。"""
    fn = fetcher or _get
    slug = (f"nba-{away_canon.lower()}-{home_canon.lower()}-"
            f"{et_date[:4]}-{et_date[4:6]}-{et_date[6:]}")
    evs = fn(GAMMA_EVENTS, params={"slug": slug})
    if not evs:
        return None, f"Gamma slug {slug} 无事件"
    ev = evs[0]
    title = ev.get("title")
    market = None
    for m in ev.get("markets") or []:
        if m.get("question") == title:
            market = m
            break
    if market is None:
        return None, f"Gamma {slug} 没有 question==标题 的 moneyline 市场"
    toks = json.loads(market.get("clobTokenIds") or "[]")
    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):  # Gamma 把 outcomes 也当 JSON 字符串返回
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    if len(toks) != 2 or len(outcomes) != 2:
        return None, "moneyline token/outcomes 不是 2 个"
    start_ts = _parse_iso(ev.get("startTime"))
    if start_ts is None:
        return None, "事件 startTime 缺失"
    return {
        "slug": slug,
        "title": title,
        "start_ts": start_ts,
        "market_id": market.get("id"),
        "outcomes": outcomes,
        "token_a": toks[0],   # outcomes[0]（客队）
        "token_b": toks[1],   # outcomes[1]（主队）
        "fee_rate": (market.get("feeSchedule") or {}).get("rate", 0.05),
        "raw_event_meta": {"id": ev.get("id"), "slug": ev.get("slug"),
                           "startTime": ev.get("startTime"),
                           "startDate": ev.get("startDate")},
        "raw_market": market,
    }, None


def _interval_for(start_ts, now=None):
    """按开球时间选 CLOB 价格历史窗口：整场都要覆盖到。"""
    age_h = ((now if now is not None else time.time()) - start_ts) / 3600.0
    if age_h < 24:
        return ("1d", 1)
    if age_h < 7 * 24:
        return ("1w", 5)
    return ("1m", 10)


def fetch_price_history(token, start_ts, fetcher=None):
    """CLOB 价格历史。返回 ([{t, p}] 按时间升序, "interval/fidelity")。"""
    fn = fetcher or _get
    interval, fid = _interval_for(start_ts)
    data = fn(CLOB_PRICES_HISTORY,
              params={"market": token, "interval": interval, "fidelity": fid})
    pts = []
    for x in (data or {}).get("history") or []:
        try:
            pts.append({"t": int(x["t"]), "p": float(x["p"])})
        except (TypeError, ValueError, KeyError):
            continue
    pts.sort(key=lambda p: p["t"])
    return pts, f"{interval}/{fid}"


def fetch_plays(espn_event_id, fetcher=None):
    """ESPN summary 的 plays，处理成时钟线。
    返回 [{t, period, clock_s, home_score, away_score, text}] 按时间升序；
    clock 缺失时沿用上一条（时钟线不断档）。"""
    fn = fetcher or _get
    data = fn(ESPN_SUMMARY, params={"event": espn_event_id})
    plays = []
    last_clock = None
    for pl in (data or {}).get("plays") or []:
        t = _parse_iso(pl.get("wallclock"))
        if t is None:
            continue
        try:
            per = int((pl.get("period") or {}).get("number"))
        except (TypeError, ValueError):
            per = None
        clk = _parse_clock((pl.get("clock") or {}).get("displayValue"))
        if clk is None:
            clk = last_clock
        else:
            last_clock = clk
        try:
            hs = int(pl.get("homeScore"))
        except (TypeError, ValueError):
            hs = None
        try:
            aws = int(pl.get("awayScore"))
        except (TypeError, ValueError):
            aws = None
        plays.append({"t": t, "period": per, "clock_s": clk,
                      "home_score": hs, "away_score": aws,
                      "text": pl.get("text") or ""})
    plays.sort(key=lambda p: p["t"])
    return plays


def cache_path(data_dir, et_date, away_canon, home_canon):
    return os.path.join(data_dir,
                        f"{et_date}_{away_canon.lower()}-{home_canon.lower()}.json")


def load_game(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_game(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)


def fetch_game(sb_game, data_dir, fetcher=None, refresh=False, throttle=0.4):
    """拉一场比赛的全部数据并缓存。返回 (record|None, 状态, path)。
    状态：cached / fetched / 跳过：原因。缺任一数据就跳过，不抛异常。"""
    et_date = sb_game["et_date"]
    away, home = sb_game["away"], sb_game["home"]
    path = cache_path(data_dir, et_date, away, home)
    if os.path.exists(path) and not refresh:
        try:
            return load_game(path), "cached", path
        except Exception:
            pass  # 损坏的缓存当没有，重拉
    fn = fetcher or _get

    gamma, err = fetch_gamma_game(away, home, et_date, fn)
    if gamma is None:
        return None, f"跳过：{err}", path
    time.sleep(throttle)
    try:
        prices_a, gran = fetch_price_history(gamma["token_a"], gamma["start_ts"], fn)
    except Exception as e:
        return None, f"跳过：A 队价格历史拉取失败 {e}", path
    time.sleep(throttle)
    try:
        prices_b, _gran2 = fetch_price_history(gamma["token_b"], gamma["start_ts"], fn)
    except Exception as e:
        return None, f"跳过：B 队价格历史拉取失败 {e}", path
    time.sleep(throttle)
    if not prices_a or not prices_b:
        return None, "跳过：某边价格历史为空", path
    try:
        plays = fetch_plays(sb_game["espn_event_id"], fn)
    except Exception as e:
        return None, f"跳过：ESPN summary 拉取失败 {e}", path
    if len(plays) < 50:
        return None, f"跳过：plays 只有 {len(plays)} 条", path

    start_ts = gamma["start_ts"]
    end_t = plays[-1]["t"]
    in_a = [p for p in prices_a if start_ts <= p["t"] <= end_t]
    in_b = [p for p in prices_b if start_ts <= p["t"] <= end_t]
    if not in_a or not in_b:
        return None, "跳过：比赛时段内没有价格点", path
    pre_a = [p for p in prices_a if start_ts - 60 <= p["t"] <= start_ts + 60]
    pre_b = [p for p in prices_b if start_ts - 60 <= p["t"] <= start_ts + 60]

    record = {
        "et_date": et_date,
        "espn_event_id": sb_game["espn_event_id"],
        "away": away, "home": home,
        "away_espn": sb_game["away_espn"], "home_espn": sb_game["home_espn"],
        "title": gamma["title"],
        "slug": gamma["slug"],
        "market_id": gamma["market_id"],
        "start_ts": start_ts,
        "winner": sb_game["winner"],
        "home_score": sb_game["home_score"],
        "away_score": sb_game["away_score"],
        "outcomes": gamma["outcomes"],
        "token_a": gamma["token_a"], "token_b": gamma["token_b"],
        "fee_rate": gamma["fee_rate"],
        "granularity": gran,
        "prices_a": prices_a, "prices_b": prices_b,
        "plays": plays,
        "coverage": {
            "points_in_game_a": len(in_a), "points_in_game_b": len(in_b),
            "pregame_window_covered": bool(pre_a and pre_b),
            "last_price_ts": max(prices_a[-1]["t"], prices_b[-1]["t"]),
            "game_end_ts": end_t,
            "plays": len(plays),
        },
        "fetched_at": int(time.time()),
        "raw": {"scoreboard_event": sb_game,
                "gamma_event_meta": gamma["raw_event_meta"],
                "gamma_market": gamma["raw_market"]},
    }
    save_game(path, record)
    return record, "fetched", path


def fetch_range(days=7, data_dir="nba_backtest_data", games_limit=0,
                refresh=False, throttle=0.4, fetcher=None):
    """拉过去 days 天已打完的比赛。返回 (records, skipped)。
    按日期从新到旧拉，games_limit>0 时只拉最近的这么多场（小样本验证用）。
    skipped: [{game, reason}]。"""
    records, skipped = [], []
    attempted = 0
    for date_str in reversed(past_et_dates(days)):
        try:
            games = list_completed_games(date_str, fetcher)
        except Exception as e:
            skipped.append({"game": date_str, "reason": f"记分牌拉取失败 {e}"})
            continue
        time.sleep(throttle)
        for sb in games:
            if games_limit and attempted >= games_limit:
                return records, skipped
            attempted += 1
            rec, status, _path = fetch_game(sb, data_dir, fetcher, refresh, throttle)
            if rec is None:
                skipped.append({"game": f"{date_str} {sb['away']}-{sb['home']}",
                                "reason": status})
            else:
                records.append(rec)
    return records, skipped
