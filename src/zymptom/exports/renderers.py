"""Deterministic JSON, CSV, Markdown, and text renderers."""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from zymptom.domain import FunctionalEpisode
from zymptom.domain.validation import serialize_timestamp
from zymptom.ui.labels import (
    ADAPTATIONS, CONSEQUENCES, FUNCTIONAL_DEMANDS, HELP_LEVELS, OUTCOMES,
    RECOVERY_ACTIONS, RESUME_ABILITIES, SYMPTOMS,
)

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
_OUTCOME_LABELS = {getattr(key, "value", key): label for key, label in OUTCOMES.items()}
_HELP_LABELS = {getattr(key, "value", key): label for key, label in HELP_LEVELS.items()}
_RESUME_LABELS = {getattr(key, "value", key): label for key, label in RESUME_ABILITIES.items()}
_DEMAND_LABELS = {getattr(key, "value", key): label for key, label in FUNCTIONAL_DEMANDS.items()}
_SYMPTOM_LABELS = {getattr(key, "value", key): label for key, label in SYMPTOMS.items()}
_ADAPTATION_LABELS = {getattr(key, "value", key): label for key, label in ADAPTATIONS.items()}
_CONSEQUENCE_LABELS = {getattr(key, "value", key): label for key, label in CONSEQUENCES.items()}
_RECOVERY_ACTION_LABELS = {
    getattr(key, "value", key): label for key, label in RECOVERY_ACTIONS.items()
}
_RECOVERY_REQUIRED_LABELS = {
    "yes": "Required", "no": "Not required", "not_recorded": "Not recorded",
}
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


def _frequency_lines(title: str, values, labels=None, *, exact_text: bool = False) -> list[str]:
    lines = [f"### {title}", ""]
    if not values:
        return [*lines, "None recorded", ""]
    lines.extend(
        f"- {_inline(value.value if exact_text else _label(value.value, labels))}: {value.episodes}"
        for value in values
    )
    lines.append("")
    return lines


def _quantity(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def _duration_line(title: str, stats: DurationStats) -> str:
    if stats.recorded_count == 0 and stats.missing_count == 0:
        return f"- {title}: no attempts"
    if stats.recorded_count == 0:
        return f"- {title}: not recorded for {_quantity(stats.missing_count, 'attempt')}"
    range_text = f"; range {stats.minimum_minutes}–{stats.maximum_minutes} minutes"
    return (
        f"- {title}: recorded for {_quantity(stats.recorded_count, 'attempt')}; "
        f"not recorded for {_quantity(stats.missing_count, 'attempt')}; "
        f"total {stats.total_minutes} minutes{range_text}"
    )


def render_summary(summary: FunctionalSummary, export_range: ExportRange) -> str:
    if summary.total_attempts == 0:
        return f"No functional attempts were recorded for {_range_label(export_range)}."
    groups = summary.outcome_groups
    attempt = "attempt" if summary.total_attempts == 1 else "attempts"
    verb = "was" if summary.total_attempts == 1 else "were"
    narrative = (
        f"For {_range_label(export_range)}, {summary.total_attempts} functional {attempt} {verb} recorded "
        f"({groups['completed']} completed, {groups['stopped_early']} stopped early, "
        f"{groups['unable_to_complete']} unable to complete, "
        f"{groups['avoided_due_to_limitation']} avoided due to limitation)."
    )
    recovery = summary.recovery_required_counts
    if recovery["yes"] or recovery["no"]:
        narrative += (
            f" Recovery was recorded as required after {_quantity(recovery['yes'], 'attempt')}; "
            f"not required after {_quantity(recovery['no'], 'attempt')}; "
            f"status not recorded for {_quantity(recovery['not_recorded'], 'attempt')}."
        )
    return narrative


def _summary_section(summary: FunctionalSummary, export_range: ExportRange) -> list[str]:
    lines = ["## Deterministic summary", "", render_summary(summary, export_range), ""]
    lines.extend(["### Outcomes", ""])
    lines.extend(
        f"- {_label(outcome, _OUTCOME_LABELS)}: {count}"
        for outcome, count in summary.outcome_counts.items()
    )
    lines.extend([
        "",
        "### Help levels",
        "",
        *(f"- {_label(key, _HELP_LABELS)}: {value}" for key, value in summary.help_level_counts.items()),
        f"- Episodes with help recorded by outcome or level: {summary.episodes_with_help}",
        "",
        "### Recovery required",
        "",
        *(
            f"- {_RECOVERY_REQUIRED_LABELS[key]}: {value}"
            for key, value in summary.recovery_required_counts.items()
        ),
        "",
        "### Resume ability",
        "",
        *(f"- {_label(key, _RESUME_LABELS)}: {value}" for key, value in summary.resume_ability_counts.items()),
        "",
        "### Durations",
        "",
        _duration_line("Activity duration", summary.duration),
        _duration_line("Recovery duration", summary.recovery_duration),
        "",
    ])
    lines.extend(_frequency_lines("Functional demands", summary.functional_demands, _DEMAND_LABELS))
    lines.extend(_frequency_lines("Symptoms", summary.symptoms, _SYMPTOM_LABELS))
    lines.extend(_frequency_lines("Adaptations", summary.adaptations, _ADAPTATION_LABELS))
    lines.extend(_frequency_lines("Activity frequencies", summary.activities, exact_text=True))
    return lines


def render_summary_details(summary: FunctionalSummary, export_range: ExportRange) -> str:
    return "\n".join(_summary_section(summary, export_range))


def _inline(value: str) -> str:
    for char in "\\`*_{}[]<>()#+-.!|":
        value = value.replace(char, "\\" + char)
    return value.replace("\n", " ").replace("\r", " ")


def _label(value, labels=None) -> str:
    if value is None:
        return "Not recorded"
    raw = value.value if hasattr(value, "value") else str(value)
    if labels is not None and raw in labels:
        return labels[raw]
    if raw == "not_recorded":
        return "Not recorded"
    if "_" in raw:
        return raw.replace("_", " ").title()
    return raw[:1].upper() + raw[1:]


def _value_field(label: str, value, labels=None, *, unit: str | None = None) -> list[str]:
    if value is None:
        display = "Not recorded"
    elif isinstance(value, bool):
        display = "Yes" if value else "No"
    elif labels is not None or hasattr(value, "value"):
        display = _label(value, labels)
    else:
        display = str(value)
    if unit is not None and value is not None:
        display = f"{display} {unit}"
    return [f"**{label}:** {display}"]


def _list_field(label: str, values: list[str], labels=None) -> list[str]:
    if not values:
        return [f"**{label}:** None recorded"]
    return [f"**{label}:**", *(f"- {_inline(_label(value, labels))}" for value in values)]


def _text_field(label: str, value: str | None) -> list[str]:
    if value is None:
        return [f"**{label}:** Not recorded"]
    if value == "":
        return [f"**{label}:** (empty)"]
    lines = value.split("\n")
    rendered = [f"**{label}:** {_inline(lines[0])}"]
    for index, line in enumerate(lines[1:], start=1):
        rendered.append(_inline(line))
        if index < len(lines) - 1:
            rendered[-1] += "  "
    if len(lines) > 1:
        rendered[0] += "  "
    return rendered


def _entry_lines(episode: FunctionalEpisode, zone: ZoneInfo) -> list[str]:
    local = episode.occurred_at.astimezone(zone)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    timezone_abbreviation = local.tzname()
    local_time = f"{hour}:{local.minute:02d} {meridiem}"
    if timezone_abbreviation:
        local_time += f" {timezone_abbreviation}"
    recovery_label = _RECOVERY_REQUIRED_LABELS[
        "not_recorded" if episode.recovery_required is None else "yes" if episode.recovery_required else "no"
    ]
    sections = [
        [f"#### {local_time}"],
        _text_field("Activity", episode.activity),
        _value_field("Outcome", episode.outcome, _OUTCOME_LABELS),
        _value_field("Duration", episode.duration_minutes, unit="minutes"),
        _list_field("Functional demands", episode.functional_demands, _DEMAND_LABELS),
        _list_field("Symptoms", episode.symptoms, _SYMPTOM_LABELS),
        _text_field("Symptom notes", episode.symptom_notes),
        _list_field("Adaptations", episode.adaptations, _ADAPTATION_LABELS),
        _value_field("Help", episode.help_level, _HELP_LABELS),
        _text_field("Help notes", episode.help_notes),
        _list_field("Consequences", episode.consequences, _CONSEQUENCE_LABELS),
        [f"**Recovery required:** {recovery_label}"],
        _list_field("Recovery actions", episode.recovery_actions, _RECOVERY_ACTION_LABELS),
        _value_field("Recovery duration", episode.recovery_minutes, unit="minutes"),
        _value_field("Resume ability", episode.resume_ability, _RESUME_LABELS),
        _text_field("Notes", episode.notes),
    ]
    lines = []
    for section in sections:
        if lines:
            lines.append("")
        lines.extend(section)
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
