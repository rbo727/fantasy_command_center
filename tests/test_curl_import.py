"""Capturing the undocumented ESPN write path from a browser request.

The security-relevant assertion here is that credentials in the captured
command never reach disk.
"""

import json

import pytest

from ffm.platforms.curl_import import CurlParseError, build_write_spec, parse_curl
from ffm.platforms.espn_pickem import Pick, WriteSpec

CHROME_CURL = r"""curl 'https://gambit-api.fantasy.espn.com/apis/v1/challenges/nfl-pickem-2026/entries/%7BABC%7D' \
  -X 'POST' \
  -H 'accept: application/json' \
  -H 'content-type: application/json' \
  -H 'cookie: SWID={ABC}; espn_s2=SECRETCOOKIEVALUE' \
  -H 'authorization: Bearer SECRETTOKEN' \
  -H 'origin: https://fantasy.espn.com' \
  --data-raw '{"entryId":"E1","picks":[{"propositionId":"401","outcomeId":"9001"}]}' \
  --compressed"""


def test_parses_method_url_and_headers():
    parsed = parse_curl(CHROME_CURL)
    assert parsed.method == "POST"
    assert parsed.url.endswith("entries/%7BABC%7D")
    assert parsed.headers["accept"] == "application/json"
    assert parsed.headers["origin"] == "https://fantasy.espn.com"


def test_credentials_are_stripped_not_stored():
    parsed = parse_curl(CHROME_CURL)
    lowered = {k.lower() for k in parsed.headers}
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert set(parsed.dropped_headers) == {"authorization", "cookie"}
    assert "SECRETCOOKIEVALUE" not in json.dumps(parsed.headers)
    assert "SECRETTOKEN" not in json.dumps(parsed.headers)


def test_build_write_spec_templatizes_the_pick_array():
    spec_kwargs, report = build_write_spec(CHROME_CURL)
    assert report["picks_path"] == "picks"
    assert report["proposition_field"] == "propositionId"
    assert report["option_field"] == "outcomeId"
    assert "{picks}" in spec_kwargs["body_template"]
    # The captured entry id survives; only the picks become a slot.
    assert "E1" in spec_kwargs["body_template"]


def test_rendered_body_round_trips_to_valid_json():
    spec_kwargs, _ = build_write_spec(CHROME_CURL)
    spec = WriteSpec(**spec_kwargs)
    body = spec.render_body(
        [Pick("401", "9001", "KC"), Pick("402", "9004", "SF", confidence=3)]
    )
    doc = json.loads(body)
    assert doc["entryId"] == "E1"
    assert doc["picks"] == [
        {"propositionId": "401", "outcomeId": "9001"},
        {"propositionId": "402", "outcomeId": "9004", "confidencePoints": 3},
    ]


def test_detects_alternative_field_spellings():
    """ESPN renames these between challenges; we read them off the capture."""
    curl = (
        "curl 'https://gambit-api.fantasy.espn.com/apis/v1/x' -X POST "
        "--data-raw '{\"selections\":[{\"propId\":\"1\",\"selectedOptionId\":\"2\"}]}'"
    )
    spec_kwargs, report = build_write_spec(curl)
    assert report["proposition_field"] == "propId"
    assert report["option_field"] == "selectedOptionId"
    assert report["picks_path"] == "selections"


def test_finds_picks_nested_deeper_in_the_body():
    curl = (
        "curl 'https://x/y' -X POST --data-raw "
        "'{\"entry\":{\"data\":{\"picks\":[{\"propositionId\":\"1\",\"outcomeId\":\"2\"}]}}}'"
    )
    spec_kwargs, report = build_write_spec(curl)
    assert report["picks_path"] == "entry.data.picks"
    body = json.loads(WriteSpec(**spec_kwargs).render_body([Pick("7", "8", "KC")]))
    assert body["entry"]["data"]["picks"] == [{"propositionId": "7", "outcomeId": "8"}]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("wget https://example.com", "curl command"),
        ("curl 'https://x/y'", "no body"),
        ("curl 'https://x/y' --data-raw 'not json'", "isn't JSON"),
        ("curl 'https://x/y' --data-raw '{\"a\":1}'", "Could not find an array"),
    ],
)
def test_unparseable_captures_fail_with_guidance(command, expected):
    with pytest.raises(CurlParseError, match=expected):
        build_write_spec(command)
