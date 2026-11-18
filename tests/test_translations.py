# SPDX-License-Identifier: GPL-3.0-or-later
"""Mirror of the hassfest translation rules we can break (hassfest itself runs in CI).

Keeps errors like upper-case selector option keys from only surfacing in CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "petra_tracker"
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))

# Same expressions as script/hassfest/translations.py in Home Assistant core.
RE_TRANSLATION_KEY = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
RE_PLACEHOLDER_IN_SINGLE_QUOTES = re.compile(r"'{\w+}'")
RE_URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)


def _keys_must_be_valid(mapping: dict, where: str) -> list[str]:
    return [f"{where}.{k}" for k in mapping if not RE_TRANSLATION_KEY.match(k)]


def _strings(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(obj, str):
        yield path, obj


def test_english_translation_equals_strings():
    assert json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")) == STRINGS


def test_keys_follow_hassfest_rules():
    bad: list[str] = []
    for sel, spec in STRINGS.get("selector", {}).items():
        bad += _keys_must_be_valid({sel: 0}, "selector")
        bad += _keys_must_be_valid(spec.get("options", {}), f"selector.{sel}.options")
    for flow in ("config", "options"):
        section = STRINGS.get(flow, {})
        for kind in ("error", "abort"):
            bad += _keys_must_be_valid(section.get(kind, {}), f"{flow}.{kind}")
        for step_id, step in section.get("step", {}).items():
            bad += _keys_must_be_valid({step_id: 0}, f"{flow}.step")
            bad += _keys_must_be_valid(step.get("menu_options", {}), f"{flow}.step.{step_id}.menu_options")
    for platform, entities in STRINGS.get("entity", {}).items():
        for key, spec in entities.items():
            bad += _keys_must_be_valid({key: 0}, f"entity.{platform}")
            bad += _keys_must_be_valid(spec.get("state", {}), f"entity.{platform}.{key}.state")
    assert bad == []


def test_data_descriptions_belong_to_fields():
    for flow in ("config", "options"):
        for step_id, step in STRINGS.get(flow, {}).get("step", {}).items():
            extra = set(step.get("data_description", {})) - set(step.get("data", {}))
            assert not extra, f"{flow}.step.{step_id}: data_description keys without data: {extra}"


def test_texts_have_no_urls_or_single_quoted_placeholders():
    for path, text in _strings(STRINGS):
        assert not RE_URL.search(text), f"{path}: URLs are rejected by hassfest; use placeholders"
        assert not RE_PLACEHOLDER_IN_SINGLE_QUOTES.search(text), path


def test_selector_options_in_code_match_translations():
    from custom_components.petra_tracker.core.client import COUNTRIES
    from custom_components.petra_tracker.core.routing import ENGINES, WAZE_REGIONS

    sel = STRINGS["selector"]
    assert set(sel["country"]["options"]) == set(COUNTRIES)
    assert set(sel["routing"]["options"]) == set(ENGINES)
    assert set(sel["waze_region"]["options"]) == set(WAZE_REGIONS)
