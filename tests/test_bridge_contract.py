"""The UI's calls must fit the bridge's signatures.

Every layer was tested and every layer reported green, and the app was still
broken: ui/index.html called `command(text, chatId)` while app.py had
`command(self, text)` (TypeError on every slash command), and
`read_text(path, chatId)` landed a chat id in `max_bytes` — no error at all,
just a wrong byte cap. Nothing tested the SEAM, so nothing caught it.

This suite reads the real call sites out of index.html and binds them against
the real signatures with inspect. It spends no tokens and touches no CLI.

Run:  python tests/test_bridge_contract.py
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")

# `pywebview.api.<name>(` — the argument text is scanned by hand below,
# because JS arguments can nest parentheses and a regex cannot balance them.
CALL = re.compile(r"pywebview\.api\.([A-Za-z_]\w*)\s*\(")


def split_args(text):
    """Top-level comma split of a JS argument list, honouring nesting and
    strings. Returns None for a call whose parens never close."""
    depth, out, cur, i, quote = 0, [], "", 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                cur += text[i:i + 2]
                i += 2
                continue
            if ch == quote:
                quote = None
            cur += ch
        elif ch in "\"'`":
            quote = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            if depth == 0:                      # the call's own closing paren
                out.append(cur)
                return [a.strip() for a in out if a.strip()]
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    return None


def ui_call_sites():
    """[(method, argument_count, line_number)] for every bridge call."""
    with open(UI, encoding="utf-8") as f:
        src = f.read()
    starts = [0]
    for m in re.finditer(r"\n", src):
        starts.append(m.end())

    def line_of(pos):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    sites = []
    for m in CALL.finditer(src):
        args = split_args(src[m.end():])
        if args is None:
            continue
        sites.append((m.group(1), len(args), line_of(m.start())))
    return sites


class BridgeContractTests(unittest.TestCase):
    def setUp(self):
        self.sites = ui_call_sites()
        self.assertTrue(self.sites, "found no pywebview.api calls to check")

    def test_every_ui_call_binds_to_its_python_signature(self):
        api = app.Api
        problems = []
        for name, argc, line in self.sites:
            fn = getattr(api, name, None)
            if fn is None or not callable(fn):
                problems.append(f"index.html:{line} calls api.{name}(), "
                                f"which Api does not define")
                continue
            sig = inspect.signature(fn)
            try:                       # `self` + the JS positional arguments
                sig.bind(None, *([None] * argc))
            except TypeError as e:
                problems.append(f"index.html:{line} api.{name}() passes "
                                f"{argc} arg(s) — {sig} rejects it: {e}")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_chat_scoped_methods_take_chat_id_in_the_position_the_ui_uses(self):
        """Binding alone is not enough. `read_text(path, chatId)` bound fine
        against `read_text(path, max_bytes)` and quietly used a chat id as a
        size limit — so for these, the id must be NAMED chat_id."""
        expected = {                    # method -> 0-based index the UI passes
            "read_text": 1,
            "read_image": 2,
            "list_workspace_files": 0,
            "command": 1,
            "interject": 2,
            "prepare_message": 2,
            "apply_role": 3,
            "stop_seat": 0,
        }
        for name, index in expected.items():
            fn = getattr(app.Api, name, None)
            self.assertIsNotNone(fn, f"Api has no {name}")
            params = list(inspect.signature(fn).parameters)[1:]   # drop self
            self.assertGreater(len(params), index,
                               f"{name}{inspect.signature(fn)} has no "
                               f"parameter at position {index}")
            self.assertEqual(params[index], "chat_id",
                             f"{name}{inspect.signature(fn)} takes "
                             f"'{params[index]}' where the UI passes the "
                             f"chat id")

    def test_no_bridge_method_is_called_that_the_ui_cannot_reach(self):
        """A public Api method whose name starts with an underscore would be
        invisible to the bridge; pywebview only exposes public attributes."""
        for name, _argc, line in self.sites:
            self.assertFalse(name.startswith("_"),
                             f"index.html:{line} calls a private method "
                             f"api.{name}(), which the bridge never exposes")


if __name__ == "__main__":
    r = unittest.TextTestRunner(verbosity=0).run(
        unittest.TestLoader().loadTestsFromTestCase(BridgeContractTests))
    print("OK" if r.wasSuccessful() else "FAILED")
    sys.exit(0 if r.wasSuccessful() else 1)
