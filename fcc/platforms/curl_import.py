"""Turn a browser's "Copy as cURL" into a replayable request spec.

ESPN doesn't publish how pick'em submissions are sent, so instead of guessing a
payload we replay the shape of a request you made yourself. This module does the
parsing, and deliberately drops credential-bearing headers: the client already
holds your SWID/espn_s2 cookies, and a captured Cookie or Authorization header
written to disk is a credential in a file nobody remembers is there.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

#: Headers we refuse to persist. Cookies come from the secret store instead.
SENSITIVE_HEADERS = {"cookie", "authorization", "x-api-key", "set-cookie"}

#: Headers that describe a specific browser session and only cause confusion.
NOISE_HEADERS = {"content-length", "host", "connection", "accept-encoding"}

_PICKS_SENTINEL = "__FCC_PICKS__"


class CurlParseError(ValueError):
    pass


@dataclass
class ParsedCurl:
    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    dropped_headers: list[str]


def parse_curl(command: str) -> ParsedCurl:
    """Parse a `curl` command line as copied from Chrome/Firefox DevTools."""
    text = command.strip()
    # DevTools wraps long commands across lines with backslashes or carets.
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    if not text.lstrip().startswith("curl"):
        raise CurlParseError("That doesn't look like a curl command.")

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise CurlParseError(f"Could not tokenize the command: {exc}") from exc

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    dropped: list[str] = []
    body: str | None = None

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-X", "--request"):
            method = tokens[i + 1].upper()
            i += 2
        elif tok in ("-H", "--header"):
            raw = tokens[i + 1]
            if ":" in raw:
                name, _, value = raw.partition(":")
                key = name.strip().lower()
                if key in SENSITIVE_HEADERS:
                    dropped.append(key)
                elif key not in NOISE_HEADERS and not key.startswith(":"):
                    headers[name.strip()] = value.strip()
            i += 2
        elif tok in ("-b", "--cookie"):
            dropped.append("cookie")
            i += 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            body = tokens[i + 1]
            i += 2
        elif tok.startswith("--data"):
            body = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 2
        elif tok.startswith("-"):
            # Flags we don't care about (--compressed, -s, --insecure, ...).
            i += 1
        else:
            if url is None:
                url = tok
            i += 1

    if url is None:
        raise CurlParseError("No URL found in the curl command.")
    if method is None:
        method = "POST" if body else "GET"

    return ParsedCurl(method, url, headers, body, sorted(set(dropped)))


def _find_pick_array(node: Any, path: list[str | int] | None = None) -> list[str | int] | None:
    """Locate the array of pick objects inside a captured body.

    Picks look like a list of small dicts mentioning a proposition and an
    outcome. We search rather than assume a key name, since ESPN's spelling
    differs between challenges.
    """
    path = path or []
    if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
        keys = {k.lower() for k in node[0]}
        if any("prop" in k for k in keys) and any(
            ("outcome" in k or "option" in k or "select" in k) for k in keys
        ):
            return path
    if isinstance(node, dict):
        for k, v in node.items():
            found = _find_pick_array(v, [*path, k])
            if found is not None:
                return found
    if isinstance(node, list):
        for idx, v in enumerate(node):
            found = _find_pick_array(v, [*path, idx])
            if found is not None:
                return found
    return None


def _get_path(node: Any, path: list[str | int]) -> Any:
    for step in path:
        node = node[step]
    return node


def _set_path(node: Any, path: list[str | int], value: Any) -> None:
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


def build_write_spec(command: str) -> tuple[dict, dict]:
    """Parse a curl command into WriteSpec kwargs plus a report for the user.

    Returns ``(spec_kwargs, report)``. The report names the field spellings we
    detected and any credentials we dropped, so nothing about the conversion is
    invisible.
    """
    parsed = parse_curl(command)
    report: dict[str, Any] = {
        "method": parsed.method,
        "url": parsed.url,
        "dropped_headers": parsed.dropped_headers,
    }

    if not parsed.body:
        raise CurlParseError(
            "The captured request has no body. Make sure you copied the request "
            "that submits your picks, not a GET that loads the page."
        )

    try:
        doc = json.loads(parsed.body)
    except json.JSONDecodeError as exc:
        raise CurlParseError(
            f"The request body isn't JSON ({exc}). fcc can only templatize JSON bodies."
        ) from exc

    path = _find_pick_array(doc)
    if path is None:
        raise CurlParseError(
            "Could not find an array of pick objects in the captured body. "
            "Submit at least one pick before copying the request, and check that "
            "the body contains objects with proposition and outcome ids."
        )

    sample = _get_path(doc, path)[0]
    prop_field = next((k for k in sample if "prop" in k.lower()), "propositionId")
    opt_field = next(
        (k for k in sample if any(t in k.lower() for t in ("outcome", "option", "select"))),
        "outcomeId",
    )
    conf_field = next(
        (k for k in sample if "conf" in k.lower() or "point" in k.lower()),
        "confidencePoints",
    )

    _set_path(doc, path, _PICKS_SENTINEL)
    template = json.dumps(doc).replace(f'"{_PICKS_SENTINEL}"', "{picks}")

    report.update(
        {
            "picks_path": ".".join(str(p) for p in path),
            "proposition_field": prop_field,
            "option_field": opt_field,
            "confidence_field": conf_field,
            "sample_pick": sample,
        }
    )
    spec_kwargs = {
        "method": parsed.method,
        "url": parsed.url,
        "headers": parsed.headers,
        "body_template": template,
        "proposition_field": prop_field,
        "option_field": opt_field,
        "confidence_field": conf_field,
    }
    return spec_kwargs, report
