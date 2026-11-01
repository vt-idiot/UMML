from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import PACKAGE_UMML_ASSETS, ModRecord, Profile
from .options import OptionError, normalize_profile_options, select_source_paths
from .regions import normalize_region
from .safety import SafetyError, normalize_relative_path, validate_sha256


@dataclass(frozen=True)
class Claim:
    mod_id: str
    mod_version: str
    source_path: str
    sha256: str


@dataclass(frozen=True)
class Conflict:
    path: str
    winner: str
    overridden: tuple[str, ...]


@dataclass
class Resolution:
    profile: str
    target_region: str = ""
    target_installation_key: str = ""
    metadata_fingerprint: str = ""
    winners: dict[str, Claim] = field(default_factory=dict)
    known_mod_hashes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unprepared: list[str] = field(default_factory=list)
    stale_prepared: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    wrong_installation: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    invalid_options: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    incompatibility_conflicts: list[str] = field(default_factory=list)
    load_order_conflicts: list[str] = field(default_factory=list)
    # character ID -> mod that supplies that whole character
    character_owners: dict[str, str] = field(default_factory=dict)
    # mod ID -> characters it has but does not supply (excluded or outranked)
    characters_withheld: dict[str, list[str]] = field(default_factory=dict)

    @property
    def blocking_issues(self) -> list[str]:
        return (
            self.missing
            + self.unprepared
            + self.stale_prepared
            + self.unsupported
            + self.incompatible
            + self.wrong_installation
            + self.invalid
            + self.missing_dependencies
            + self.incompatibility_conflicts
            + self.load_order_conflicts
        )


def resolve_profile(
    profile: Profile,
    mods: list[ModRecord],
    *,
    target_region: str = "",
    target_installation_key: str = "",
    metadata_fingerprint: str = "",
) -> Resolution:
    records = {record.id: record for record in mods}
    claims: dict[str, list[Claim]] = {}
    region = normalize_region(target_region or profile.region, default="")
    installation_key = str(target_installation_key or "").strip()
    fingerprint = str(metadata_fingerprint or "").strip().casefold()
    resolution = Resolution(
        profile=profile.name,
        target_region=region,
        target_installation_key=installation_key,
        metadata_fingerprint=fingerprint,
    )
    resolution.known_mod_hashes = _known_mod_hashes(mods)
    if profile.installation_key:
        if not installation_key:
            resolution.wrong_installation.append(
                f"profile is bound to {profile.installation_key}, but the target "
                "installation identity is unverified"
            )
        elif profile.installation_key != installation_key:
            resolution.wrong_installation.append(
                f"profile is bound to {profile.installation_key}, not {installation_key}"
            )
    enabled = _deduplicate_profile(profile.enabled, resolution)
    enabled_set = set(enabled)
    enabled_order = {mod_id: index for index, mod_id in enumerate(enabled)}

    for mod_id in enabled:
        record = records.get(mod_id)
        if record is None:
            resolution.missing.append(mod_id)
            continue
        if (
            record.package_type != PACKAGE_UMML_ASSETS
            or "deploy-files" not in record.capabilities
        ):
            resolution.unsupported.append(
                f"{mod_id} ({record.package_type or 'unknown package type'})"
            )
            continue
        if region and record.regions:
            supported = {
                normalize_region(value, default="")
                for value in record.regions
            }
            if region not in supported:
                resolution.incompatible.append(
                    f"{mod_id} supports {', '.join(record.regions)}, not {region}"
                )
                continue
        missing = [
            dependency
            for dependency in record.dependencies
            if dependency not in enabled_set
        ]
        if missing:
            resolution.missing_dependencies.append(
                f"{mod_id} requires {', '.join(missing)}"
            )
            continue
        conflicts = [
            other
            for other in record.incompatibilities
            if other in enabled_set
        ]
        if conflicts:
            resolution.incompatibility_conflicts.append(
                f"{mod_id} conflicts with {', '.join(conflicts)}"
            )
            continue

        current_index = enabled_order[mod_id]
        must_follow = [
            other
            for other in record.load_after
            if other in enabled_order and current_index <= enabled_order[other]
        ]
        must_precede = [
            other
            for other in record.load_before
            if other in enabled_order and current_index >= enabled_order[other]
        ]
        if must_follow or must_precede:
            details: list[str] = []
            if must_follow:
                details.append("must load after " + ", ".join(must_follow))
            if must_precede:
                details.append("must load before " + ", ".join(must_precede))
            resolution.load_order_conflicts.append(
                f"{mod_id} " + "; ".join(details)
            )
            continue

        # Keep the resolver's structured blocker contract stable. The GUI owns the
        # human-facing automatic-preparation wording and queue state.
        if not record.prepared_path:
            resolution.unprepared.append(mod_id)
            continue
        if record.option_groups:
            payloads = _source_payloads(record)
            if not record.files or not payloads or not record.source_roots:
                resolution.unprepared.append(
                    f"{mod_id} needs option-aware re-preparation"
                )
                continue
        elif not record.files and not record.pending_targets:
            resolution.unprepared.append(mod_id)
            continue

        prepared_against = str(record.prepared_against or "").strip().casefold()
        if fingerprint and not prepared_against:
            resolution.stale_prepared.append(
                f"{mod_id} has no metadata fingerprint; re-prepare it against "
                f"the current metadata {fingerprint[:12]}…"
            )
            continue
        if fingerprint and prepared_against != fingerprint:
            resolution.stale_prepared.append(
                f"{mod_id} was prepared against {prepared_against[:12]}…, "
                f"current metadata is {fingerprint[:12]}…"
            )
            continue

        if record.option_groups:
            _resolve_configurable_record(
                record,
                profile,
                claims,
                resolution,
            )
            continue

        validated: list[tuple[str, str]] = []
        try:
            for relative, sha256 in sorted(record.files.items()):
                validated.append(
                    (
                        normalize_relative_path(relative),
                        validate_sha256(sha256),
                    )
                )
        except SafetyError as exc:
            resolution.invalid.append(f"{mod_id}: {exc}")
            continue
        for relative, sha256 in validated:
            claims.setdefault(relative, []).append(
                Claim(
                    mod_id=record.id,
                    mod_version=record.version,
                    source_path=record.prepared_path,
                    sha256=sha256,
                )
            )

    _apply_character_ownership(profile, enabled, records, claims, resolution)

    for relative, path_claims in claims.items():
        winner = path_claims[-1]
        resolution.winners[relative] = winner
        if len(path_claims) > 1:
            resolution.conflicts.append(
                Conflict(
                    path=relative,
                    winner=winner.mod_id,
                    overridden=tuple(
                        claim.mod_id for claim in path_claims[:-1]
                    ),
                )
            )
    resolution.conflicts.sort(key=lambda item: item.path)
    return resolution


def _resolve_configurable_record(
    record: ModRecord,
    profile: Profile,
    claims: dict[str, list[Claim]],
    resolution: Resolution,
) -> None:
    try:
        payloads = _source_payloads(record)
        selected = normalize_profile_options(
            record.option_groups,
            profile.options.get(record.id, {}),
        )
        selected_sources = select_source_paths(
            record.option_groups,
            selected,
            payloads.keys(),
        )
        selected_claims: dict[str, Claim] = {}
        selected_owners: dict[str, str] = {}
        for source in sorted(selected_sources):
            if source not in payloads or source not in record.source_roots:
                raise OptionError(
                    f"prepared source payload is incomplete for {source!r}; re-prepare the mod"
                )
            root_relative = normalize_relative_path(record.source_roots[source])
            for raw_target, raw_sha256 in sorted(payloads[source].items()):
                target = normalize_relative_path(raw_target)
                sha256 = validate_sha256(raw_sha256)
                previous = selected_owners.get(target)
                if previous is not None:
                    raise OptionError(
                        "selected source bundles resolve to the same game target: "
                        f"{previous!r} and {source!r} -> {target}"
                    )
                selected_owners[target] = source
                selected_claims[target] = Claim(
                    mod_id=record.id,
                    mod_version=record.version,
                    source_path=str(Path(record.prepared_path) / root_relative),
                    sha256=sha256,
                )
    except (OptionError, SafetyError) as exc:
        _record_option_error(resolution, record.id, str(exc))
        return
    if not selected_claims:
        _record_option_error(
            resolution,
            record.id,
            "selected options produced no deployable assets",
        )
        return
    for target, claim in selected_claims.items():
        claims.setdefault(target, []).append(claim)


def _source_payloads(record: ModRecord) -> dict[str, dict[str, str]]:
    if record.source_payloads:
        return {
            str(source): {
                str(target): str(sha256)
                for target, sha256 in payload.items()
            }
            for source, payload in record.source_payloads.items()
            if payload
        }
    payloads: dict[str, dict[str, str]] = {}
    for source, target in record.source_files.items():
        sha256 = record.source_hashes.get(source)
        if sha256:
            payloads[str(source)] = {str(target): str(sha256)}
    return payloads


def _known_mod_hashes(mods: list[ModRecord]) -> dict[str, tuple[str, ...]]:
    known: dict[str, set[str]] = {}
    for record in mods:
        if record.package_type != PACKAGE_UMML_ASSETS:
            continue
        try:
            payloads = _source_payloads(record) if record.option_groups else {}
            if payloads:
                for payload in payloads.values():
                    for target, sha256 in payload.items():
                        canonical = normalize_relative_path(target)
                        known.setdefault(canonical, set()).add(
                            validate_sha256(sha256)
                        )
            else:
                for relative, sha256 in record.files.items():
                    canonical = normalize_relative_path(relative)
                    known.setdefault(canonical, set()).add(validate_sha256(sha256))
        except SafetyError:
            continue
    return {
        relative: tuple(sorted(hashes))
        for relative, hashes in known.items()
    }


def _record_option_error(
    resolution: Resolution,
    mod_id: str,
    message: str,
) -> None:
    detail = f"{mod_id}: profile options: {message}"
    resolution.invalid_options.append(detail)
    resolution.invalid.append(detail)


def _deduplicate_profile(
    values: list[str],
    resolution: Resolution,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        mod_id = str(value)
        if mod_id in seen:
            resolution.duplicates.append(mod_id)
            continue
        seen.add(mod_id)
        result.append(mod_id)
    return result


def _apply_character_ownership(
    profile: Profile,
    enabled: list[str],
    records: dict[str, ModRecord],
    claims: dict[str, list[Claim]],
    resolution: Resolution,
) -> None:
    """Give each character to exactly one mod, as a whole.

    Mixing one character's files from two mods (mesh from one, textures from
    another) is what produces broken shoulders and creases, so:

    * a profile can exclude characters from a mod;
    * of the mods that still touch a character, the highest-priority one
      supplies all of that character's files and the others supply none;
    * "fill gaps" mods rank below every normal mod, wherever they sit.

    Files with no character ID (shared assets) and configurable mods keep the
    normal per-file "later mod wins" rule, except that fill-gaps mods still
    rank lowest.
    """

    fill = set(profile.fill_gaps)

    def priority(mod_id: str) -> tuple[int, int]:
        return (0 if mod_id in fill else 1, enabled.index(mod_id) if mod_id in enabled else -1)

    def character(claim: Claim, target: str) -> str | None:
        record = records.get(claim.mod_id)
        if record is None or record.option_groups:
            return None
        return record.target_characters.get(target)

    withheld: dict[str, set[str]] = {}

    # 1. Exclusions.
    for target, path_claims in claims.items():
        kept = []
        for claim in path_claims:
            ch = character(claim, target)
            if ch and ch in profile.excluded_characters.get(claim.mod_id, ()):
                withheld.setdefault(claim.mod_id, set()).add(ch)
                continue
            kept.append(claim)
        path_claims[:] = kept

    # 2. One owner per character: the highest-priority mod that touches it.
    owners: dict[str, str] = {}
    for target, path_claims in claims.items():
        for claim in path_claims:
            ch = character(claim, target)
            if not ch:
                continue
            current = owners.get(ch)
            if current is None or priority(claim.mod_id) > priority(current):
                owners[ch] = claim.mod_id

    # 3. Drop every other mod's files for owned characters, then order the
    #    remaining claims so the last one (the winner) has the highest priority.
    for target in list(claims):
        kept = []
        for claim in claims[target]:
            ch = character(claim, target)
            if ch and owners.get(ch) != claim.mod_id:
                withheld.setdefault(claim.mod_id, set()).add(ch)
                continue
            kept.append(claim)
        kept.sort(key=lambda claim: priority(claim.mod_id))
        if kept:
            claims[target] = kept
        else:
            del claims[target]

    resolution.character_owners = dict(sorted(owners.items()))
    resolution.characters_withheld = {
        mod_id: sorted(values) for mod_id, values in sorted(withheld.items())
    }
