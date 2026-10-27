import { describe, expect, it } from 'vitest';
import fixture from '../fixtures/z5-v1.json';
import { candidateSummary, conflictExplanation, syncFailure, syncLabel, syncMessage, type SyncViewState } from '../../src/sandbox/presentation';
import type { Z5Change } from '../../src/sandbox/z5-wire';

const base: SyncViewState = { connected: false, busy: false, successful: false, conflicts: 0 };
describe('sync presentation', () => {
  it.each([
    [base, 'Not connected'], [{ ...base, connected: true }, 'Connected'],
    [{ ...base, busy: true, connecting: true }, 'Connecting…'],
    [{ ...base, busy: true, operation: 'disconnect' }, 'Disconnecting…'],
    [{ ...base, busy: true, operation: 'resolving' }, 'Saving…'],
    [{ ...base, connected: true, busy: true }, 'Syncing…'],
    [{ ...base, connected: true, successful: true }, 'Up to date'],
    [{ ...base, failure: 'offline' }, 'Offline'], [{ ...base, failure: 'auth' }, 'Not connected'],
    [{ ...base, conflicts: 1 }, 'Needs attention'], [{ ...base, failure: 'integrity' }, 'Needs attention'],
  ] as const)('renders the shared status', (state, label) => expect(syncLabel(state)).toBe(label));

  it('maps failure codes without displaying secret values or raw messages', () => {
    expect(syncFailure({ code: 'auth/user-token-expired', message: 'secret' })).toBe('auth');
    expect(syncFailure({ code: 'auth/popup-closed-by-user' })).toBe('cancelled');
    expect(syncFailure({ code: 'unavailable', message: 'secret' })).toBe('offline');
    expect(syncFailure({ code: 'permission-denied', message: 'secret' })).toBe('permission');
    expect(syncFailure({ name: 'Z5PacketError', message: 'secret' })).toBe('integrity');
    expect(syncMessage({ ...base, failure: syncFailure({ message: 'secret' }) })).not.toContain('secret');
    expect(syncMessage({ ...base, conflicts: 1, failure: 'offline' })).toContain('offline');
  });

  it('summarizes complete versions without IDs, raw JSON or inferred recovery', () => {
    const change = fixture.changes[0] as Z5Change;
    const text = candidateSummary(change);
    expect(text).toContain('DEMO: Café');
    expect(text).toContain('Sensory overload');
    expect(text).toContain('0 min');
    expect(text).toContain('Recovery required: No');
    expect(text).not.toContain(change.change_id);
    expect(text).not.toContain('"activity"');
    expect(candidateSummary(fixture.changes[1] as Z5Change)).toContain('deleted the entry');
    expect(conflictExplanation(fixture.changes as Z5Change[])).toContain('deleted on one device but changed');
    expect(conflictExplanation([fixture.changes[1] as Z5Change])).toContain('deleted on all devices');
  });
});
