"""Generate/check PWA references from desktop v1; never opens a journal/config."""

import argparse
import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from zymptom.domain import FunctionalEpisode, EpisodeOutcome, HelpLevel, ResumeAbility
from zymptom.sync.contract import Change, SyncBatch, CONTRACT_VERSION, RECORD_SCHEMA_VERSION
from zymptom.ui import labels

ROOT = Path(__file__).resolve().parents[1]
check = argparse.ArgumentParser()
check.add_argument("--check", action="store_true")
args = check.parse_args()

palette = {}
theme = ROOT.parent / "src/zymptom/ui/theme.py"
for node in ast.parse(theme.read_text()).body:
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        palette[node.targets[0].id] = node.value.value

reference = {
    "contract_version": CONTRACT_VERSION, "record_schema_version": RECORD_SCHEMA_VERSION,
    "record_type": "functional_episode",
    "outcomes": labels.OUTCOMES, "help_levels": labels.HELP_LEVELS, "resume_abilities": labels.RESUME_ABILITIES,
    "tags": {field: getattr(labels, field.upper()) for field in
             ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions")},
    "palette": palette,
}
time = datetime(2026, 10, 25, 22, 15, 30, 123456, tzinfo=UTC)
episode = FunctionalEpisode(
    id=UUID(int=101), activity="DEMO: Café ☕ and lunch", outcome=EpisodeOutcome.COMPLETED_WITH_DIFFICULTY,
    occurred_at=time, duration_minutes=0, functional_demands=["walking", "memory"],
    symptoms=["sensory_overload", "pain"], symptom_notes="雪\nSecond line", adaptations=["changed_position"],
    help_level=HelpLevel.NONE, help_notes="", consequences=["needed_rest"], recovery_required=False,
    recovery_actions=["lay_down"], recovery_minutes=None, resume_ability=ResumeAbility.AFTER_SHORT_BREAK,
    notes="Disposable fixture only\nNo real journal data", created_at=time, updated_at=time,
)
create = Change.build(record_id=episode.id, episode=episode, device_id=UUID(int=201), operation="create",
                      updated_at=time, change_id=UUID(int=301))
delete = Change.build(record_id=episode.id, episode=None, device_id=UUID(int=201), operation="delete",
                      parents=(create,), updated_at=datetime(2026, 10, 26, tzinfo=UTC), change_id=UUID(int=302))
outputs = {
    ROOT / "src/contract/desktop-v1.json": json.dumps(reference, ensure_ascii=False, indent=2) + "\n",
    ROOT / "tests/fixtures/z5-v1.json": SyncBatch((create, delete)).to_json() + "\n",
}
for path, content in outputs.items():
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"Desktop compatibility reference is stale: {path.relative_to(ROOT)}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
print("Desktop v1 references match" if args.check else "Generated disposable desktop v1 references")
