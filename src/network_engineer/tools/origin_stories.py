"""Origin stories — operator-supplied 'why does this exist?' notes.

Heritage networks have years of accumulated config — VLANs, port forwards,
firewall rules, oddly named devices — that look strange to an agent doing a
clean-slate analysis. The operator's history of *why* each artifact exists is
the missing context.

Storage:  config/origin_stories.yaml  (gitignored, per-fork)
Example:  examples/origin_stories.example.yaml  (checked in)

Mirrored to Supabase `origin_stories` table in Phase 11.

Lookup helpers consult by (subject_kind, subject_key). Auditor and security
agent should call get_story() before flagging any non-default config artifact;
finding a recorded rationale (especially with do_not_touch=True) should
materially soften the finding.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from network_engineer.tools.schemas import OriginStory

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _REPO_ROOT / "config" / "origin_stories.yaml"


class OriginStoryRegistry:
    """In-memory store of origin stories with YAML persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._stories: dict[tuple[str, str], OriginStory] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> OriginStoryRegistry:
        registry = cls(path=path)
        if registry.path.exists():
            raw = yaml.safe_load(registry.path.read_text()) or {}
            for entry in raw.get("origin_stories", []):
                story = OriginStory(**entry)
                registry._stories[(story.subject_kind, story.subject_key)] = story
        return registry

    def get(self, subject_kind: str, subject_key: str) -> OriginStory | None:
        return self._stories.get((subject_kind, subject_key))

    def has(self, subject_kind: str, subject_key: str) -> bool:
        return (subject_kind, subject_key) in self._stories

    def upsert(self, story: OriginStory) -> OriginStory:
        story.updated_at = datetime.now(UTC)
        self._stories[(story.subject_kind, story.subject_key)] = story
        return story

    def all_for_kind(self, subject_kind: str) -> list[OriginStory]:
        return [s for (k, _), s in self._stories.items() if k == subject_kind]

    def save(self) -> None:
        if not self._stories:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "origin_stories": [
                s.model_dump(mode="json", exclude_none=True)
                for s in sorted(self._stories.values(),
                                key=lambda s: (s.subject_kind, s.subject_key))
            ],
        }
        self.path.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120),
        )

    def __len__(self) -> int:
        return len(self._stories)
