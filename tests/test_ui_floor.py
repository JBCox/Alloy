"""Static UI contract for the human floor override."""

import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


class FloorControlUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.source = f.read()

    def test_live_seat_card_exposes_floor_action(self):
        self.assertIn('class="floor-btn"', self.source)
        self.assertIn("#seatList.locked .floor-btn", self.source)
        self.assertIn("next eligible turn", self.source)

    def test_floor_action_uses_the_persisted_command_path(self):
        self.assertIn("pywebview.api.command(`/next ${label}`, activeId)",
                      self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
