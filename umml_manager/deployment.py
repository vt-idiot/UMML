from __future__ import annotations

from . import engine as _engine
from .engine import ApplyError, ApplyResult, LegacyBaselineMigrationRequired
from .resolver import Resolution

_BaseApplyEngine = getattr(
    _engine,
    "_UMML_TRANSACTIONAL_APPLY_ENGINE",
    _engine.ApplyEngine,
)
_engine._UMML_TRANSACTIONAL_APPLY_ENGINE = _BaseApplyEngine  # type: ignore[attr-defined]


class ApplyEngine(_BaseApplyEngine):
    """Public deployment engine with complete resolver checks.

    The transactional implementation remains isolated in ``engine.py``. Every
    supported GUI, CLI, package-level, and compatibility entry point resolves to
    this class so callers cannot accidentally ignore planner blocker categories.

    There is no "is the game running" check: mods can be applied or removed
    with the game open. A file the game has loaded fails with Windows'
    "in use" error, and the transaction rolls back.
    """

    def _validate_resolution(self, resolution: Resolution) -> None:
        groups = (
            ("Missing mods", resolution.missing),
            ("Unprepared mods", resolution.unprepared),
            ("Stale prepared caches", resolution.stale_prepared),
            ("Unsupported packages", resolution.unsupported),
            ("Region incompatibilities", resolution.incompatible),
            ("Wrong installation", resolution.wrong_installation),
            ("Invalid manifests", resolution.invalid),
            ("Missing dependencies", resolution.missing_dependencies),
            (
                "Declared incompatibilities",
                resolution.incompatibility_conflicts,
            ),
        )
        problems = [
            f"{label}: {', '.join(values)}"
            for label, values in groups
            if values
        ]
        if problems:
            raise ApplyError(
                "Profile cannot be applied.\n" + "\n".join(problems)
            )


# Compatibility bridge for older internal imports. Package import executes this
# module before ``umml_manager.cli`` or the GUI action modules, so their existing
# ``from .engine import ApplyEngine`` statements receive the validated facade.
# New code should import from ``umml_manager.deployment`` or package root.
_engine.ApplyEngine = ApplyEngine

__all__ = [
    "ApplyEngine",
    "ApplyError",
    "ApplyResult",
    "LegacyBaselineMigrationRequired",
]
