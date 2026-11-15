import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "Boothbridge"
if not (ROOT / "djapp").is_dir():
    ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "djapp" / "ui"
CALLS = ("connect", "addAction")


def trees():
    for path in sorted(UI.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


class SignalSlotTests(unittest.TestCase):
    def test_a_lambda_given_to_a_signal_or_a_menu_takes_any_arguments(self):
        # the program is compiled by Cython: PySide cannot count the parameters of a compiled lambda, so one that
        # names fewer or more than the signal sends is never called (a zero argument lambda on textChanged, a
        # one argument lambda on clicked). A lambda with *args is called with whatever the signal sends
        bad = []
        for path, tree in trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CALLS:
                    for arg in node.args:
                        if isinstance(arg, ast.Lambda) and arg.args.vararg is None:
                            bad.append(f"{path.name}:{arg.lineno}")
        self.assertEqual(bad, [], "a lambda connected to a signal must be written lambda *_: ...")

    def test_the_lambdas_of_the_menu_tables_take_any_arguments(self):
        text = (UI / "main_window.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for value in node.values:
                    for sub in ast.walk(value):
                        if isinstance(sub, ast.Lambda) and sub.args.vararg is None:
                            bad.append(sub.lineno)
        self.assertEqual(bad, [])

    def test_the_support_link_accepts_the_checked_flag_of_a_menu_action(self):
        tree = ast.parse((UI / "support.py").read_text(encoding="utf-8"))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "open_link")
        self.assertIsNotNone(func.args.vararg)


if __name__ == "__main__":
    unittest.main()
