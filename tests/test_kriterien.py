import pytest

import kriterien as k


@pytest.fixture(autouse=True)
def kein_netz(monkeypatch):
    """Geocoding nie ins Netz - feste Testkoordinaten."""
    orte = {
        "Kantstraße 50, 10625, Berlin, Germany": (52.5058, 13.3180),   # nahe Savignyplatz
        "Kaiserdamm 100, 14057, Berlin, Germany": (52.5100, 13.2700),  # weit weg
        "Zehdenicker Straße 5, Berlin, Germany": (52.5310, 13.4040),   # Mitte/PB
    }
    monkeypatch.setattr(k, "geocode_address", lambda q: orte.get(q))


def basis(**kw):
    l = {"title": "Schöne Altbauwohnung", "rooms": 3, "size_qm": 90, "rent": 1800,
         "plz": "10435", "status": "verfuegbar"}
    l.update(kw)
    return l


def test_passt():
    assert k.matches_criteria(basis()) == (True, [])


def test_zimmer_knapp():
    ok, g = k.matches_criteria(basis(rooms=2.5))
    assert not ok and g == ["zimmer"]
    assert k.is_near_miss(basis(rooms=2.5), g)


def test_mehrere_gruende_keine_geocodierung():
    ok, g = k.matches_criteria(basis(rooms=1, size_qm=30))
    assert "lage_ungeprueft" in g and not k.is_near_miss(basis(), g)


def test_warmmiete_wird_geprueft():
    # 3200 warm bei 90 qm -> ca. 2930 kalt -> zu teuer (frueher: ungeprueft durch)
    ok, g = k.matches_criteria(basis(rent=None, rent_warm=3200))
    assert not ok and "miete" in g


def test_warmmiete_passend():
    assert k.matches_criteria(basis(rent=None, rent_warm=2000))[0]


@pytest.mark.parametrize("titel,erwartet", [
    ("Wohnung, WBS erforderlich", True),
    ("Wohnung, kein WBS nötig", False),
    ("Wohnung ohne WBS", False),
    ("WBS-frei, 3 Zimmer", False),
])
def test_wbs(titel, erwartet):
    assert k.needs_wbs({"title": titel}) is erwartet


def test_wbs_feld_der_ki():
    ok, g = k.matches_criteria(basis(wbs=True))
    assert g == ["wbs"]


def test_unbefristet_ist_nicht_befristet():
    assert not k.looks_temporary({"title": "unbefristeter Mietvertrag"})
    assert k.looks_temporary({"title": "befristet bis 2027"})
    assert k.looks_temporary({"befristet": True, "title": "Wohnung"})


def test_stellplatz_macht_wohnung_nicht_zu_gewerbe():
    assert not k.looks_commercial({"title": "3-Zimmer-Wohnung mit Tiefgaragenstellplatz"})
    assert k.looks_commercial({"title": "Bürofläche in Mitte"})


def test_charlottenburg_mit_adresse():
    assert k.pruefe_lage(basis(plz="10625", street="Kantstraße 50")) == "ok"


def test_charlottenburg_ohne_adresse_ist_unklar():
    l = basis(plz="10625", street=None, title="Traumwohnung in Charlottenburg")
    ok, g = k.matches_criteria(l)
    assert g == ["lage_unklar"] and k.is_near_miss(l, g)


def test_titel_wird_nur_als_adresse_geocodiert():
    assert k.geocode_query({"title": "Traumhafte 3-Zimmer-Wohnung mit Balkon"}) is None
    assert k.geocode_query({"title": "Altbau in der Zehdenicker Straße 5"}) == \
        "Zehdenicker Straße 5, Berlin, Germany"


def test_ohne_plz_mit_strasse_wird_geocodiert():
    assert k.pruefe_lage(basis(plz=None, street="Zehdenicker Straße 5", title="Wohnung")) == "ok"


def test_zehlendorf_mitte_ist_nicht_mitte():
    assert not k.mentions_area("mitte", "zehlendorf-mitte")
    assert k.mentions_area("mitte", "berlin-mitte")


def test_falsche_plz():
    assert k.matches_criteria(basis(plz="12049"))[1] == ["lage"]
