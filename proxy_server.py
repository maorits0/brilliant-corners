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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

TELAVIV_GIS_BASE = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/IView2/MapServer"

# business type value -> (osm key, osm value), used only by the passive recheck job
# to test whether a matching business now exists near a previously-logged search
BUSINESS_TAG_MAP = {
    "cafe": ("amenity", "cafe"), "restaurant": ("amenity", "restaurant"),
    "fast_food": ("amenity", "fast_food"), "bakery": ("shop", "bakery"),
    "ice_cream": ("amenity", "ice_cream"), "bar": ("amenity", "bar"),
    "supermarket": ("shop", "supermarket"), "convenience": ("shop", "convenience"),
    "pharmacy": ("amenity", "pharmacy"), "florist": ("shop", "florist"),
    "clothing": ("shop", "clothes"), "shoes": ("shop", "shoes"),
    "jewelry": ("shop", "jewelry"), "furniture": ("shop", "furniture"),
    "hairdresser": ("shop", "hairdresser"), "beauty": ("shop", "beauty"),
    "tattoo": ("shop", "tattoo"), "gym": ("leisure", "fitness_centre"),
    "dentist": ("amenity", "dentist"), "optician": ("shop", "optician"),
    "veterinary": ("amenity", "veterinary"), "escape_game": ("leisure", "escape_game"),
    "games": ("shop", "games"), "laundry": ("shop", "laundry"),
    "travel_agency": ("shop", "travel_agency"), "copyshop": ("shop", "copyshop"),
    "toys": ("shop", "toys"), "driving_school": ("amenity", "driving_school"),
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def log_message(self, fmt, *args):
        # פלט קצר וקריא בטרמינל במקום הפורמט הארוך של הספרייה
        print("  " + (fmt % args))

    def _send_json_bytes(self, status, data_bytes):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data_bytes)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            # הדפדפן כבר סגר את החיבור (למשל timeout בצד הלקוח) - מתעדים ולא מפילים את השרת
            print("  [החיבור נסגר לפני שהתשובה נשלחה]", exc)

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
            want_geometry = qs.get("geometry", [""])[0] == "1"
            if not lat or not lon:
                self._send_error_json(400, "missing lat/lon")
                return
            params = {
                "geometry": lon + "," + lat,
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true" if want_geometry else "false",
                "f": "json",
            }
            if want_geometry:
                params["outSR"] = "4326"
            if distance:
                params["distance"] = distance
                params["units"] = "esriSRUnit_Meter"
            target = TELAVIV_GIS_BASE + "/" + layer_id + "/query?" + urllib.parse.urlencode(params)
            self._proxy_get(target)
            return
        if parsed.path == "/api/recheck":
            self._run_recheck_batch()
            return
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/overpass":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            self._proxy_overpass(body)
            return
        if parsed.path == "/api/log":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length else b"{}"
            self._log_search(body_bytes)
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

    def _supabase_request(self, method, path_and_query, body=None):
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise Exception("Supabase not configured (missing SUPABASE_URL/SUPABASE_KEY)")
        url = SUPABASE_URL + "/rest/v1/" + path_and_query
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json",
        }
        if method == "POST":
            headers["Prefer"] = "return=minimal"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def _log_search(self, body_bytes):
        # best-effort, anonymous logging - a failure here must never surface as an
        # error to the person using the app, since it's not part of their actual analysis
        try:
            payload = json.loads(body_bytes)
        except Exception:
            payload = {}
        record = {
            "mode": payload.get("mode"),
            "lat": payload.get("lat"),
            "lon": payload.get("lon"),
            "address_label": payload.get("addressLabel"),
            "business_type": payload.get("businessType"),
            "score": payload.get("score"),
            "weakest_factor": payload.get("weakestFactor"),
        }
        try:
            self._supabase_request("POST", "searches", record)
        except Exception as exc:
            print("  [שגיאת תיעוד - לא קריטי]", exc)
        self._send_json_bytes(200, json.dumps({"ok": True}).encode("utf-8"))

    def _osm_tag_exists_nearby(self, lat, lon, osm_key, osm_value, radius_m=150):
        q = (
            "[out:json][timeout:12];("
            "node(around:" + str(radius_m) + "," + str(lat) + "," + str(lon) + ")[" + osm_key + "=" + osm_value + "];"
            "way(around:" + str(radius_m) + "," + str(lat) + "," + str(lon) + ")[" + osm_key + "=" + osm_value + "];"
            ");out ids;"
        ).encode("utf-8")
        for endpoint in OVERPASS_ENDPOINTS:
            req = urllib.request.Request(
                endpoint, data=q,
                headers={"User-Agent": "strata-local-proxy/1.0", "Content-Type": "text/plain; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                    return len(data.get("elements", [])) > 0
            except Exception:
                continue
        return None  # all mirrors failed - inconclusive, caller should leave the record for next run

    def _run_recheck_batch(self):
        import datetime
        now = datetime.datetime.utcnow()
        checked = {"recheck_3mo": 0, "recheck_12mo": 0, "skipped_unconfigured": False}
        try:
            for stage, months, min_months in (("recheck_3mo", 4, 3), ("recheck_12mo", 12, 9)):
                cutoff_max = (now - datetime.timedelta(days=min_months * 30)).isoformat()
                query = (
                    "searches?" + stage + "_done=eq.false"
                    + "&created_at=lte." + urllib.parse.quote(cutoff_max)
                    + "&select=id,lat,lon,business_type&limit=15"
                )
                rows = self._supabase_request("GET", query) or []
                for row in rows:
                    bt = row.get("business_type")
                    tag = BUSINESS_TAG_MAP.get(bt)
                    if not tag or row.get("lat") is None or row.get("lon") is None:
                        continue
                    found = self._osm_tag_exists_nearby(row["lat"], row["lon"], tag[0], tag[1])
                    if found is None:
                        continue  # leave for next run
                    update = {stage + "_done": True, stage + "_found": found}
                    self._supabase_request("PATCH", "searches?id=eq." + str(row["id"]), update)
                    checked[stage] += 1
        except Exception as exc:
            if "not configured" in str(exc):
                checked["skipped_unconfigured"] = True
            else:
                print("  [שגיאת recheck]", exc)
        self._send_json_bytes(200, json.dumps(checked).encode("utf-8"))


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
