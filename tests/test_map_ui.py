from pathlib import Path
BASE=Path(__file__).resolve().parent.parent

def test_map_has_toggle_and_leaflet():
    page=(BASE/'static/index.html').read_text()
    assert 'id="map-view-btn"' in page
    assert 'id="list-view-btn"' in page
    assert 'id="roost-map"' in page
    assert 'leaflet@1.9.4' in page

def test_map_uses_coordinates_without_geocoding():
    js=(BASE/'static/app.js').read_text()
    assert 'function onMap(x)' in js
    assert 'function renderMap()' in js
    assert 'function setViewMode(mode)' in js
    assert 'positioned=sortedListings().filter(onMap)' in js
    assert 'missing usable coordinates' not in js or 'usable map coordinates' in js
    assert 'https://tile.openstreetmap.org/' in js
