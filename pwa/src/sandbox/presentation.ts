import { contract, type Episode, type TagField } from '../model';
import type { Z5Change } from './z5-wire';

export type SyncFailure = 'offline' | 'auth' | 'permission' | 'integrity' | 'signin' | 'cancelled' | 'other';
export interface SyncViewState { connected: boolean; busy: boolean; connecting?: boolean; operation?: 'disconnect' | 'resolving'; successful: boolean; conflicts: number; failure?: SyncFailure; }

export function syncLabel(state: SyncViewState): string {
  if (state.connecting) return 'Connecting…';
  if (state.operation === 'disconnect') return 'Disconnecting…';
  if (state.operation === 'resolving') return 'Saving…';
  if (state.busy) return 'Syncing…';
  if (state.conflicts) return 'Needs attention';
  if (state.failure === 'offline') return 'Offline';
  if (state.failure === 'auth' || state.failure === 'cancelled') return 'Not connected';
  if (state.failure) return 'Needs attention';
  if (!state.connected) return 'Not connected';
  return state.successful ? 'Up to date' : 'Connected';
}

export function syncMessage(state: SyncViewState): string {
  const messages: Record<SyncFailure, string> = {
    offline: 'Zymptom is offline. Your entries are safe on this device and can sync later.',
    auth: 'Your connection expired. Sign in again to continue syncing.',
    permission: 'Zymptom could not access your sync data.',
    integrity: 'Sync needs attention. Your local entries remain available.',
    signin: 'Could not connect. Check your connection and try again.',
    cancelled: 'Sign-in was cancelled.',
    other: 'Sync needs attention. Your local entries remain available.',
  };
  if (state.conflicts && !state.busy) {
    const detail = state.failure ? ` ${messages[state.failure]}` : '';
    return `An entry changed differently on two devices. Review the versions to continue.${detail}`;
  }
  return state.failure ? messages[state.failure] : '';
}

export function syncFailure(error: unknown): SyncFailure {
  let code: unknown;
  let name: unknown;
  try {
    if (error && (typeof error === 'object' || typeof error === 'function')) {
      code = Reflect.get(error, 'code'); name = Reflect.get(error, 'name');
    }
  } catch { return 'other'; }
  if (typeof code === 'string') {
    if (['auth/popup-closed-by-user', 'auth/cancelled-popup-request'].includes(code)) return 'cancelled';
    if (['auth/user-token-expired', 'auth/invalid-user-token', 'unauthenticated', 'firestore/unauthenticated'].includes(code)) return 'auth';
    if (['auth/network-request-failed', 'unavailable', 'firestore/unavailable', 'deadline-exceeded'].includes(code)) return 'offline';
    if (['permission-denied', 'firestore/permission-denied'].includes(code)) return 'permission';
    if (code.startsWith('auth/')) return 'signin';
  }
  if (name === 'Z5PacketError' || name === 'SandboxIdentityCollisionError') return 'integrity';
  return 'other';
}

function tags(episode: Episode, field: TagField): string {
  const labels: Record<string, string> = contract.tags[field];
  return episode[field].map((value) => labels[value] ?? value.replaceAll('_', ' ')).join(', ') || 'None recorded';
}

export function candidateSummary(change: Z5Change): string {
  const e = change.episode;
  if (!e) return 'This version deleted the entry.';
  const recovery = e.recovery_required === null ? 'Not recorded' : e.recovery_required ? 'Yes' : 'No';
  return [
    `Activity: ${e.activity}`, `When: ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(e.occurred_at))}`,
    `Duration: ${e.duration_minutes === null ? 'Not recorded' : `${e.duration_minutes} min`}`,
    `Outcome: ${contract.outcomes[e.outcome]}`, `Functional demands: ${tags(e, 'functional_demands')}`,
    `Symptoms: ${tags(e, 'symptoms')}`, `Symptom notes: ${e.symptom_notes ?? 'Not recorded'}`,
    `Adaptations: ${tags(e, 'adaptations')}`, `Help: ${e.help_level === null ? 'Not recorded' : contract.help_levels[e.help_level]}`,
    `Help notes: ${e.help_notes ?? 'Not recorded'}`, `Consequences: ${tags(e, 'consequences')}`,
    `Recovery required: ${recovery}`, `Recovery actions: ${tags(e, 'recovery_actions')}`,
    `Recovery time: ${e.recovery_minutes === null ? 'Not recorded' : `${e.recovery_minutes} min`}`,
    `Able to resume: ${e.resume_ability === null ? 'Not recorded' : contract.resume_abilities[e.resume_ability]}`,
    `Notes: ${e.notes ?? 'Not recorded'}`,
  ].join('\n');
}

export function conflictExplanation(candidates: Z5Change[]): string {
  if (candidates.every((candidate) => candidate.deleted)) return 'This entry was deleted on all devices. Choose a version to confirm the deletion.';
  if (candidates.some((candidate) => candidate.deleted)) return 'This entry was deleted on one device but changed on another.';
  return 'This entry changed differently on two devices. Choose the version you want to keep.';
}
