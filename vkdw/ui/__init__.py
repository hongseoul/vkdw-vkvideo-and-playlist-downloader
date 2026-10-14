"""Window built with PySide6 and QFluentWidgets."""

import contextlib
import io

# QFluentWidgets prints an advert when it is imported; keep the console clean.
with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets  # noqa: F401
