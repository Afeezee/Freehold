"""Credentials for people without a shell.

The environment must keep winning, the file must never reach the manifest,
and values must never be printed back.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freehold.core import credstore  # noqa: E402
from freehold.core import KeyRing, Manifest  # noqa: E402
from freehold.core.portable import export_manifest  # noqa: E402
from freehold.dashboard import DashboardAPI  # noqa: E402


def test_environment_beats_the_file(tmp_path, monkeypatch):
    credstore.set_value("FH_TEST_TOKEN", "from-file", tmp_path)
    assert credstore.get("FH_TEST_TOKEN", tmp_path) == "from-file"
    monkeypatch.setenv("FH_TEST_TOKEN", "from-env")
    assert credstore.get("FH_TEST_TOKEN", tmp_path) == "from-env"
    assert credstore.sources("FH_TEST_TOKEN", tmp_path) == "environment"


def test_set_list_remove(tmp_path):
    assert credstore.get("NOPE", tmp_path) is None
    assert credstore.sources("NOPE", tmp_path) == "not set"

    credstore.set_value("A_TOKEN", "one", tmp_path)
    credstore.set_value("B_TOKEN", "two", tmp_path)
    assert credstore.names(tmp_path) == ["A_TOKEN", "B_TOKEN"]
    assert credstore.get("A_TOKEN", tmp_path) == "one"

    assert credstore.unset("A_TOKEN", tmp_path) is True
    assert credstore.unset("A_TOKEN", tmp_path) is False
    assert credstore.names(tmp_path) == ["B_TOKEN"]


def test_a_corrupt_file_does_not_crash(tmp_path):
    credstore.path(tmp_path).write_text("{ this is not json")
    assert credstore.get("ANY", tmp_path) is None
    assert credstore.names(tmp_path) == []


def test_credentials_never_ride_along_with_the_manifest(tmp_path):
    """The manifest is copied into the backends for disaster recovery.
    A token must not travel to the service it authenticates to."""
    credstore.set_value("FREEHOLD_GITHUB_TOKEN", "ghp_secret_value",
                        tmp_path)
    m = Manifest(tmp_path / "manifest.db")
    m.add_backend("gh", "local", {"path": str(tmp_path / "blobs")})
    blob = export_manifest(m, KeyRing.generate())
    m.close()
    assert b"ghp_secret_value" not in blob
    assert b"ghp_secret_value" not in (tmp_path / "manifest.db").read_bytes()


def test_the_driver_reads_it(tmp_path, monkeypatch):
    from freehold.core.driver import build_driver
    monkeypatch.delenv("FREEHOLD_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("FREEHOLD_HOME", str(tmp_path))
    with pytest.raises(Exception) as exc:
        build_driver("github", {"owner": "someone"})
    assert "credentials set" in str(exc.value)     # tells you what to do

    credstore.set_value("FREEHOLD_GITHUB_TOKEN", "ghp_x", tmp_path)
    driver = build_driver("github", {"owner": "someone"})
    assert driver.api.token == "ghp_x"


def test_dashboard_lists_names_but_never_values(tmp_path):
    credstore.set_value("FREEHOLD_GITHUB_TOKEN", "ghp_do_not_show", tmp_path)
    m = Manifest(tmp_path / "m.db")
    api = DashboardAPI(tmp_path, m, KeyRing.generate())
    rows = api.credentials()
    payload = json.dumps(rows)
    assert "ghp_do_not_show" not in payload
    assert {"name": "FREEHOLD_GITHUB_TOKEN",
            "source": "credentials.json"} in rows
    # the hints tell the page which boxes to show, without naming vendors
    # anywhere in core
    hints = {d["id"]: d["secrets"] for d in api.drivers()}
    assert hints["github"][0]["name"] == "FREEHOLD_GITHUB_TOKEN"
    assert hints["local"] == []
    m.close()


def test_mobile_can_configure_a_backend_and_a_token(tmp_path):
    """A phone has no shell, so the bridge has to do both."""
    from freehold.mobile import handle
    from freehold.mobile import bridge
    key = KeyRing.generate()
    home = tmp_path / "phone"
    reply = json.loads(handle({"cmd": "enrol", "home": str(home),
                               "key": key.master.hex(), "device": "pixel"}))
    assert reply["ok"]

    saved = json.loads(handle({"cmd": "set_credential", "home": str(home),
                               "name": "FREEHOLD_GITHUB_TOKEN",
                               "value": "ghp_phone"}))
    assert saved["ok"] and saved["stored"] is True

    added = json.loads(handle({"cmd": "add_backend", "home": str(home),
                               "name": "disk", "driver": "local",
                               "options": {"path": str(tmp_path / "blobs")}}))
    assert added["ok"] and added["name"] == "disk"

    status = json.loads(handle({"cmd": "status", "home": str(home)}))
    assert [b["name"] for b in status["backends"]] == ["disk"]

    listed = json.loads(handle({"cmd": "credentials", "home": str(home)}))
    assert "ghp_phone" not in json.dumps(listed)
    bridge.close_all()


def test_mobile_refuses_a_backend_it_cannot_build(tmp_path):
    from freehold.mobile import handle
    from freehold.mobile import bridge
    home = tmp_path / "phone"
    handle({"cmd": "enrol", "home": str(home),
            "key": KeyRing.generate().master.hex(), "device": "pixel"})
    bad = json.loads(handle({"cmd": "add_backend", "home": str(home),
                             "name": "oops", "driver": "local",
                             "options": {}}))
    assert bad["ok"] is False and "path" in bad["error"]
    bridge.close_all()
