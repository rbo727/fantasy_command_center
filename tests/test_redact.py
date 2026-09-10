"""Redacting a captured dump so it is safe to share.

Two properties matter, and they fail in opposite directions: the output must not
leak identifiers, and it must stay a faithful fixture — same keys, same types,
same NFL content.
"""

import json

from fcc.platforms.redact import is_nfl_content, redact, redact_document

# Shaped like a gambit entry response: identifiers, other people, NFL content.
DUMP = {
    "challenge": "nfl-pickem-2026",
    "entryId": "9f8e7d6c",
    "userId": "{2A4B6C8D-1E3F-4A5B-8C7D-9E0F1A2B3C4D}",
    "displayName": "Ryan O.",
    "email": "manager@example.com",
    "avatarUrl": "https://secure.espncdn.com/users/2A4B6C8D/pic.png",
    "scoringPeriodId": 3,
    "propositions": [
        {
            "id": "401671801",
            "lockTime": 1758000000000,
            "possibleOutcomes": [
                {"id": "9001", "abbrev": "BAL", "name": "Baltimore Ravens", "percentage": 41.2},
                {"id": "9002", "abbrev": "KC", "name": "Kansas City Chiefs", "percentage": 58.8},
            ],
        }
    ],
    "group": {
        "groupId": 55512345,
        "name": "Work League",
        "members": [
            {"memberId": "abc-1", "displayName": "Dana Whitfield"},
            {"memberId": "abc-2", "displayName": "Sam Ortiz"},
        ],
    },
}


def _flat_values(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _flat_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _flat_values(v)
    else:
        yield node


# --- nothing personal survives --------------------------------------------
def test_no_personal_value_survives():
    out, _ = redact_document(DUMP)
    blob = json.dumps(out)
    for secret in (
        "2A4B6C8D",            # the ESPN user guid
        "manager@example.com",
        "Ryan O.",
        "Dana Whitfield",
        "Sam Ortiz",
        "Work League",
        "9f8e7d6c",            # entry id
        "55512345",            # group id
        "espncdn.com",         # avatar host
    ):
        assert secret not in blob, f"{secret!r} leaked into the redacted dump"


def test_other_peoples_names_go_even_though_the_key_is_generic():
    out, _ = redact_document(DUMP)
    members = out["group"]["members"]
    assert all(m["displayName"].startswith("name-") for m in members)
    assert all(m["memberId"].startswith("id-") for m in members)


# --- the fixture stays useful ---------------------------------------------
def test_nfl_content_is_kept():
    out, _ = redact_document(DUMP)
    outcomes = out["propositions"][0]["possibleOutcomes"]
    assert [o["abbrev"] for o in outcomes] == ["BAL", "KC"]
    assert [o["name"] for o in outcomes] == ["Baltimore Ravens", "Kansas City Chiefs"]


def test_structure_and_types_are_preserved():
    out, _ = redact_document(DUMP)

    def shape(node):
        if isinstance(node, dict):
            return {k: shape(v) for k, v in node.items() if k != "_fcc_redaction"}
        if isinstance(node, list):
            return [shape(v) for v in node]
        return type(node).__name__

    assert shape(out) == shape(DUMP)


def test_non_personal_fields_are_untouched():
    out, _ = redact_document(DUMP)
    assert out["challenge"] == "nfl-pickem-2026"
    assert out["scoringPeriodId"] == 3
    prop = out["propositions"][0]
    assert prop["id"] == "401671801"          # proposition ids aren't personal
    assert prop["lockTime"] == 1758000000000
    assert prop["possibleOutcomes"][0]["percentage"] == 41.2


def test_a_uuid_anywhere_is_caught_even_under_an_innocuous_key():
    out, _ = redact_document({"someField": "2a4b6c8d-1e3f-4a5b-8c7d-9e0f1a2b3c4d"})
    assert "2a4b6c8d" not in json.dumps(out)


# --- pseudonyms are stable ------------------------------------------------
def test_same_value_maps_to_the_same_pseudonym():
    """Cross-references must still line up, or the fixture stops making sense."""
    doc = {"a": {"userId": "SAME"}, "b": {"userId": "SAME"}, "c": {"userId": "OTHER"}}
    out, _ = redact_document(doc)
    assert out["a"]["userId"] == out["b"]["userId"]
    assert out["a"]["userId"] != out["c"]["userId"]


def test_redaction_is_deterministic_across_runs():
    first, _ = redact_document(DUMP)
    second, _ = redact_document(DUMP)
    assert first == second


def test_int_ids_stay_ints():
    out, _ = redact_document({"groupId": 55512345})
    assert isinstance(out["groupId"], int)
    assert out["groupId"] != 55512345


# --- the report -----------------------------------------------------------
def test_report_lists_paths_but_never_values():
    out, report = redact_document(DUMP)
    assert report.total >= 8
    assert "group.members[].displayName" in report.paths
    blob = json.dumps(out["_fcc_redaction"])
    for secret in ("Dana Whitfield", "Work League", "manager@example.com"):
        assert secret not in blob


def test_report_records_kept_nfl_names_for_review():
    _, report = redact_document(DUMP)
    assert "Baltimore Ravens" in report.kept_names


# --- the NFL test itself ---------------------------------------------------
def test_is_nfl_content():
    assert is_nfl_content("Kansas City Chiefs")
    assert is_nfl_content("KC")
    assert is_nfl_content("BAL@KC")
    assert not is_nfl_content("Work League")
    assert not is_nfl_content("Dana Whitfield")
    # Ambiguous by design in teams.py, so it must not be treated as NFL content.
    assert not is_nfl_content("New York")


def test_redact_accepts_a_bare_list():
    out = redact([{"userId": "x"}, {"userId": "y"}])
    assert all(item["userId"].startswith("id-") for item in out)


# --- identifiers are default-deny -----------------------------------------
def test_an_id_key_nobody_enumerated_is_still_redacted():
    """The groupId bug: an enumerated deny-list is only as good as the last
    response you looked at, so anything id-shaped is redacted by default."""
    out, _ = redact_document(
        {"groupId": 55512345, "subscriberId": "s-1", "poolEntrantId": "pe-9"}
    )
    blob = json.dumps(out)
    for leaked in ("55512345", "s-1", "pe-9"):
        assert leaked not in blob


def test_public_nfl_ids_survive_so_the_fixture_stays_useful():
    doc = {
        "propositions": [
            {"id": "401671801", "eventId": "401671801", "outcomeId": "9001"}
        ],
        "scoringPeriodId": 3,
        "challengeId": "nfl-pickem-2026",
    }
    out, _ = redact_document(doc)
    prop = out["propositions"][0]
    assert prop["id"] == "401671801"
    assert prop["eventId"] == "401671801"
    assert prop["outcomeId"] == "9001"
    assert out["scoringPeriodId"] == 3
    assert out["challengeId"] == "nfl-pickem-2026"


def test_a_bare_id_inside_a_personal_subtree_is_redacted():
    """`id` is public on a proposition and personal on a group."""
    out, _ = redact_document(
        {
            "propositions": [{"id": "401671801"}],
            "group": {"id": "private-pool-77"},
            "leaderboard": [{"id": "entrant-3"}],
        }
    )
    assert out["propositions"][0]["id"] == "401671801"
    blob = json.dumps(out)
    assert "private-pool-77" not in blob
    assert "entrant-3" not in blob


def test_only_the_immediate_parent_decides_personal_scope():
    doc = {
        "entry": {
            "propositions": [{"id": "401671801", "possibleOutcomes": [{"id": "9001"}]}],
            "group": {"id": "private-77", "members": [{"id": "m-1"}]},
        }
    }
    out, _ = redact_document(doc)
    prop = out["entry"]["propositions"][0]
    assert prop["id"] == "401671801"
    assert prop["possibleOutcomes"][0]["id"] == "9001"
    blob = json.dumps(out["entry"]["group"])
    assert "private-77" not in blob
    assert "m-1" not in blob
