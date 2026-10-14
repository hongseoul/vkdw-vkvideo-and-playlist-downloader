from vkdw import console, i18n
from vkdw.settings import load_settings

if __name__ == "__main__":
    console.log_crashes()
    # Hide the console before the window libraries load, so it only flickers.
    hidden = console.hide_if_asked()
    i18n.set_language(i18n.pick_language(load_settings()))
    try:
        from vkdw.app import main

        main()
    except Exception:
        console.report_crash(hidden)
        raise SystemExit(1)
