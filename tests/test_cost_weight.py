"""The two copies of the token-weight rule, and the one table that keeps them equal.

`relay.cost_weight` and ui/index.html's `costWeight` are a DELIBERATE second
copy: the mode picker paints before any conversation exists and repaints on
every roster change, so a bridge round trip per seat toggle would lag a
control that has to feel instant -- and ui/index.html already mirrors
`peel_directives` for the same reason.

Two copies of one rule drift. The answer this repo settled on is not a
docstring promising parity (browser_mcp._confine's docstring claimed exactly
that while four rules differed) but ONE table of cases fed to BOTH halves and
compared field by field.

Token-free. Skips the parity half cleanly where node is absent.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")
NODE = shutil.which("node")

# The recipes exactly as PRESET_RECIPES declares them. Written out here rather
# than scraped out of the UI, so that a change to that table fails this suite
# loudly instead of being mirrored into the expectation meant to check it.
RECIPES = {
    "open_discussion": dict(mode="round_robin", concurrency="sequential",
                            floor="cyclic", workflow="conversation",
                            routing="broadcast", completion="participants",
                            unit="laps"),
    "panel_review": dict(mode="panel", concurrency="barrier", floor="all",
                         workflow="panel", routing="broadcast",
                         completion="synthesizer", unit="phases"),
    "build_execute": dict(mode="supervisor", concurrency="barrier",
                          floor="manager", workflow="supervisor",
                          routing="isolated", completion="supervisor",
                          unit="waves"),
    "live_room": dict(mode="free", concurrency="reactive", floor="fair",
                      workflow="conversation", routing="addressed",
                      completion="participants", unit="turns"),
    "keep_improving": dict(mode="supervisor", concurrency="barrier",
                           floor="manager", workflow="supervisor",
                           routing="isolated", completion="supervisor",
                           unit="waves", continuous=True),
    "arena": dict(mode="battle", concurrency="barrier", floor="all",
                  workflow="battle", routing="isolated",
                  completion="participants", unit="phases"),
}
SEAT_COUNTS = (1, 2, 3, 5)

JS_HARNESS = """
const out = {};
const RECIPES = %s;
for (const name of Object.keys(RECIPES))
  for (const n of %s) {
    const c = costWeight(RECIPES[name], n);
    out[name + "/" + n] = [c.multiplier, c.band, c.relay_reads, c.why];
  }
process.stdout.write(JSON.stringify(out));
"""


def _ui():
    with io.open(UI, encoding="utf-8") as fh:
        return fh.read()


def _js_block(src=None):
    """The mirror, lifted out of the single inline <script>.

    Starts at the HEADER COMMENT, not at the first statement: that comment is
    the mirror's docstring and is what names its Python twin, so an extractor
    that skipped it would report the pairing missing while it was right
    there."""
    src = _ui() if src is None else src
    start = src.index("// ------------------------------------------------------- cost weight ----")
    end = src.index("const BAND_LABEL = Object.freeze(")
    return src[start:end]


class CostWeight(unittest.TestCase):
    def test_solo_is_light_in_every_mode(self):
        """One seat relays nothing, whatever the routing axis claims."""
        for name, recipe in RECIPES.items():
            got = relay.cost_weight(recipe, 1)
            self.assertEqual(got["band"], "light", name)
            self.assertEqual(got["relay_reads"], 0.0, name)
            self.assertIn("One seat", got["why"], name)

    def test_broadcast_is_quadratic_in_seats(self):
        """The whole reason the badge exists: commit_reply appends the FULL
        reply to every peer's backlog, so the multiplier tracks seat count."""
        r = RECIPES["open_discussion"]
        self.assertEqual(relay.cost_weight(r, 2)["multiplier"], 2.0)
        self.assertEqual(relay.cost_weight(r, 3)["multiplier"], 3.0)
        self.assertEqual(relay.cost_weight(r, 5)["multiplier"], 5.0)

    def test_isolated_never_grows(self):
        """Workstream radio silence: a worker never pays to read a worker."""
        for n in SEAT_COUNTS:
            got = relay.cost_weight(RECIPES["build_execute"], n)
            self.assertEqual(got["multiplier"], 1.0, n)
            self.assertEqual(got["band"], "light", n)

    def test_panel_is_not_charged_as_broadcast(self):
        """Its axis says broadcast; draft and critique commit fan_out=False,
        so only one phase of three actually relays. Reading the axis literally
        would price the phase-isolated mode as open debate."""
        n = 4
        panel = relay.cost_weight(RECIPES["panel_review"], n)["multiplier"]
        talk = relay.cost_weight(RECIPES["open_discussion"], n)["multiplier"]
        self.assertLess(panel, talk)

    def test_digest_routing_does_not_scale_with_seats(self):
        """A digest is summarized and capped, so five seats cost what two do."""
        two = relay.cost_weight(RECIPES["live_room"], 2)["multiplier"]
        five = relay.cost_weight(RECIPES["live_room"], 5)["multiplier"]
        self.assertEqual(two, five)

    def test_never_claims_to_be_measured(self):
        """estimate_calls' contract, one function over: stats.py holds what a
        run actually cost; this is what the recipe implies beforehand."""
        for name, recipe in RECIPES.items():
            self.assertIs(relay.cost_weight(recipe, 3)["estimated"], True, name)

    def test_reason_names_the_mechanism_not_the_verdict(self):
        """A band is a judgement; `why` is the fact it was made from. 'Heavy'
        tells Josh nothing to act on -- 'read by 2 other seats' does."""
        why = relay.cost_weight(RECIPES["open_discussion"], 3)["why"]
        self.assertIn("2 other seats", why)
        for bad in ("heavy", "expensive", "costly"):
            self.assertNotIn(bad, why.lower())

    def test_singular_peer_reads_correctly(self):
        self.assertIn("1 other seat.",
                      relay.cost_weight(RECIPES["open_discussion"], 2)["why"])

    def test_unknown_routing_falls_back_to_the_expensive_reading(self):
        """An axis nobody taught it about must not be priced as free. An
        under-claim here is a control quietly saying 'this one is cheap'."""
        got = relay.cost_weight(dict(RECIPES["open_discussion"],
                                     routing="something-new"), 3)
        self.assertEqual(got["multiplier"], 3.0)

    def test_a_recipe_of_none_still_answers(self):
        self.assertIs(relay.cost_weight(None, 3)["estimated"], True)

    def test_zero_and_negative_seats_are_treated_as_one(self):
        for seats in (0, -1, None):
            self.assertEqual(
                relay.cost_weight(RECIPES["open_discussion"], seats)["band"],
                "light", seats)


@unittest.skipUnless(NODE, "node not installed")
class Parity(unittest.TestCase):
    """ONE table, BOTH halves, identical answers."""

    def test_python_and_js_agree_on_every_case(self):
        block = _js_block()
        harness = block + JS_HARNESS % (json.dumps(RECIPES),
                                        json.dumps(list(SEAT_COUNTS)))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "parity.js")
            io.open(path, "w", encoding="utf-8").write(harness)
            res = subprocess.run([NODE, path], capture_output=True, text=True,
                                 timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(res.returncode, 0, res.stderr)
        js = json.loads(res.stdout)
        # A parity run that quietly compared nothing would pass forever.
        self.assertEqual(len(js), len(RECIPES) * len(SEAT_COUNTS))
        for name, recipe in RECIPES.items():
            for n in SEAT_COUNTS:
                py = relay.cost_weight(recipe, n)
                self.assertEqual(
                    js["%s/%d" % (name, n)],
                    [py["multiplier"], py["band"], py["relay_reads"],
                     py["why"]],
                    "%s at %d seats: the two copies disagree" % (name, n))

    def test_the_mirror_is_actually_present(self):
        block = _js_block()
        for token in ("function costWeight", "function costReason",
                      "RELAY_READS", "BAND_HEAVY"):
            self.assertIn(token, block)

    def test_both_docstrings_name_their_twin(self):
        """The confinement-parity move: whoever finds one copy is told the
        other exists, so a fix lands on both."""
        self.assertIn("costWeight", relay.cost_weight.__doc__)
        self.assertIn("relay.cost_weight",
                      _js_block())


class Wiring(unittest.TestCase):
    """The badge has to reach the row. W0.1's lesson is that the engine can be
    perfect while the bridge drops the key."""

    def setUp(self):
        self.src = _ui()

    def test_the_picker_renders_the_badge(self):
        self.assertIn("costForPreset(m.v, n)", self.src)
        self.assertIn('b.querySelector(".cost-badge")', self.src)

    def test_every_band_has_a_style(self):
        for band in ("light", "moderate", "heavy"):
            self.assertIn(".cost-%s {" % band, self.src)

    def test_an_unavailable_mode_quotes_no_price(self):
        """A row that cannot be picked must not advertise its cost."""
        self.assertIn("if (!why) {", self.src)
        self.assertIn("const c = costForPreset", self.src)

    def test_the_mechanism_line_is_withheld_when_it_cannot_vary(self):
        """Solo makes every row's sentence identical, and six copies of one
        true sentence is noise rather than information."""
        self.assertIn("if (n > 1) whyLine.textContent = c.why;", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
