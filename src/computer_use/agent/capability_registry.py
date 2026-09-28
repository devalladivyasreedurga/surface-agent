"""Owns artifact persistence, versioning, and deduplication.

Files are named `<capability_id>.v<version>.json`. The registry is the only
thing that writes to `artifacts/` -- ArtifactRecorder only builds candidate
objects in memory. This split is what makes "discovery ran twice, produced
one artifact" a property of *this* module rather than something the caller
has to remember to check.

Dedup is structural, not textual: two candidates are "materially identical"
if their steps/inputs/outputs/checkpoints match once discovery-run-specific
noise (literal example values, timestamps, provenance, free-text
name/description wording) is stripped out. Same flow recorded under a
slightly different goal phrasing should still collapse to one artifact;
a flow whose steps genuinely changed should not.
"""
from __future__ import annotations

import re
from pathlib import Path

from computer_use.models.actions import Locator, Target
from computer_use.models.artifact import Checkpoint, CapabilityArtifact

_FILENAME_RE = re.compile(r"^(?P<capability_id>.+)\.v(?P<version>\d+)\.json$")


class CapabilityRegistry:
    def __init__(self, artifacts_dir: Path):
        self._dir = artifacts_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---- reading -----------------------------------------------------

    def list_all_versions(self, capability_id: str) -> list[CapabilityArtifact]:
        versions = []
        for path in self._dir.glob(f"{capability_id}.v*.json"):
            m = _FILENAME_RE.match(path.name)
            if m and m.group("capability_id") == capability_id:
                versions.append(CapabilityArtifact.model_validate_json(path.read_text()))
        return sorted(versions, key=lambda a: a.capability_version)

    def list_capabilities(self) -> list[CapabilityArtifact]:
        """Latest version of every distinct capability_id, sorted by name."""
        by_id: dict[str, CapabilityArtifact] = {}
        for path in self._dir.glob("*.json"):
            m = _FILENAME_RE.match(path.name)
            if not m:
                continue
            artifact = CapabilityArtifact.model_validate_json(path.read_text())
            existing = by_id.get(artifact.capability_id)
            if existing is None or artifact.capability_version > existing.capability_version:
                by_id[artifact.capability_id] = artifact
        return sorted(by_id.values(), key=lambda a: a.name)

    def get(self, capability_id: str, version: int | None = None) -> CapabilityArtifact | None:
        if version is None:
            versions = self.list_all_versions(capability_id)
            return versions[-1] if versions else None
        path = self.path_for(capability_id, version)
        if not path.exists():
            return None
        return CapabilityArtifact.model_validate_json(path.read_text())

    def path_for(self, capability_id: str, version: int) -> Path:
        return self._dir / f"{capability_id}.v{version}.json"

    # ---- writing -------------------------------------------------------

    def save(self, artifact: CapabilityArtifact) -> Path:
        path = self.path_for(artifact.capability_id, artifact.capability_version)
        path.write_text(artifact.model_dump_json(indent=2))
        return path

    def reconcile(self, candidate: CapabilityArtifact, run_id: str) -> tuple[CapabilityArtifact, bool, str]:
        """Decide whether `candidate` (freshly built, version defaults to 1)
        needs to be saved as a new artifact, reused as-is, or saved as the
        next version. Returns (artifact_to_use, was_newly_written, reason).
        """
        versions = self.list_all_versions(candidate.capability_id)
        if not versions:
            candidate.capability_version = 1
            self.save(candidate)
            return candidate, True, f"created {candidate.capability_id}.v1"

        latest = versions[-1]
        if _fingerprint(latest) == _fingerprint(candidate):
            if run_id != latest.created_from_run_id and run_id not in latest.additional_run_ids:
                latest.additional_run_ids.append(run_id)
                self.save(latest)
            return latest, False, f"reused existing {latest.capability_id}.v{latest.capability_version}"

        new_version = latest.capability_version + 1
        candidate.capability_version = new_version
        self.save(candidate)
        return candidate, True, f"created new version {candidate.capability_id}.v{new_version} (flow changed)"


def _locator_fingerprint(loc: Locator | None) -> tuple | None:
    if loc is None:
        return None
    return (loc.strategy.value, loc.value)


def _target_fingerprint(target: Target | None) -> tuple | None:
    if target is None:
        return None
    return (_locator_fingerprint(target.primary), tuple(_locator_fingerprint(f) for f in target.fallbacks))


def _checkpoint_fingerprint(cp: Checkpoint | None) -> tuple | None:
    if cp is None:
        return None
    return (
        cp.expect_url_contains,
        cp.expect_text_contains,
        _locator_fingerprint(cp.expect_element),
        _locator_fingerprint(cp.expect_element_visible),
        cp.expect_output_present,
        tuple(_checkpoint_fingerprint(alt) for alt in cp.any_of),
    )


def _fingerprint(artifact: CapabilityArtifact) -> tuple:
    """Normalized structural signature. Deliberately excludes: name,
    description, step descriptions, timestamps, created_from_run_id,
    additional_run_ids, review_notes, and input `example` values (the whole
    point is that two runs with different example member IDs still match)."""
    steps_fp = tuple(
        (
            s.action.type.value,
            _target_fingerprint(s.action.target),
            s.action.value,
            s.risk.value,
            s.extract_as,
            _checkpoint_fingerprint(s.checkpoint),
        )
        for s in artifact.steps
    )
    inputs_fp = tuple(sorted((p.name, p.type.value, p.required) for p in artifact.inputs))
    outputs_fp = tuple(sorted((o.name, o.type.value, o.source_step_id) for o in artifact.outputs))
    return (
        artifact.target.app_key,
        inputs_fp,
        outputs_fp,
        steps_fp,
        _checkpoint_fingerprint(artifact.success_checkpoint),
    )
