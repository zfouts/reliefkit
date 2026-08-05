"""API tests.

A stub source stands in for the real elevation services so these stay offline
and deterministic.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest
from fastapi.testclient import TestClient

from reliefkit.dem import DEMGrid
from reliefkit.geo import BBox
from reliefkit.stl import read_binary_stl
from reliefkit.threemf import read_3mf
from reliefkit.web import app as web_app

RAINIER = {"west": -121.85, "south": 46.75, "east": -121.65, "north": 46.90}


class StubSource:
    name = "stub"
    licence = "n/a"
    attribution = "stub source for tests"

    def covers(self, bbox):
        return True

    def fetch(self, bbox: BBox, target_dim: int) -> DEMGrid:
        n = min(target_dim, 64)
        ys = np.linspace(0, np.pi, n)[:, None]
        xs = np.linspace(0, np.pi, n)[None, :]
        return DEMGrid(500.0 + 800.0 * np.sin(xs) * np.sin(ys), bbox, source=self.name)


@pytest.fixture
def client(monkeypatch):
    stub = StubSource()
    monkeypatch.setitem(web_app.SOURCES, "stub", stub)
    monkeypatch.setattr(web_app, "choose_source", lambda bbox: stub, raising=False)
    for module in ("reliefkit.sources", "reliefkit.pipeline"):
        monkeypatch.setattr(f"{module}.choose_source", lambda bbox: stub, raising=False)
    return TestClient(web_app.app)


# --- routing ------------------------------------------------------------


def test_index_serves_the_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "reliefkit" in res.text
    assert 'id="map"' in res.text


def test_health_reports_ok_without_touching_upstreams(client):
    """The probe must not depend on external services -- otherwise a USGS
    outage would make Docker restart a perfectly healthy container."""
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["version"]


def test_health_works_even_when_every_source_is_down(client, monkeypatch):
    from reliefkit.sources import SourceError

    broken = StubSource()
    broken.fetch = lambda bbox, n: (_ for _ in ()).throw(SourceError("down"))
    monkeypatch.setitem(web_app.SOURCES, "stub", broken)
    assert client.get("/api/health").status_code == 200


def test_sources_are_listed_with_licences(client):
    body = client.get("/api/sources").json()
    names = {s["name"] for s in body["sources"]}
    assert {"usgs3dep", "copernicus"} <= names
    assert all(s["licence"] and s["attribution"] for s in body["sources"])


# --- preview ------------------------------------------------------------


def test_preview_returns_raw_elevations_and_ground_size(client):
    body = client.post("/api/preview", json={**RAINIER, "source": "stub"}).json()
    assert body["rows"] * body["cols"] == len(body["elevations_m"])
    assert body["elevation"]["max_m"] > body["elevation"]["min_m"]
    assert body["ground"]["width_m"] > 0
    assert body["source"] == "stub"
    assert body["attribution"]


def test_preview_squares_the_bbox_when_asked(client):
    body = client.post("/api/preview", json={**RAINIER, "source": "stub", "square": True}).json()
    box = BBox(**body["bbox"])
    assert box.aspect == pytest.approx(1.0, rel=1e-5)


def test_preview_leaves_bbox_alone_when_not_squaring(client):
    body = client.post("/api/preview", json={**RAINIER, "source": "stub", "square": False}).json()
    assert body["bbox"]["west"] == pytest.approx(RAINIER["west"])


def test_preview_estimate_tracks_the_export_grid(client):
    small = client.post("/api/preview", json={**RAINIER, "source": "stub", "grid": 200}).json()
    large = client.post("/api/preview", json={**RAINIER, "source": "stub", "grid": 800}).json()
    assert large["estimated"]["triangles"] > small["estimated"]["triangles"] * 10
    assert large["estimated"]["bytes"] == 84 + 50 * large["estimated"]["triangles"]


def test_flat_region_is_reported_as_a_warning(client, monkeypatch):
    flat = StubSource()
    flat.fetch = lambda bbox, n: DEMGrid(np.full((32, 32), 100.0), bbox, source="stub")
    monkeypatch.setitem(web_app.SOURCES, "stub", flat)
    body = client.post("/api/preview", json={**RAINIER, "source": "stub"}).json()
    assert any("flat" in w for w in body["warnings"])


# --- export -------------------------------------------------------------


def test_export_returns_a_valid_binary_stl(client):
    res = client.post("/api/export", json={**RAINIER, "source": "stub", "grid": 120})
    assert res.status_code == 200
    assert res.headers["content-type"] == "model/stl"
    assert res.headers["content-disposition"].endswith('.stl"')

    body = res.content
    (count,) = struct.unpack("<I", body[80:84])
    assert count == int(res.headers["X-Triangle-Count"])
    assert len(body) == 84 + 50 * count
    assert body[:5].lower() != b"solid", "binary STL header must not be mistaken for ASCII"


def test_export_honours_fit_scale(client, tmp_path):
    res = client.post(
        "/api/export",
        json={**RAINIER, "source": "stub", "grid": 100, "scale_mode": "fit", "target_size_mm": 60},
    )
    path = tmp_path / "out.stl"
    path.write_bytes(res.content)

    _, tris = read_binary_stl(path)
    verts = tris.reshape(-1, 3)
    span = verts.max(axis=0) - verts.min(axis=0)
    assert max(span[0], span[1]) == pytest.approx(60.0, rel=1e-3)
    assert verts[:, 2].min() == pytest.approx(0.0, abs=1e-4), "base must sit on z=0"


def test_export_honours_true_scale(client, tmp_path):
    res = client.post(
        "/api/export",
        json={
            **RAINIER, "source": "stub", "grid": 100, "square": False,
            "scale_mode": "true", "scale_denominator": 100_000, "z_exaggeration": 2.0,
        },
    )
    path = tmp_path / "out.stl"
    path.write_bytes(res.content)

    _, tris = read_binary_stl(path)
    span = tris.reshape(-1, 3).max(axis=0) - tris.reshape(-1, 3).min(axis=0)
    expected_x = BBox(**RAINIER).width_m * 1000.0 / 100_000
    assert span[0] == pytest.approx(expected_x, rel=1e-3)


# --- validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"west": 10, "south": 0, "east": 5, "north": 1}, "region"),
        ({**RAINIER, "grid": 99999}, "grid"),
        ({**RAINIER, "target_size_mm": -5}, "size"),
        ({**RAINIER, "source": "nope"}, "source"),
    ],
)
def test_invalid_requests_are_rejected(client, payload, field):
    res = client.post("/api/preview", json=payload)
    assert res.status_code == 422, f"{field} should have been rejected"


def test_upstream_failure_maps_to_502(client, monkeypatch):
    from reliefkit.sources import SourceError

    broken = StubSource()

    def boom(bbox, n):
        raise SourceError("elevation service unavailable")

    broken.fetch = boom
    monkeypatch.setitem(web_app.SOURCES, "stub", broken)

    res = client.post("/api/preview", json={**RAINIER, "source": "stub"})
    assert res.status_code == 502
    assert "unavailable" in res.json()["detail"]


def test_export_as_3mf(client, tmp_path):
    res = client.post("/api/export", json={**RAINIER, "source": "stub", "grid": 100, "fmt": "3mf"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "model/3mf"
    assert res.headers["content-disposition"].endswith('.3mf"')

    path = tmp_path / "out.3mf"
    path.write_bytes(res.content)
    verts, faces = read_3mf(path)
    assert len(faces) == int(res.headers["X-Triangle-Count"])
    assert len(verts) < len(faces) * 3, "3mf must index vertices, not repeat them per triangle"


def test_3mf_export_is_smaller_than_the_same_stl(client):
    payload = {**RAINIER, "source": "stub", "grid": 200}
    stl = client.post("/api/export", json={**payload, "fmt": "stl"}).content
    mf = client.post("/api/export", json={**payload, "fmt": "3mf"}).content
    assert len(mf) < len(stl) / 3


def test_preview_estimates_both_formats(client):
    body = client.post("/api/preview", json={**RAINIER, "source": "stub", "grid": 500}).json()
    est = body["estimated"]
    assert est["bytes_3mf"] < est["bytes"]
    assert est["bytes"] == 84 + 50 * est["triangles"]


def test_unknown_format_is_rejected(client):
    res = client.post("/api/export", json={**RAINIER, "source": "stub", "fmt": "obj"})
    assert res.status_code == 422
