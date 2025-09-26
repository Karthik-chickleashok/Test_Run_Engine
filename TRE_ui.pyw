# TRE_ui.pyw — Stability Drop v2.2.0
# Launcher for Test Run Engine (TRE)

from TRE_ui_core import AppCore

# --- tolerant import for Online tab ---
try:
    from TRE_ui_online_lab import attach_online_tab   # preferred entrypoint
    _ONLINE_ATTACH = attach_online_tab
except Exception:
    try:
        from TRE_ui_online_lab import build_online_tab  # fallback entrypoint
        _ONLINE_ATTACH = build_online_tab
    except Exception as e:
        _ONLINE_ATTACH = None
        _ONLINE_ERR = str(e)

APP_NAME     = "Test Run Engine"
APP_VERSION  = "2.2.0"
AUTHOR_NAME  = "Karthik Chickleashok"
AUTHOR_EMAIL = "karthik.chickel@gmail.com"


def main():
    # AppCore creates root + notebook + Offline tab
    app = AppCore(APP_NAME, APP_VERSION, AUTHOR_NAME, AUTHOR_EMAIL)

    # Attach Online tab
    if _ONLINE_ATTACH is not None:
        _ONLINE_ATTACH(app, app.nb)   # works for both attach/build
    else:
        app.add_disabled_online_tab(error=_ONLINE_ERR if '_ONLINE_ERR' in globals() else "Online tab unavailable")

    # Attach Help/About tabs
    app.attach_help_tab()
    app.attach_about_tab()

    # Run mainloop
    app.run()


if __name__ == "__main__":
    main()
