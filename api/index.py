```python
from http.server import BaseHTTPRequestHandler
import json
import subprocess

class handler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        response = {
            "status": "ok",
            "message": "Müşteri Bulma API çalışıyor"
        }

        self.wfile.write(json.dumps(response).encode())

    def do_POST(self):

        try:

            content_length = int(self.headers["Content-Length"])
            body = self.rfile.read(content_length)

            data = json.loads(body)

            query = data.get("query", "")
            location = data.get("location", "")

            cmd = [
                "python3",
                "musteri_ajan.py",
                "scrape",
                "--query",
                query,
                "--location",
                location,
                "--max",
                "50",
                "--output",
                "/tmp/output.json"
            ]

            subprocess.run(cmd, check=True)

            with open("/tmp/output.json", "r", encoding="utf-8") as f:
                result = json.load(f)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps(result).encode())

        except Exception as e:

            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps({
                "error": str(e)
            }).encode())
```
