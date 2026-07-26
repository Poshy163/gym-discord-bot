"""Static guard against reading a module global that nothing ever binds.

This exists because the settings refactor deleted ~250 lines of module-level
``NAME = os.getenv(...)`` bindings and replaced them with a ``_bind_config``
function that assigns into ``globals()``. One leftover survived that cut:
``_gid`` was still read inside ``_command_tree_signature``, which is the first
statement of ``_sync_commands`` — so every boot raised NameError, the bare
``except Exception`` in ``on_ready`` swallowed it, and the bot came up looking
healthy while publishing zero slash commands. ``/sync`` could not fix it,
because ``/sync`` is itself a slash command.

Nothing caught it: it is not a syntax error, the module imports fine, and no
test exercised that path. A grep for SCREAMING_CASE config names missed it
because the leftover was lowercase.

``symtable`` does the scope analysis properly — for each nested scope it knows
whether a name resolves to a global — so this is a real check rather than a
regex approximation.
"""
from __future__ import annotations

import builtins
import pathlib
import re
import symtable

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
BUILTINS = set(dir(builtins))

#: Names bound dynamically via ``globals()["NAME"] = ...``. app/bot.py's
#: _bind_config does this for every config setting, so they are legitimately
#: absent from the static module scope.
_DYNAMIC = re.compile(r'g\["([A-Za-z_][A-Za-z0-9_]*)"\]\s*=')


def _module_bindings(table: symtable.SymbolTable) -> set[str]:
    return {
        sym.get_name()
        for sym in table.get_symbols()
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace()
    }


def _global_reads(table: symtable.SymbolTable) -> set[str]:
    """Every name that nested scopes resolve to a module global."""
    found: set[str] = set()
    for child in table.get_children():
        for sym in child.get_symbols():
            # is_global() means "resolves to module or builtin scope". Skip
            # names the scope also assigns — those are its own locals or an
            # explicit `global x` write.
            if sym.is_global() and sym.is_referenced() and not sym.is_assigned():
                found.add(sym.get_name())
        found |= _global_reads(child)
    return found


@pytest.mark.parametrize(
    "path", sorted(APP.glob("*.py")), ids=lambda p: p.name,
)
def test_module_has_no_unbound_global_reads(path: pathlib.Path):
    source = path.read_text(encoding="utf-8")
    table = symtable.symtable(source, str(path), "exec")

    allowed = _module_bindings(table) | BUILTINS
    allowed |= set(_DYNAMIC.findall(source))
    allowed.add("__file__")
    allowed.add("__name__")
    allowed.add("__doc__")

    missing = sorted(name for name in _global_reads(table) if name not in allowed)
    assert not missing, (
        f"{path.name} reads module globals that nothing binds: {missing}. "
        f"Each is a NameError the moment that code path runs — and if the "
        f"caller swallows exceptions, it fails silently."
    )
