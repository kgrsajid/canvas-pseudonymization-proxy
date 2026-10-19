"""Request rewriting BOTH directions (design §5.1).

App sees only pseudonyms. Proxy:
  - rewrites P-xxxx -> real id in paths, query params, and JSON bodies before
    forwarding upstream;
  - unknown P-xxxx -> UnknownSubject error (fail closed, 404 at route level).
"""

from __future__ import annotations

from typing import Any

from ..pseudonym import is_valid_pseudonym
from ..vault import Vault

# Note: path rewriting is done segment-wise in rewrite_path (no regex needed).



class UnknownSubject(Exception):
    """Pseudonym not present in the vault — fail closed, never guess."""

    def __init__(self, pseudonym: str):
        self.pseudonym = pseudonym
        super().__init__(f"unknown subject {pseudonym}")


class MalformedSubject(Exception):
    """Value looks like a pseudonym reference but is malformed — fail closed."""

    def __init__(self, value: str):
        self.value = value
        super().__init__(f"malformed pseudonym {value!r}")


async def rewrite_path(path: str, vault: Vault) -> str:
    """Rewrite path segments that are P-ids into real ids (fail closed)."""
    parts = path.split("/")
    out = []
    for part in parts:
        if part.startswith("P-"):
            if not is_valid_pseudonym(part):
                raise MalformedSubject(part)
            real = await vault.reverse_lookup(part)
            if real is None:
                raise UnknownSubject(part)
            out.append(str(real))
        else:
            out.append(part)
    return "/".join(out)


async def rewrite_query(params: dict[str, list[str]], vault: Vault) -> dict[str, list[str]]:
    """Rewrite values that are P-ids (e.g. user_id[]=P-xxx). P-ids may also be
    comma-joined (Canvas array params) — each element resolved independently."""
    out: dict[str, list[str]] = {}
    for key, values in params.items():
        new_vals = []
        for v in values:
            if "," in v and all(x.startswith("P-") or x == "" for x in v.split(",")):
                pieces = []
                for x in v.split(","):
                    if not x:
                        continue
                    if not is_valid_pseudonym(x):
                        raise MalformedSubject(x)
                    real = await vault.reverse_lookup(x)
                    if real is None:
                        raise UnknownSubject(x)
                    pieces.append(str(real))
                new_vals.append(",".join(pieces))
            elif v.startswith("P-"):
                if not is_valid_pseudonym(v):
                    raise MalformedSubject(v)
                real = await vault.reverse_lookup(v)
                if real is None:
                    raise UnknownSubject(v)
                new_vals.append(str(real))
            else:
                new_vals.append(v)
        out[key] = new_vals
    return out


async def rewrite_body(data: Any, vault: Vault, *, resolve_keys: frozenset[str] | None = None) -> Any:
    """Rewrite P-ids inside a JSON request body.

    resolve_keys: keys whose values are P-ids or lists of P-ids (per endpoint).
    Default: recipients-like keys handled by callers explicitly; here we walk
    every string value that is a bare valid-format P- candidate and resolve it —
    but only within keys listed, to avoid rewriting free-text.
    """
    if resolve_keys is None:
        return data
    if isinstance(data, list):
        return [await rewrite_body(item, vault, resolve_keys=resolve_keys) for item in data]
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key in resolve_keys:
                out[key] = await _resolve_value(value, vault)
            elif isinstance(value, (dict, list)):
                out[key] = await rewrite_body(value, vault, resolve_keys=resolve_keys)
            else:
                out[key] = value
        return out
    return data


async def _resolve_value(value: Any, vault: Vault) -> Any:
    if isinstance(value, list):
        return [await _resolve_value(v, vault) for v in value]
    if isinstance(value, str) and value.startswith("P-"):
        if not is_valid_pseudonym(value):
            raise MalformedSubject(value)
        real = await vault.reverse_lookup(value)
        if real is None:
            raise UnknownSubject(value)
        return real
    # Canvas conversations accepts numeric-string or int recipients
    if isinstance(value, int):
        return value
    raise MalformedSubject(str(value))
