from copy import deepcopy

from .contracts import (
    EvaluationReport,
    FeedbackRecord,
    PolicyVersion,
    VersionHistoryEntry,
)


class VersionRegistry:
    """In-memory, deterministic policy registry for the evolution PoC."""

    def __init__(self, *, initial_parameters: dict[str, object]) -> None:
        initial = PolicyVersion(
            version_id="v1",
            parent_version_id=None,
            parameters=deepcopy(initial_parameters),
        )
        self._versions = {initial.version_id: initial}
        self._accepted_ids = [initial.version_id]
        self._active_id = initial.version_id
        self._candidates: dict[str, PolicyVersion] = {}
        self._history: list[VersionHistoryEntry] = []
        self._next_number = 2

    @property
    def active(self) -> PolicyVersion:
        return self._versions[self._active_id].model_copy(deep=True)

    @property
    def accepted_versions(self) -> tuple[PolicyVersion, ...]:
        return tuple(self._versions[item].model_copy(deep=True) for item in self._accepted_ids)

    @property
    def history(self) -> tuple[VersionHistoryEntry, ...]:
        return tuple(self._history)

    def propose(self, feedback: FeedbackRecord) -> PolicyVersion:
        parent = self._versions[self._active_id]
        candidate = PolicyVersion(
            version_id=f"v{self._next_number}",
            parent_version_id=parent.version_id,
            parameters={
                **deepcopy(parent.parameters),
                **deepcopy(feedback.proposed_updates),
            },
        )
        self._next_number += 1
        self._candidates[candidate.version_id] = candidate
        self._versions[candidate.version_id] = candidate
        self._history.append(
            VersionHistoryEntry(action="proposed", version_id=candidate.version_id)
        )
        return candidate.model_copy(deep=True)

    def promote(
        self,
        candidate: PolicyVersion,
        report: EvaluationReport,
    ) -> PolicyVersion:
        stored = self._candidates.get(candidate.version_id)
        if stored is None or stored != candidate:
            raise KeyError("unknown candidate version")
        if not report.passed:
            self._history.append(
                VersionHistoryEntry(action="rejected", version_id=candidate.version_id)
            )
            return self.active

        self._active_id = candidate.version_id
        self._accepted_ids.append(candidate.version_id)
        del self._candidates[candidate.version_id]
        self._history.append(
            VersionHistoryEntry(action="promoted", version_id=candidate.version_id)
        )
        return self.active

    def rollback(self, version_id: str) -> PolicyVersion:
        if version_id not in self._accepted_ids:
            raise KeyError("unknown version")
        current_id = self._active_id
        self._active_id = version_id
        self._history.append(
            VersionHistoryEntry(
                action="rollback",
                version_id=current_id,
                target_version_id=version_id,
            )
        )
        return self.active
