"""NbaClock 缩写归一回归测试。

ESPN 记分牌用 GS/NY/NO/SA/UTAH/WSH，parse_teams / get_game 用规范形式
GSW/NYK/NOP/SAS/UTA/WAS。归一之前 get_game("GSW", "MEM", ...) 查不到
勇士的比赛（时钟一直缺失，策略 fail-closed 不新开仓）。
"""
import unittest

from quant_platform.nba_clock import NbaClock, parse_teams

FAKE_ESPN = {
    "events": [
        {"name": "Golden State Warriors at Memphis Grizzlies",
         "status": {"clock": 0.0, "period": 4,
                    "type": {"state": "post", "completed": True}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "score": "126",
              "team": {"displayName": "Memphis Grizzlies",
                       "abbreviation": "MEM"}},
             {"homeAway": "away", "score": "98",
              "team": {"displayName": "Golden State Warriors",
                       "abbreviation": "GS"}},
         ]}]},
        {"name": "Washington Wizards at Chicago Bulls",
         "status": {"clock": 0.0, "period": 0,
                    "type": {"state": "pre", "completed": False}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "score": "0",
              "team": {"displayName": "Chicago Bulls",
                       "abbreviation": "CHI"}},
             {"homeAway": "away", "score": "0",
              "team": {"displayName": "Washington Wizards",
                       "abbreviation": "WSH"}},
         ]}]},
    ]
}


class TestNbaClockAbbr(unittest.TestCase):
    def test_espn_abbr_normalized(self):
        clock = NbaClock(
            fetcher=lambda url, params=None, timeout=10: FAKE_ESPN,
            min_interval=0)
        self.assertEqual(parse_teams("Warriors vs. Grizzlies"),
                         ("GSW", "MEM"))
        snap = clock.get_game("GSW", "MEM", "20261119")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["state"], "final")
        self.assertEqual(snap["winner"], "MEM")
        self.assertEqual(snap["home_abbr"], "MEM")
        self.assertEqual(snap["away_abbr"], "GSW")
        snap2 = clock.get_game("WAS", "CHI", "20261119")
        self.assertIsNotNone(snap2)
        self.assertEqual(snap2["state"], "pre")
        self.assertEqual(snap2["away_abbr"], "WAS")


if __name__ == "__main__":
    unittest.main()
