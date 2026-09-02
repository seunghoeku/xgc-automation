"""Text-level edits of an XGC namelist for the f_init procedure.

Scalar overrides reuse :func:`tools.namelist.apply_overrides`, so comments,
ordering, and spacing survive and the resulting diff is small enough to
review. On top of that this module edits one slot of a per-species array
(``ptl_f_init_file``, ``ptl_maxwellian_init`` hold one value per species),
which the generic override path does not know about.

No XGC metadata is required: every parameter this procedure touches is
already present in the source input, and the fallback insertion for an
absent one places it by name prefix.
"""

from __future__ import annotations

import re

from tools import namelist as nml


class NamelistEditError(RuntimeError):
    pass


_ASSIGN = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^!]*)(!.*)?$")

# Where to insert a parameter that the source file does not have. XGC groups
# its namelists by prefix, so this covers everything the procedure sets.
_PREFIX_TO_NAMELIST = {
    "sml_": "sml_param",
    "ptl_": "ptl_param",
    "f0_": "f0_param",
    "diag_": "diag_param",
    "eq_": "eq_param",
    "col_": "col_param",
}


def read_value(text: str, name: str) -> str | None:
    """Return the raw right-hand side of ``name=...``, comment stripped."""
    for line in text.splitlines():
        match = _ASSIGN.match(line)
        if match and match.group(2) == name:
            return match.group(3).strip()
    return None


def split_array(value: str) -> list[str]:
    """Split a Fortran array literal into tokens, keeping quotes on strings.

    ``'a.bp' 'b.bp' 'c.bp'`` -> ``["'a.bp'", "'b.bp'", "'c.bp'"]`` and
    ``.true. .false. .true.`` -> three tokens. Commas are accepted separators.
    """
    return re.findall(r"'[^']*'|\"[^\"]*\"|[^\s,]+", value)


def unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def set_slot(value: str, slot: int, new_token: str) -> str:
    """Replace the 1-based ``slot`` of an array literal and re-render it."""
    tokens = split_array(value)
    if slot < 1:
        raise NamelistEditError(f"species slot must be 1-based, got {slot}")
    if slot > len(tokens):
        raise NamelistEditError(
            f"array has {len(tokens)} value(s); cannot set slot {slot} "
            f"in {value!r}. Extend the array in the source input first."
        )
    tokens[slot - 1] = new_token
    return " ".join(tokens)


def array_slot(text: str, name: str, slot: int) -> str | None:
    """The unquoted value in ``slot`` of array parameter ``name``, if present."""
    raw = read_value(text, name)
    if raw is None:
        return None
    tokens = split_array(raw)
    if slot < 1 or slot > len(tokens):
        return None
    return unquote(tokens[slot - 1])


def apply(text: str, overrides: dict[str, str]) -> tuple[str, list[str]]:
    """Apply ``name -> rendered value`` overrides to namelist text.

    Present parameters are edited in place through the shared tool. Absent
    ones are appended to the namelist block chosen by prefix, since there is
    no metadata available locally to place them.
    """
    present = {k: v for k, v in overrides.items() if read_value(text, k) is not None}
    absent = {k: v for k, v in overrides.items() if k not in present}

    new_text, notes = nml.apply_overrides(text, present, metadata={})

    lines = new_text.splitlines(keepends=True)
    for name, value in absent.items():
        block = next(
            (nl for prefix, nl in _PREFIX_TO_NAMELIST.items() if name.startswith(prefix)),
            None,
        )
        if block is None:
            raise NamelistEditError(f"do not know which namelist holds {name!r}")
        for idx, line in enumerate(lines):
            if line.strip().startswith(f"&{block}"):
                lines.insert(idx + 1, f"{name}={value}\n")
                notes.append(f"{name}: (absent) -> {value}  [added to &{block}]")
                break
        else:
            raise NamelistEditError(f"namelist &{block} not found; cannot add {name}")
    return "".join(lines), notes
