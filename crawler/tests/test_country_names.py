"""Country-name loader tests.

Specifically guards the YAML 1.1 "Norway problem": unquoted ``NO`` parses
as the boolean False, ``ON`` as True. The loader in export_for_site.py
re-stringifies those keys so they land in countries_names.json as the
country codes they were meant to be.
"""
import importlib.util
from pathlib import Path

import yaml

_spec = importlib.util.spec_from_file_location(
    "_export_for_site",
    Path(__file__).resolve().parents[1] / "scripts" / "export_for_site.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_norway_is_loaded_even_when_unquoted_in_yaml(tmp_path, monkeypatch):
    yaml_text = "names:\n  NO: Norway\n  US: United States\n"
    f = tmp_path / "countries.yaml"
    f.write_text(yaml_text)
    monkeypatch.setattr(_mod, "COUNTRIES_CONFIG_PATH", f)
    out = _mod.load_country_names()
    assert out["NO"] == "Norway"
    assert out["US"] == "United States"


def test_on_is_loaded_even_when_unquoted_in_yaml(tmp_path, monkeypatch):
    yaml_text = "names:\n  ON: Ontario\n"
    f = tmp_path / "countries.yaml"
    f.write_text(yaml_text)
    monkeypatch.setattr(_mod, "COUNTRIES_CONFIG_PATH", f)
    out = _mod.load_country_names()
    assert out["ON"] == "Ontario"


def test_normal_codes_load_unchanged(tmp_path, monkeypatch):
    yaml_text = "names:\n  GB: United Kingdom\n  FR: France\n"
    f = tmp_path / "countries.yaml"
    f.write_text(yaml_text)
    monkeypatch.setattr(_mod, "COUNTRIES_CONFIG_PATH", f)
    out = _mod.load_country_names()
    assert out == {"GB": "United Kingdom", "FR": "France"}


def test_actual_config_includes_norway():
    """The shipped config/countries.yaml should round-trip Norway."""
    out = _mod.load_country_names()
    assert out.get("NO") == "Norway", (
        "NO key missing from countries_names — has someone unquoted it again? "
        "Check config/countries.yaml: the line must read '\"NO\": Norway', not 'NO: Norway'."
    )


def test_yaml_no_actually_does_parse_as_false():
    # Document the YAML 1.1 bug we're guarding against.
    assert yaml.safe_load("NO: x")["__placeholder__".replace("__placeholder__", "x")] if False else True
    parsed = yaml.safe_load("NO: x")
    assert parsed == {False: "x"}, "If this changes, PyYAML may have moved to YAML 1.2; the guard is no longer needed."
