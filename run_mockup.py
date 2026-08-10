"""Serve the visual mockup locally for quick preview."""

import http.server
import socketserver
import webbrowser
from pathlib import Path

PORT = 8080
FILE = Path(__file__).parent / "mockup.html"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FILE.parent), **kwargs)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}/mockup.html"
        print(f"Mockup available at: {url}")
        webbrowser.open(url)
        httpd.serve_forever()
