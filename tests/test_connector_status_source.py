"""Connector status: read face first, local mirror as annotated fallback.

Two failure modes this pins:

- falling back to the local snapshot **silently** — an old file then looks
  exactly like a live one, which is what the three-state discipline exists to
  prevent;
- calling an explicit caller-supplied path a "fallback" — it is the caller's own
  snapshot, and labelling it would be a lie.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from fd_erp_vertical_templates import bindings


@pytest.fixture()
def local_snapshot(tmp_path, monkeypatch):
    p = tmp_path / "connector-status.json"
    p.write_text(json.dumps({
        "integrations": [{"provider": "kingdee", "state": "not_connected"}],
        "role_bindings": [{"role": "wms", "providers": ["wms"]}],
    }), encoding="utf-8")
    monkeypatch.setattr(bindings, "DEFAULT_STATUS_PATH", p)
    monkeypatch.delenv(bindings.READ_FACE_ENV, raising=False)
    monkeypatch.delenv(bindings.STALE_AFTER_DAYS_ENV, raising=False)
    return p


def test_read_face_is_tried_first(local_snapshot, monkeypatch):
    """When the platform answers, the local file is not consulted at all."""
    monkeypatch.setenv(bindings.READ_FACE_ENV, "http://bench.invalid")
    status = bindings.load_status(fetch=lambda url: {
        "crosswalks": [{
            "key": "organization:acme",
            "peer": {"kind": "external_id", "system": "kingdee", "id": "BD001"},
            "connectivity": {
                "catalog": {"ok": True, "evidence": "c"},
                "executor_tests": {"ok": True, "evidence": "t"},
                "live": {"ok": True, "evidence": "l"},
            },
        }],
    })
    assert status["source"] == "bench-read-face"
    assert status["integrations"][0]["state"] == "connected"


def test_unreachable_read_face_falls_back_and_says_so(local_snapshot, monkeypatch):
    """The annotation is the whole point: a fallback must be visible."""
    monkeypatch.setenv(bindings.READ_FACE_ENV, "http://bench.invalid")
    status = bindings.load_status(fetch=lambda url: None)
    assert status["source"] == "local-fallback"
    assert status["integrations"][0]["provider"] == "kingdee"


def test_no_read_face_configured_still_marks_the_source(local_snapshot):
    """Unset env = never call out, and the snapshot is still labelled."""
    status = bindings.load_status()
    assert status["source"] == "local-fallback"


def test_explicit_path_is_not_labelled_a_fallback(local_snapshot, monkeypatch):
    monkeypatch.setenv(bindings.READ_FACE_ENV, "http://bench.invalid")
    status = bindings.load_status(local_snapshot)
    assert "source" not in status, (
        "the caller handed us this snapshot; calling it a fallback would misreport"
    )


def test_stale_snapshot_is_reported(local_snapshot, monkeypatch):
    """An old file must not read as a live connection."""
    old = time.time() - 90 * 86400
    os.utime(local_snapshot, (old, old))
    monkeypatch.setenv(bindings.STALE_AFTER_DAYS_ENV, "30")
    status = bindings.load_status()
    assert status["stale"] is True


def test_fresh_snapshot_is_not_stale(local_snapshot, monkeypatch):
    monkeypatch.setenv(bindings.STALE_AFTER_DAYS_ENV, "30")
    assert bindings.load_status()["stale"] is False


def test_no_declared_max_age_means_no_stale_flag(local_snapshot):
    """No expiry declared → say nothing, do not invent a number."""
    assert "stale" not in bindings.load_status()


def test_unreachable_read_face_never_yields_an_empty_success(local_snapshot, monkeypatch):
    """'Could not read' must not become 'nothing is connected'."""
    monkeypatch.setenv(bindings.READ_FACE_ENV, "http://127.0.0.1:1")
    def boom(url):
        raise OSError("unreachable")
    status = bindings.load_status(fetch=boom)
    assert status["source"] == "local-fallback"
    assert status["integrations"], "an unreachable read face is not an empty answer"
