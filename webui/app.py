"""Desktop window for PDF Narrator (pywebview over the system WebView)."""

import sys

from jobs import JobService
from library import Library, Settings, config_dir
from webui.api import Api
from webui.server import MediaServer


def run(dev=False):
    try:
        import webview
        from webview.dom import DOMEventHandler
    except ImportError as error:
        print("The desktop app needs pywebview and a system WebView "
              "(on Linux: WebKitGTK, e.g. 'sudo apt install gir1.2-webkit2-4.1 python3-gi').\n"
              f"Details: {error}", file=sys.stderr)
        return 1

    settings = Settings()
    library = Library(settings.library_root)
    jobs = JobService(library, settings)
    server = MediaServer(lambda: library.root).start()
    api = Api(library, settings, jobs, server)
    window = webview.create_window(
        "PDF Narrator", server.app_url, js_api=api, width=1280, height=860, min_size=(900, 620),
        background_color="#FAFAFA", text_select=True)
    api._window = window

    def on_drop(event):
        files = event.get("dataTransfer", {}).get("files", [])
        paths = [item.get("pywebviewFullPath") for item in files if item.get("pywebviewFullPath")]
        if paths:
            api.import_paths(paths)

    def on_loaded():
        window.dom.document.events.dragover += DOMEventHandler(lambda event: None, True, True)
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)

    window.events.loaded += on_loaded
    try:
        webview.start(debug=dev, private_mode=False, storage_path=str(config_dir() / "webview"))
    finally:
        jobs.stop()
        server.stop()
    return 0
