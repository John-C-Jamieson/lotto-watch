"""Run with:  python -m unittest -v"""
import datetime as dt
import os
import unittest
from unittest import mock

import lotto_watch as lw

TZ = lw.TZ

# Trimmed from the real WCLC home page (Sep 26, 2026) - sidebar jackpot widgets.
WCLC_HOME = """
<div class="goldball"><h4>GOLD BALL JACKPOT</h4><span>$</span><span>12</span><span>Million</span>
<p>or Guaranteed $1 Million</p><p><strong>29</strong> Balls Remaining</p><p>Exact Match Only</p>
<p>Saturday, September 26, 2026</p></div>
<div class="lottomax"><span>$</span><span>65</span><span>Million</span>
<p>8 x $1 Million Prize</p><p>65 x<br/> $100,000</p><p>Tuesday, September 29, 2026</p></div>
<div>JACKPOT $265,103* *Estimated As of: Sep 26, 2026 11:00:11 AM CT</div>
"""
WCLC_HOME_UNDER_50M = """
<div><span>$</span><span>25</span><span>Million</span><p>25 x $100,000</p>
<p>Friday, October 2, 2026</p></div>"""
WCLC_HOME_CAPPED = """
<div><span>$</span><span>90</span><span>Million</span><p>31 x $1 Million Prizes</p>
<p>90 x $100,000</p><p>Friday, October 9, 2026</p></div>"""

LOTTERYFM = """<div>NOT YET DRAWN</div><div>NEXT DRAW</div><div>estimated jackpot</div>
<div>C$65M</div><div>Tuesday · 10:30pm EDT</div>"""

PLAYNOW_SUPER = """<p>Don't miss the Lotto 6/49 SuperDraw only on October 24, 2026. The same Lotto 6/49
ticket will offer more chances to win with 20 additional $40,000 GUARANTEED WINNERS!</p>"""
PLAYNOW_PAST = """<p>Don't miss the Lotto 6/49 SuperDraw only on August 29, 2026.</p>"""


def fake_fetcher(pages):
    def f(url, timeout=30):
        for key, body in pages.items():
            if key in url:
                return (200, body) if body is not None else (404, "")
        return 404, ""
    return f


NOW = dt.datetime(2026, 9, 26, 9, 17, tzinfo=TZ)  # Saturday


class ParseLottoMax(unittest.TestCase):
    def test_wclc_with_maxmillions(self):
        amt, d = lw.parse_wclc_lotto_max(lw.to_text(WCLC_HOME))
        self.assertEqual(amt, 65_000_000)  # not the $12M Gold Ball jackpot
        self.assertEqual(d, dt.date(2026, 9, 29))

    def test_wclc_without_maxmillions(self):
        amt, d = lw.parse_wclc_lotto_max(lw.to_text(WCLC_HOME_UNDER_50M))
        self.assertEqual((amt, d), (25_000_000, dt.date(2026, 10, 2)))

    def test_lotteryfm(self):
        amt, d = lw.parse_lotteryfm_lotto_max(lw.to_text(LOTTERYFM))
        self.assertEqual((amt, d), (65_000_000, None))

    def test_fallback_source(self):
        f = fake_fetcher({"wclc.com": "<html>redesigned</html>", "lottery.fm": LOTTERYFM})
        amt, d, src = lw.get_lotto_max(NOW, f)
        self.assertEqual((amt, d, src), (65_000_000, dt.date(2026, 9, 29), "lottery.fm"))

    def test_next_draw(self):
        tue_evening = dt.datetime(2026, 9, 29, 23, 0, tzinfo=TZ)
        self.assertEqual(lw.next_lotto_max_draw(tue_evening), dt.date(2026, 10, 2))
        tue_morning = dt.datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
        self.assertEqual(lw.next_lotto_max_draw(tue_morning), dt.date(2026, 9, 29))

    def test_html_comments_ignored(self):
        # The live PlayNow page has stale promo text inside <!-- -->.
        page = "<p>Live</p><!-- <p>Lotto 6/49 SuperDraw on December 19, 2026</p> -->"
        self.assertEqual(lw.to_text(page), "Live")
        found, _ = lw.find_super_draws(NOW.date(), fake_fetcher({"playnow.com": page}))
        self.assertEqual(found, {})


class LottoMaxAlerts(unittest.TestCase):
    def test_sequence(self):
        s = {}
        seq = [(65, "09-29"), (70, "10-02"), (80, "10-06"), (85, "10-09"),
               (90, "10-13"), (90, "10-13"), (90, "10-16"), (10, "10-20"), (80, "10-23")]
        titles = []
        for m, d in seq:
            a = lw.check_lotto_max(s, m * 1_000_000, dt.date.fromisoformat(f"2026-{d}"))
            titles.append([x["title"] for x in a])
        self.assertEqual(titles, [
            [], [], ["Lotto Max getting close to the max"], [],
            ["Lotto Max is at the MAX"], [],                 # same draw twice -> one alert
            ["Lotto Max still at the max"],                  # next capped draw
            [],                                              # won, resets
            ["Lotto Max getting close to the max"],          # re-armed
        ])

    def test_small_dip_between_sources_does_not_rewarn(self):
        s = {}
        draw = dt.date(2026, 10, 9)
        self.assertEqual(len(lw.check_lotto_max(s, 85_000_000, draw)), 1)  # WCLC
        self.assertEqual(lw.check_lotto_max(s, 84_500_000, draw), [])      # lottery.fm, rounded lower


class SuperDraws(unittest.TestCase):
    def test_future_announcement_and_reminder(self):
        s = {}
        f = fake_fetcher({"playnow.com": PLAYNOW_SUPER, "wclc.com": WCLC_HOME})
        found, ok = lw.find_super_draws(NOW.date(), f)
        self.assertEqual(ok, 1)  # WCLC loaded but doesn't mention a Super Draw
        self.assertEqual(found["2026-10-24"], (
            "Lotto 6/49", "Lotto 6/49 Super Draw on Saturday, October 24, 2026 (via PlayNow (BCLC))"))
        a = lw.check_super_draws(s, found, NOW.date())
        self.assertEqual([x["title"] for x in a], ["Super Draw announced"])
        self.assertEqual(lw.check_super_draws(s, found, NOW.date()), [])  # no repeat
        a = lw.check_super_draws(s, found, dt.date(2026, 10, 24))
        self.assertEqual([x["title"] for x in a], ["Super Draw is TONIGHT"])

    def test_reminder_names_the_right_game(self):
        s = {}
        lw.check_super_draws(s, {"2026-11-05": ("Daily Grand", "Daily Grand Super Draw")}, NOW.date())
        a = lw.check_super_draws(s, {}, dt.date(2026, 11, 5))  # page no longer lists it
        self.assertTrue(a[0]["body"].startswith("Daily Grand Super Draw tonight"))

    def test_past_super_draw_ignored(self):
        found, _ = lw.find_super_draws(NOW.date(), fake_fetcher({"playnow.com": PLAYNOW_PAST}))
        self.assertEqual(found, {})

    def test_olg_conditions_page(self):
        page = "<h1>LOTTO 6/49 Super Draw November 2026 Game Conditions</h1><p>On Saturday, November 21, 2026 ...</p>"
        f = fake_fetcher({"lotto-649-super-draw/november-2026": page})
        found, _ = lw.find_super_draws(NOW.date(), f)
        self.assertIn("2026-11-21", found)

    def test_olg_month_then_exact_date_no_duplicate(self):
        s = {}
        month = {"2026-11": ("Lotto 6/49", "Super Draw in November 2026")}
        a = lw.check_super_draws(s, month, NOW.date())
        self.assertEqual(len(a), 1)
        a = lw.check_super_draws(s, {"2026-11-21": ("Lotto 6/49", "Super Draw Nov 21")}, NOW.date())
        self.assertEqual(a, [])
        a = lw.check_super_draws(s, month, NOW.date())
        self.assertEqual(a, [])


class Health(unittest.TestCase):
    def test_alert_after_three_failures_then_recovery(self):
        s = {}
        out = [lw.track_health(s, "x", False, "thing") for _ in range(4)]
        self.assertEqual([len(o) for o in out], [0, 0, 1, 0])
        self.assertEqual(lw.track_health(s, "x", True, "thing")[0]["title"], "Lotto watch recovered")

    def test_full_run_offline(self):
        s = {}
        f = fake_fetcher({"wclc.com": WCLC_HOME_CAPPED, "playnow.com": PLAYNOW_SUPER})
        titles = [a["title"] for a in lw.run(s, NOW, f)]
        self.assertEqual(titles, ["Lotto Max is at the MAX", "Super Draw announced"])

    def test_super_draw_page_gone_is_noticed(self):
        # WCLC keeps loading, but PlayNow's Super Draw page 404s.
        s = {}
        f = fake_fetcher({"wclc.com": WCLC_HOME})
        titles = [[a["title"] for a in lw.run(s, NOW, f)] for _ in range(3)]
        self.assertEqual(titles, [[], [], ["Lotto watch needs attention"]])

    def test_bad_setting_falls_back_and_is_reported(self):
        with mock.patch.dict(os.environ, {"HEARTBEAT_WEEKDAY": "Mon"}), \
                mock.patch.object(lw, "CONFIG_ERRORS", []):
            self.assertEqual(lw.env_int("HEARTBEAT_WEEKDAY", -1), -1)
            self.assertEqual(len(lw.CONFIG_ERRORS), 1)


class Outbox(unittest.TestCase):
    A = {"title": "A", "body": "a", "priority": "default", "tags": ""}
    B = {"title": "B", "body": "b", "priority": "default", "tags": ""}

    def test_failed_alert_retried_without_repeating_sent_ones(self):
        s, sent = {}, []

        def flaky(alert, dry_run):
            if alert["title"] == "B":
                raise RuntimeError("down")
            sent.append(alert["title"])

        self.assertEqual(lw.send_all(s, [self.A, self.B], NOW.date(), False, flaky), 1)
        self.assertEqual((sent, s["outbox"]), (["A"], [self.B]))
        # Next run: B goes out, A isn't sent again.
        works = lambda alert, dry_run: sent.append(alert["title"])
        self.assertEqual(lw.send_all(s, [], NOW.date(), False, works), 0)
        self.assertEqual(sent, ["A", "B"])
        self.assertNotIn("outbox", s)

    def test_stale_retry_dropped(self):
        s = {"outbox": [dict(self.A, expires="2026-09-25")]}  # draw was yesterday
        sent = []
        lw.send_all(s, [], NOW.date(), False, lambda alert, dry_run: sent.append(alert["title"]))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
