import json

import extraktion as e


def test_zahlen():
    assert e.zahl_aus_text("1.890,00 €") == 1890
    assert e.zahl_aus_text("1890.00") == 1890
    assert e.zahl_aus_text("1.890") == 1890
    assert e.zahl_aus_text("2,5 Zimmer") == 2.5
    assert e.zahl_aus_text("85,5 m²") == 85.5


def test_normalisierung_text_zu_zahl():
    l = e.normalisiere_inserat({"title": "Whg", "rooms": "3,5", "size_qm": "92 m²",
                                "rent": "1.850 €", "plz": "Berlin 10437", "wbs": "nein"})
    assert l["rooms"] == 3.5 and l["size_qm"] == 92 and l["rent"] == 1850
    assert l["plz"] == "10437" and l["wbs"] is False


def test_normalisierung_unplausible_werte():
    l = e.normalisiere_inserat({"rooms": 85, "rent": 450000})
    assert l["rooms"] is None and l["rent"] is None


def test_abgeschnittenes_json_wird_gerettet():
    roh = '[{"title":"A","rooms":3},{"title":"B","rooms":4},{"title":"C","roo'
    assert [x["title"] for x in e._rette_json_array(roh)] == ["A", "B"]


def test_json_mit_codeblock():
    assert e._rette_json_array('```json\n[{"title":"A"}]\n```') == [{"title": "A"}]


def test_vorfilter():
    assert e.sieht_leer_aus("Impressum Kontakt Über uns")
    assert e.sieht_leer_aus("Derzeit keine freien Wohnungen. Kontakt: 030 123 €")
    assert not e.sieht_leer_aus("3 Zimmer, 90 m², 1.800 € Kaltmiete; 2 Zimmer 60 m² 1.200 €")


def test_jsonld_apartment():
    html = """<script type="application/ld+json">{"@type":"Apartment","name":"Altbau",
      "numberOfRooms":3,"floorSize":{"value":91},"address":{"postalCode":"10435",
      "streetAddress":"Rykestraße 1"},"offers":{"price":"1.790"}}</script>"""
    l, art = e.strukturierte_inserate(html, "https://x.de/")
    assert art == "json-ld" and l[0]["rooms"] == 3 and l[0]["rent"] == 1790


def test_jsonld_product_mit_wohnungstext():
    html = """<script type="application/ld+json">{"@type":"Product",
      "name":"3-Zimmer-Wohnung, 88 m² in Mitte","offers":{"price":1650}}</script>"""
    l, art = e.strukturierte_inserate(html, "https://x.de/")
    assert l[0]["rooms"] == 3 and l[0]["size_qm"] == 88


def test_microdata():
    html = """<div itemscope itemtype="https://schema.org/Apartment">
      <span itemprop="name">Dachgeschoss</span><meta itemprop="numberOfRooms" content="4">
      <span itemprop="floorSize">110 m²</span><span itemprop="postalCode">10119</span></div>"""
    l, art = e.strukturierte_inserate(html, "https://x.de/")
    assert art == "microdata" and l[0]["rooms"] == 4 and l[0]["plz"] == "10119"


def test_eingebettetes_json():
    daten = {"props": {"pageProps": {"objects": [
        {"title": "Whg 1", "rooms": 3, "livingSpace": 85, "address": {"zip": "10405",
         "street": "Winsstraße", "houseNumber": "7"}, "price": {"coldRent": 1700}, "url": "/expose/1"},
        {"title": "Whg 2", "rooms": 2, "livingSpace": 55, "address": {"zip": "10405"},
         "price": {"coldRent": 1100}, "url": "/expose/2"},
    ]}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(daten)}</script>'
    l, art = e.strukturierte_inserate(html, "https://x.de/liste")
    assert art == "eingebettetes json" and len(l) == 2
    assert l[0]["street"] == "Winsstraße 7" and l[0]["rent"] == 1700
    assert l[0]["url"] == "https://x.de/expose/1"


def test_xhr_kaufobjekte_werden_ignoriert():
    daten = [{"title": "ETW", "rooms": 3, "livingSpace": 80, "price": 450000, "zip": "10115"}]
    l, _ = e.strukturierte_inserate("<html></html>", "https://x.de/", xhr_daten=[daten])
    assert l == []


def test_feed_erkennung_nur_inseratsfeeds():
    html = """<link rel="alternate" type="application/rss+xml" href="/feed/">
              <link rel="alternate" type="application/rss+xml" href="/comments/feed/">
              <link rel="alternate" type="application/rss+xml" href="/objektstatus/miete/feed/">"""
    assert e.finde_inserats_feeds(html, "https://x.de/") == ["https://x.de/objektstatus/miete/feed/"]


def test_feed_auswertung():
    xml = """<rss><channel><item><title>3-Zimmer-Wohnung, 92 m², 10437 Berlin</title>
      <link>https://x.de/obj/1</link><description>Kaltmiete: 1.850 €</description></item>
      </channel></rss>"""
    l = e.inserate_aus_feed(xml, "https://x.de/feed/")
    assert l[0]["rooms"] == 3 and l[0]["size_qm"] == 92 and l[0]["rent"] == 1850
    assert l[0]["plz"] == "10437"


def test_rate_limiter_misst_ab_letztem_start():
    import time
    from util import RateLimiter
    r = RateLimiter(0.2)
    r.warte()
    time.sleep(0.15)
    t = time.time()
    r.warte()
    assert time.time() - t < 0.1


def test_feed_link_wird_uebernommen():
    xml = """<rss><channel><item><title>3 Zimmer, 90 m²</title>
      <link>https://x.de/obj/7</link></item></channel></rss>"""
    assert e.inserate_aus_feed(xml, "https://x.de/feed/")[0]["url"] == "https://x.de/obj/7"


class _Antwort:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code))


def test_gemini_abgeschnitten_und_schema_fallback(monkeypatch):
    aufrufe = []

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        aufrufe.append(json["generationConfig"])
        if len(aufrufe) == 1:
            return _Antwort(400, {"error": {"message": "Invalid responseSchema"}})
        return _Antwort(200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [
            {"text": '[{"title":"A","rooms":3},{"title":"B","ro'}]}}]})

    monkeypatch.setattr(e.requests, "post", fake_post)
    monkeypatch.setattr(e, "_schema_abgelehnt", False)
    monkeypatch.setattr(e._gemini_takt, "min_abstand_s", 0)
    ergebnis = e.call_gemini_extract("https://x.de", "x" * 100)
    assert [r["title"] for r in ergebnis] == ["A"]
    assert "responseSchema" in aufrufe[0] and "responseSchema" not in aufrufe[1]
    assert e.ki_abgeschnitten >= 1
