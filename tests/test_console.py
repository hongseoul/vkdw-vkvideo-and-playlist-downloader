import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vkdw import __version__, console


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.window = patch("vkdw.console._console_window", return_value=1234)
        self.show = patch("vkdw.console._show_window")
        self.window.start()
        self.shown = self.show.start()

    def tearDown(self):
        self.show.stop()
        self.window.stop()

    def test_console_stays_without_the_request_from_run_bat(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop(console.HIDE_VARIABLE, None)
            self.assertFalse(console.hide_if_asked())
        self.shown.assert_not_called()

    def test_console_is_hidden_when_run_bat_asks(self):
        with patch.dict("os.environ", {console.HIDE_VARIABLE: "1"}):
            self.assertTrue(console.hide_if_asked())
        self.shown.assert_called_once_with(1234, console.SW_HIDE)

    def test_nothing_to_hide_without_a_console(self):
        with patch.dict("os.environ", {console.HIDE_VARIABLE: "1"}), patch("vkdw.console._console_window", return_value=0):
            self.assertFalse(console.hide_if_asked())
        self.shown.assert_not_called()

    def test_crash_brings_the_console_back_and_logs_the_error(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "errors.log"
            with patch("vkdw.console.log_path", return_value=log), patch("builtins.input", return_value="") as wait:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        raise RuntimeError("сломалось при запуске")
                    except RuntimeError:
                        console.report_crash(True)
            self.assertIn("сломалось при запуске", log.read_text(encoding="utf-8"))
        self.shown.assert_called_once_with(1234, console.SW_SHOW)
        wait.assert_called_once()

    def test_crash_in_a_visible_console_does_not_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "errors.log"
            with patch("vkdw.console.log_path", return_value=log), patch("builtins.input") as wait:
                with redirect_stderr(io.StringIO()) as err:
                    try:
                        raise RuntimeError("ошибка")
                    except RuntimeError:
                        console.report_crash(False)
            self.assertIn("ошибка", log.read_text(encoding="utf-8"))
        self.assertIn("RuntimeError", err.getvalue())
        self.shown.assert_not_called()
        wait.assert_not_called()

    def test_crash_without_any_console_shows_a_message_box(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "errors.log"
            with (
                patch("vkdw.console.log_path", return_value=log),
                patch("vkdw.console.sys.stderr", None),
                patch("vkdw.console._message_box") as box,
                patch("builtins.input") as wait,
            ):
                try:
                    raise RuntimeError("сломалось в exe")
                except RuntimeError:
                    console.report_crash(False)
            text = log.read_text(encoding="utf-8")
            self.assertIn("сломалось в exe", text)
            self.assertIn(f"VKDW {__version__}", text)  # a report can be matched to its build
        self.assertIn(str(log), box.call_args.args[0])
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
