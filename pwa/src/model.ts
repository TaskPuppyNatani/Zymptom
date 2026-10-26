import desktop from './contract/desktop-v1.json';

export const contract = desktop;
export type Outcome = keyof typeof desktop.outcomes;
export type HelpLevel = keyof typeof desktop.help_levels;
export type ResumeAbility = keyof typeof desktop.resume_abilities;
export const tagFields = ['functional_demands', 'symptoms', 'adaptations', 'consequences', 'recovery_actions'] as const;
export type TagField = typeof tagFields[number];

// Exactly the FunctionalEpisode snapshot fields; local storage metadata is separate.
export interface Episode {
  id: string;
  occurred_at: string;
  activity: string;
  outcome: Outcome;
  duration_minutes: number | null;
  functional_demands: string[];
  symptoms: string[];
  symptom_notes: string | null;
  adaptations: string[];
  help_level: HelpLevel | null;
  help_notes: string | null;
  consequences: string[];
  recovery_required: boolean | null;
  recovery_actions: string[];
  recovery_minutes: number | null;
  resume_ability: ResumeAbility | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}
export type EpisodeInput = Omit<Episode, 'id' | 'created_at' | 'updated_at'>;
export const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function utc(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) throw new Error('Timestamps must include a UTC offset.');
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const days = new Date(Date.UTC(year < 100 ? year + 400 : year, month, 0)).getUTCDate();
  const parsed = new Date(value);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days || hour > 23 || minute > 59 || second > 59 || !Number.isFinite(parsed.getTime())) {
    throw new Error('Invalid timestamp.');
  }
  const iso = parsed.toISOString();
  if (iso.length !== 24 || iso.startsWith('0000')) throw new Error('Timestamp is outside the supported calendar.');
  return iso.slice(0, 19) + '.' + (match[7] ?? '').padEnd(6, '0') + '+00:00';
}
export function nowUtc(): string { return utc(new Date().toISOString()); }
export function unique(values: string[]): string[] { return [...new Set(values)]; }
export function addSelected(current: string[], selected: string[]): string[] {
  return unique([...current, ...selected]);
}

export function validateEpisode(value: unknown): asserts value is Episode {
  if (!value || typeof value !== 'object') throw new Error('Invalid episode.');
  const episode = value as Episode;
  const keys = ['id', 'occurred_at', 'activity', 'outcome', 'duration_minutes', ...tagFields,
    'symptom_notes', 'help_level', 'help_notes', 'recovery_required', 'recovery_minutes', 'resume_ability',
    'notes', 'created_at', 'updated_at'].sort();
  if (JSON.stringify(Object.keys(episode).sort()) !== JSON.stringify(keys)) throw new Error('Episode fields do not match desktop v1.');
  if (typeof episode.id !== 'string' || !uuidPattern.test(episode.id)) throw new Error('Invalid episode UUID.');
  if (typeof episode.activity !== 'string' || !episode.activity.trim()) throw new Error('Describe what happened.');
  if (!Object.hasOwn(desktop.outcomes, episode.outcome)) throw new Error('Choose an outcome.');
  if (episode.help_level !== null && !Object.hasOwn(desktop.help_levels, episode.help_level)) throw new Error('Invalid help level.');
  if (episode.resume_ability !== null && !Object.hasOwn(desktop.resume_abilities, episode.resume_ability)) throw new Error('Invalid resume ability.');
  for (const field of ['occurred_at', 'created_at', 'updated_at'] as const) {
    if (typeof episode[field] !== 'string') throw new Error('Invalid timestamp.');
    utc(episode[field]);
  }
  for (const field of ['duration_minutes', 'recovery_minutes'] as const) {
    const number = episode[field];
    if (number !== null && (!Number.isSafeInteger(number) || number < 0)) throw new Error('Minutes must be a whole number of zero or more.');
  }
  if (episode.recovery_required !== null && typeof episode.recovery_required !== 'boolean') throw new Error('Invalid recovery state.');
  for (const field of ['notes', 'symptom_notes', 'help_notes'] as const) {
    if (episode[field] !== null && typeof episode[field] !== 'string') throw new Error('Invalid notes.');
  }
  for (const field of tagFields) {
    if (!Array.isArray(episode[field]) || episode[field].some((tag) => typeof tag !== 'string' || !tag.trim())) throw new Error('Invalid list values.');
  }
}

export function blankEpisode(): EpisodeInput {
  return {
    occurred_at: nowUtc(), activity: '', outcome: '' as Outcome, duration_minutes: null,
    functional_demands: [], symptoms: [], symptom_notes: null, adaptations: [], help_level: null,
    help_notes: null, consequences: [], recovery_required: null, recovery_actions: [],
    recovery_minutes: null, resume_ability: null, notes: null,
  };
}

export function localDate(value: string | Date): string {
  const date = typeof value === 'string' ? new Date(value) : value;
  return [date.getFullYear().toString().padStart(4, '0'), (date.getMonth() + 1).toString().padStart(2, '0'), date.getDate().toString().padStart(2, '0')].join('-');
}
export function localInput(value: string): string {
  const date = new Date(value);
  return `${localDate(date)}T${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`;
}
export function occurredFromInput(value: string, original: string): string {
  // Preserve seconds, microseconds, and an ambiguous DST fold when unchanged.
  if (value === localInput(original)) return original;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime()) || localInput(date.toISOString()) !== value) throw new Error('Choose a valid local date and time.');
  return utc(date.toISOString());
}
