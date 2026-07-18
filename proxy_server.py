"""
שרת ל-strata-location-analyzer.html - עובד גם מקומית וגם בפריסה אמיתית (למשל Render).
לא צריך להתקין שום דבר - זה משתמש רק בספריית הפייתון הרגילה.

הפעלה מקומית:
    python3 proxy_server.py
    ואז פותחים בדפדפן: http://localhost:8000/strata-location-analyzer.html

בפריסה (Render וכדומה), הפורט מגיע ממשתנה הסביבה PORT אוטומטית - אין צורך לגעת בקוד.

למה זה נחוץ: הדפדפן חוסם קריאות ישירות מהאפליקציה לשירותים חיצוניים
(Nominatim, Overpass, GIS עירוני) בגלל מדיניות CORS. השרת הזה יושב באמצע -
הוא מקבל את הבקשה מהדפדפן (מאותו מקור, בלי חסימה), פונה בעצמו
לשירותים החיצוניים (כמו curl, בלי מגבלת CORS), ומחזיר את התשובה.
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import os
import sys

PORT = int(os.environ.get("PORT", 8000))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

TELAVIV_GIS_BASE = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/IView2/MapServer"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def log_message(self, fmt, *args):
        # פלט קצר וקריא בטרמינל במקום הפורמט הארוך של הספרייה
        print("  " + (fmt % args))

    def _send_json_bytes(self, status, data_bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data_bytes)

    def _send_error_json(self, status, message):
        self._send_json_bytes(status, json.dumps({"error": message}).encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_response(302)
            self.send_header("Location", "/strata-location-analyzer.html")
            self.end_headers()
            return
        if parsed.path == "/api/geocode":
            qs = urllib.parse.parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            limit = qs.get("limit", ["1"])[0]
            if not q.strip():
                self._send_error_json(400, "missing query")
                return
            target = (
                "https://nominatim.openstreetmap.org/search?format=json"
                "&limit=" + urllib.parse.quote(limit)
                + "&accept-language=he&q=" + urllib.parse.quote(q)
            )
            self._proxy_get(target)
            return
        if parsed.path.startswith("/api/telaviv-gis/"):
            layer_id = parsed.path.rsplit("/", 1)[-1]
            if not layer_id.isdigit():
                self._send_error_json(400, "invalid layer id")
                return
            qs = urllib.parse.parse_qs(parsed.query)
            lat = qs.get("lat", [""])[0]
            lon = qs.get("lon", [""])[0]
            distance = qs.get("distance", [""])[0]
            if not lat or not lon:
                self._send_error_json(400, "missing lat/lon")
                return
            params = {
                "geometry": lon + "," + lat,
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
            }
            if distance:
                params["distance"] = distance
                params["units"] = "esriSRUnit_Meter"
            target = TELAVIV_GIS_BASE + "/" + layer_id + "/query?" + urllib.parse.urlencode(params)
            self._proxy_get(target)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/overpass":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            self._proxy_overpass(body)
            return
        self._send_error_json(404, "not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _proxy_get(self, target_url):
        req = urllib.request.Request(
            target_url, headers={"User-Agent": "strata-local-proxy/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                self._send_json_bytes(200, resp.read())
        except Exception as exc:
            print("  [שגיאת פרוקסי - geocode]", exc)
            self._send_error_json(502, str(exc))

    def _proxy_overpass(self, body):
        last_exc = None
        for endpoint in OVERPASS_ENDPOINTS:
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "User-Agent": "strata-local-proxy/1.0",
                    "Content-Type": "text/plain; charset=utf-8",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=18) as resp:
                    self._send_json_bytes(200, resp.read())
                    return
            except Exception as exc:
                print("  [שגיאת פרוקסי - overpass:", endpoint, "]", exc)
                last_exc = exc
        self._send_error_json(502, str(last_exc) if last_exc else "overpass failed")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
            is_local = "PORT" not in os.environ
            print("השרת פועל על פורט %d" % PORT)
            if is_local:
                print("פתח בדפדפן: http://localhost:%d/strata-location-analyzer.html" % PORT)
                print("לעצירה: Control + C")
            httpd.serve_forever()
    except OSError as e:
        print("שגיאה בהפעלת השרת (ייתכן שהפורט כבר תפוס):", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
