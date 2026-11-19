"""Drift gate: platform crosswalk state vs the local derived status.

The point is NOT "platform is the truth". ERP owns inputs the platform does not
have (provider test counts, live acceptance records, template role bindings), so
collapsing to one authority loses them. What this pins is the weaker, checkable
claim: **two independent derivations of the same three-state evidence must not
contradict each other**, and the gate says "cannot tell" as loudly as it says
"drift".

Three exits, kept apart (``line-seam-proof``): 0 clean, 1 drift, 2 undetermined.
Folding "I could not read it" into 0 is what makes every dashboard green while
the read face is unreachable.

The projection under test is the package's own (``bindings.load_status(fetch=…)``)
— not a copy here — so a bug in shaping shows up in BOTH the generator switch and
this gate rather than in one of them only.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "bench_crosswalk_drift.py"


def _mod():
    spec = importlib.util.spec_from_file_location("bench_crosswalk_drift", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _payload(*rows) -> dict:
    return {"domain": "erp", "crosswalks": list(rows)}


def _row(system: str, *, catalog=None, executor_tests=None, live=None) -> dict:
    conn = {}
    if catalog is not None:
        conn["catalog"] = catalog
    if executor_tests is not None:
        conn["executor_tests"] = executor_tests
    if live is not None:
        conn["live"] = live
    return {
        "key": f"external_system:{system}",
        "relation": "same_as",
        "peer": {"kind": "external_id", "system": system, "id": system},
        "connectivity": conn,
    }


def _local(integrations: list[dict]) -> dict:
    return {"schema": "erp-connector-status/v1", "integrations": integrations}


def _integ(provider: str, *, states: dict[str, bool]) -> dict:
    return {
        "provider": provider,
        "state": "connected" if states and all(states.values()) else "not_connected",
        "states": {
            s: {"ok": states.get(s, False), "evidence": f"{provider}/{s}"}
            for s in ("catalog", "executor_tests", "live")
        },
    }


@pytest.fixture(autouse=True)
def _read_face_configured(monkeypatch):
    """``bindings._load_from_read_face`` only calls out when this is set."""
    monkeypatch.setenv("BENCH_CROSSWALK_READ_FACE", "https://bench.example")


def _platform(payload: dict) -> dict:
    """Project a raw read-face payload the way the package does, then bucket it.

    Going through ``bindings.load_status(fetch=…)`` on purpose: the gate must
    consume the SAME projection a future generator switch would, so a shaping
    bug cannot live in one and not the other.
    """
    from fd_erp_vertical_templates import bindings

    return _mod()._states(bindings.load_status(fetch=lambda url: payload))


def _local_states(integrations: list[dict]) -> dict:
    """Bucket a local status the same way the gate does (no projection there)."""
    return _mod()._states(_local(integrations))


# ------------------------------------------------------------- projection #


def test_platform_side_is_the_package_projection_not_a_copy():
    """The gate must consume ``bindings``' projection — same code path as the
    generator switch, so the two cannot disagree about what a provider's state is.
    """
    from fd_erp_vertical_templates import bindings

    body = bindings.load_status(fetch=lambda url: _payload(
        _row("kingdee", catalog={"ok": True, "evidence": "cat"}),
    ))
    assert body["source"] == "bench-read-face"
    assert _mod()._states(body) == {
        "kingdee": {"catalog": {"ok": True, "evidence": "cat"}}
    }


def test_ontology_peers_are_not_vendor_state():
    """A crosswalk onto another layer's key says nothing about a vendor."""
    got = _platform(_payload(
        _row("kingdee", catalog={"ok": True, "evidence": "cat"}),
        {
            "key": "organization:acme",
            "relation": "same_as",
            "peer": {"kind": "ontology", "layer_ref": "foundation",
                     "endpoint": "organization:acme"},
            "connectivity": {"catalog": {"ok": True, "evidence": "x"}},
        },
    ))
    assert set(got) == {"kingdee"}


def test_ok_false_is_absence_not_a_third_state():
    got = _platform(_payload(
        _row("kingdee", catalog={"ok": True, "evidence": "cat"},
             live={"ok": False, "evidence": None}),
    ))
    assert got == {"kingdee": {"catalog": {"ok": True, "evidence": "cat"}}}


# ------------------------------------------------------------------ diff #


def test_agreement_is_clean():
    m = _mod()
    local = _local_states([_integ("kingdee", states={"catalog": True, "executor_tests": True})])
    remote = _platform(_payload(_row(
        "kingdee",
        catalog={"ok": True, "evidence": "kingdee/catalog"},
        executor_tests={"ok": True, "evidence": "kingdee/executor_tests"},
    )))
    result = m.diff(local, remote)
    assert result["drift"] == []
    assert result["evidence_only"] == []


def test_ok_disagreement_is_drift():
    m = _mod()
    local = _local_states([_integ("kingdee", states={"catalog": True, "live": True})])
    remote = _platform(_payload(_row(
        "kingdee", catalog={"ok": True, "evidence": "kingdee/catalog"},
    )))
    assert [(d["provider"], d["state"]) for d in m.diff(local, remote)["drift"]] == [
        ("kingdee", "live")
    ]


def test_evidence_wording_difference_is_a_warning_not_drift():
    m = _mod()
    local = _local_states([_integ("kingdee", states={"catalog": True})])
    local["kingdee"]["catalog"]["evidence"] = "catalog/apps/kingdee.json"
    remote = _platform(_payload(_row(
        "kingdee", catalog={"ok": True, "evidence": "crosswalk:kingdee"},
    )))
    result = m.diff(local, remote)
    assert result["drift"] == []
    assert [(e["provider"], e["state"]) for e in result["evidence_only"]] == [("kingdee", "catalog")]


def test_one_sided_providers_are_coverage_not_drift():
    m = _mod()
    local = _local_states([_integ("kingdee", states={"catalog": True})])
    remote = _platform(_payload(_row("unionpay", catalog={"ok": True, "evidence": "x"})))
    result = m.diff(local, remote)
    assert result["drift"] == []
    assert result["not_in_read_face"] == ["kingdee"]
    assert result["not_in_local"] == ["unionpay"]


# ------------------------------------------------------------- exit codes #


@pytest.fixture()
def local_file(tmp_path):
    p = tmp_path / "connector-status.json"
    p.write_text(json.dumps(_local([
        _integ("kingdee", states={"catalog": True, "live": True}),
    ])), encoding="utf-8")
    return p


def _patched(m, payload):
    """Point the gate at a fake read face; returns the monkeypatch setup."""
    return lambda url: payload


def test_clean_run_exits_0(local_file, monkeypatch):
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.setenv(m.TOKEN_ENV, "tok")
    payload = _payload(_row(
        "kingdee",
        catalog={"ok": True, "evidence": "kingdee/catalog"},
        live={"ok": True, "evidence": "kingdee/live"},
    ))
    monkeypatch.setattr(m, "_http_fetch", lambda token: (lambda url: payload))
    assert m.main(["--local", str(local_file)]) == 0


def test_drift_run_exits_1_and_names_the_provider(local_file, monkeypatch, capsys):
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.setenv(m.TOKEN_ENV, "tok")
    payload = _payload(_row(
        "kingdee", catalog={"ok": True, "evidence": "kingdee/catalog"},
    ))
    monkeypatch.setattr(m, "_http_fetch", lambda token: (lambda url: payload))
    assert m.main(["--local", str(local_file)]) == 1
    out = capsys.readouterr()
    assert "DRIFT kingdee/live" in (out.out + out.err)


def test_unreachable_read_face_exits_2_not_0(local_file, monkeypatch):
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.setenv(m.TOKEN_ENV, "tok")

    def boom(url):
        raise OSError("nope")

    monkeypatch.setattr(m, "_http_fetch", lambda token: boom)
    assert m.main(["--local", str(local_file)]) == 2


def test_missing_read_face_env_exits_2(local_file, monkeypatch):
    m = _mod()
    monkeypatch.delenv(m.READ_FACE_ENV, raising=False)
    assert m.main(["--local", str(local_file)]) == 2


def test_missing_token_exits_2(local_file, monkeypatch):
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.delenv(m.TOKEN_ENV, raising=False)
    assert m.main(["--local", str(local_file)]) == 2


def test_fallback_body_is_not_accepted_as_a_platform_answer(local_file, monkeypatch):
    """``load_status`` falls back to the local file; that is NOT a platform answer.

    Without this, a broken token would look like "clean" — the local file agrees
    with itself perfectly. The gate has to notice it stopped answering.
    """
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.setenv(m.TOKEN_ENV, "tok")

    def boom(url):
        raise OSError("nope")

    monkeypatch.setattr(m, "_http_fetch", lambda token: boom)
    assert m.main(["--local", str(local_file)]) == 2


def test_undetermined_never_reads_like_clean(local_file, monkeypatch, capsys):
    m = _mod()
    monkeypatch.delenv(m.READ_FACE_ENV, raising=False)
    m.main(["--local", str(local_file)])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "判不出" in text
    assert "漂移 0" not in text


def test_fetch_never_echoes_the_token(local_file, monkeypatch, capsys):
    """A refusal reason is an error CLASS — never the URL and never the token."""
    m = _mod()
    monkeypatch.setenv(m.READ_FACE_ENV, "https://bench.example")
    monkeypatch.setenv(m.TOKEN_ENV, "super-secret-token")

    def boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(m, "_http_fetch", lambda token: boom)
    m.main(["--local", str(local_file)])
    out = capsys.readouterr()
    assert "super-secret-token" not in (out.out + out.err)
