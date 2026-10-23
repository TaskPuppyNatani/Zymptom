"""Deterministic JSON, CSV, Markdown, and text renderers."""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from zymptom.domain import FunctionalEpisode
from zymptom.domain.validation import serialize_timestamp

from .summary import DurationStats, FunctionalSummary

SCHEMA_NAME = "zymptom.functional-episodes"
SCHEMA_VERSION = 1
CSV_FIELDS = (
    "id", "occurred_at", "activity", "duration_minutes", "outcome",
    "functional_demands", "symptoms", "symptom_notes", "adaptations",
    "help_level", "help_notes", "consequences", "recovery_required",
    "recovery_actions", "recovery_minutes", "resume_ability", "notes",
    "created_at", "updated_at",
)
TAG_FIELDS = (
    "functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions",
)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass(frozen=True)
class ExportRange:
    start_date: date | None
    end_date: date | None
    timezone: str

    def __post_init__(self):
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date.")
        # Fail early for invalid timezone identifiers, before a file is opened.
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("Timezone must be a valid IANA timezone identifier.") from exc


def _record(episode: FunctionalEpisode) -> dict[str, object]:
    record: dict[str, object] = {
        "id": str(episode.id),
        "occurred_at": serialize_timestamp(episode.occurred_at),
        "activity": episode.activity,
        "duration_minutes": episode.duration_minutes,
        "outcome": episode.outcome.value,
        "symptom_notes": episode.symptom_notes,
        "help_level": episode.help_level.value if episode.help_level is not None else None,
        "help_notes": episode.help_notes,
        "recovery_required": episode.recovery_required,
        "recovery_minutes": episode.recovery_minutes,
        "resume_ability": episode.resume_ability.value if episode.resume_ability is not None else None,
        "notes": episode.notes,
        "created_at": serialize_timestamp(episode.created_at),
        "updated_at": serialize_timestamp(episode.updated_at),
    }
    for field in TAG_FIELDS:
        record[field] = list(getattr(episode, field))
    return {field: record[field] for field in CSV_FIELDS}


def _ordered(episodes):
    return sorted(episodes, key=lambda episode: (episode.occurred_at, str(episode.id)))


def render_json(episodes, export_range: ExportRange) -> str:
    document = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "date_range": {
            "start": export_range.start_date.isoformat() if export_range.start_date else None,
            "end": export_range.end_date.isoformat() if export_range.end_date else None,
            "timezone": export_range.timezone,
        },
        "episodes": [_record(episode) for episode in _ordered(episodes)],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_csv(episodes) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for episode in _ordered(episodes):
        record = _record(episode)
        writer.writerow([_csv_cell(record[field]) for field in CSV_FIELDS])
    return output.getvalue()


def _date_label(value: date | None, *, fallback: str) -> str:
    if value is None:
        return fallback
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


def _range_label(export_range: ExportRange) -> str:
    start, end = export_range.start_date, export_range.end_date
    if start is not None and end is not None:
        return f"{_date_label(start, fallback='')} through {_date_label(end, fallback='')}"
    if start is not None:
        return f"from {_date_label(start, fallback='')} onward"
    if end is not None:
        return f"through {_date_label(end, fallback='')}"
    return "all recorded dates"


def _frequency_lines(title: str, values) -> list[str]:
    if not values:
        return [f"- {title}: none recorded"]
    return [f"- {title} (episodes containing each value):"] + [
        f"  - {_code(value.value)}: {value.episodes}" for value in values
    ]


def _duration_line(title: str, stats: DurationStats) -> str:
    if stats.recorded_count == 0 and stats.missing_count == 0:
        return f"- {title}: no attempts"
    if stats.recorded_count == 0:
        return f"- {title}: not recorded for {stats.missing_count} attempts"
    range_text = f"; range {stats.minimum_minutes}–{stats.maximum_minutes} minutes"
    return (
        f"- {title}: {stats.recorded_count} recorded, {stats.missing_count} not recorded; "
        f"total {stats.total_minutes} minutes{range_text}"
    )


def render_summary(summary: FunctionalSummary, export_range: ExportRange) -> str:
    if summary.total_attempts == 0:
        return f"No functional attempts were recorded for {_range_label(export_range)}."
    groups = summary.outcome_groups
    narrative = (
        f"For {_range_label(export_range)}, {summary.total_attempts} functional attempts were recorded "
        f"({groups['completed']} completed, {groups['stopped_early']} stopped early, "
        f"{groups['unable_to_complete']} unable to complete, "
        f"{groups['avoided_due_to_limitation']} avoided due to limitation)."
    )
    recovery = summary.recovery_required_counts
    if recovery["yes"] or recovery["no"]:
        narrative += (
            f" Recovery was recorded as required after {recovery['yes']} attempts; "
            f"not required after {recovery['no']}; status not recorded for {recovery['not_recorded']}."
        )
    return narrative


def _summary_section(summary: FunctionalSummary, export_range: ExportRange) -> list[str]:
    lines = ["## Deterministic summary", "", render_summary(summary, export_range), ""]
    lines.append("### Outcomes")
    lines.extend(f"- {_code(outcome)}: {count}" for outcome, count in summary.outcome_counts.items())
    lines.extend([
        "",
        "### Recorded details",
        f"- Help levels: {', '.join(f'{_code(key)} {value}' for key, value in summary.help_level_counts.items())}",
        f"- Episodes with help recorded by outcome or level: {summary.episodes_with_help}",
        f"- Recovery required: yes {summary.recovery_required_counts['yes']}, "
        f"no {summary.recovery_required_counts['no']}, "
        f"not recorded {summary.recovery_required_counts['not_recorded']}",
        f"- Resume ability: {', '.join(f'{_code(key)} {value}' for key, value in summary.resume_ability_counts.items())}",
        _duration_line("Activity duration", summary.duration),
        _duration_line("Recovery duration", summary.recovery_duration),
        "",
        *_frequency_lines("Functional demands", summary.functional_demands),
        *_frequency_lines("Symptoms", summary.symptoms),
        *_frequency_lines("Adaptations", summary.adaptations),
        *_frequency_lines("Activity frequencies (exact stored text)", summary.activities),
    ])
    return lines


def render_summary_details(summary: FunctionalSummary, export_range: ExportRange) -> str:
    return "\n".join(_summary_section(summary, export_range))


def _inline(value: str) -> str:
    for char in "\\`*_{}[]<>()#+-.!|":
        value = value.replace(char, "\\" + char)
    return value.replace("\n", " ").replace("\r", " ")


def _code(value: str) -> str:
    longest = max(_backtick_runs(value), default=0)
    fence = "`" * max(1, longest + 1)
    return f"{fence}{value}{fence}"


def _fenced(label: str, value: str) -> list[str]:
    longest = max(_backtick_runs(value), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"- {label}:", "", fence, value, fence]


def _backtick_runs(value: str):
    current = 0
    for char in value:
        if char == "`":
            current += 1
        elif current:
            yield current
            current = 0
    if current:
        yield current


def _display(value, *, missing="Not recorded") -> str:
    if value is None:
        return missing
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _entry_lines(episode: FunctionalEpisode, zone: ZoneInfo) -> list[str]:
    local = episode.occurred_at.astimezone(zone)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    title = f"{hour}:{local.minute:02d} {meridiem} — {_inline(episode.activity)}"
    lines = [f"#### {title}", f"- ID: `{episode.id}`", f"- Timestamp (UTC): `{serialize_timestamp(episode.occurred_at)}`"]
    lines.extend(_fenced("Activity (exact stored text)", episode.activity))
    for label, value in (
        ("Activity duration (minutes)", episode.duration_minutes),
        ("Outcome", episode.outcome),
        ("Functional demands", episode.functional_demands),
        ("Symptoms", episode.symptoms),
        ("Adaptations", episode.adaptations),
        ("Help level", episode.help_level),
        ("Consequences", episode.consequences),
        ("Recovery required", episode.recovery_required),
        ("Recovery actions", episode.recovery_actions),
        ("Recovery duration (minutes)", episode.recovery_minutes),
        ("Resume ability", episode.resume_ability),
    ):
        if isinstance(value, list):
            lines.extend(_fenced(label, json.dumps(value, ensure_ascii=False, separators=(",", ":"))))
        else:
            lines.append(f"- {label}: {_code(_display(value))}")
    lines.extend(_fenced("Symptom notes", episode.symptom_notes) if episode.symptom_notes is not None else ["- Symptom notes: Not recorded"])
    lines.extend(_fenced("Help notes", episode.help_notes) if episode.help_notes is not None else ["- Help notes: Not recorded"])
    lines.extend(_fenced("General notes", episode.notes) if episode.notes is not None else ["- General notes: Not recorded"])
    lines.extend([
        f"- Created (UTC): `{serialize_timestamp(episode.created_at)}`",
        f"- Updated (UTC): `{serialize_timestamp(episode.updated_at)}`",
    ])
    return lines


def render_markdown(episodes, summary: FunctionalSummary, export_range: ExportRange) -> str:
    zone = ZoneInfo(export_range.timezone)
    lines = [
        "# Zymptom Functional Episode Report", "",
        f"Date range: {_range_label(export_range)}", f"Timezone: `{export_range.timezone}`", "",
        *_summary_section(summary, export_range), "", "## Episodes", "",
    ]
    previous_date = None
    ordered = _ordered(episodes)
    if not ordered:
        lines.append("No episodes matched this date range.")
    for episode in ordered:
        local = episode.occurred_at.astimezone(zone)
        if local.date() != previous_date:
            if previous_date is not None:
                lines.append("")
            lines.append(f"### {_date_label(local.date(), fallback='')}")
            lines.append("")
            previous_date = local.date()
        lines.extend(_entry_lines(episode, zone))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
