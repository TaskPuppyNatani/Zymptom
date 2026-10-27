import './style.css';
import { blankEpisode, contract, localDate, localInput, occurredFromInput, tagFields,
  type Episode, type EpisodeInput, type HelpLevel, type Outcome, type ResumeAbility } from './model';
import { EpisodeStore, type LocalRecord } from './storage';
import { TagPicker } from './tags';
import { SandboxSyncPanel } from './sandbox/panel';

const app = document.querySelector<HTMLDivElement>('#app')!;
for (const [name, value] of Object.entries(contract.palette)) {
  document.documentElement.style.setProperty(`--${name.toLowerCase().replaceAll('_', '-')}`, value);
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text: string, action: () => void, className = ''): HTMLButtonElement {
  const node = el('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
let fieldId = 0;
function label(text: string, control: HTMLElement): HTMLDivElement {
  const node = el('div', undefined, 'field');
  const caption = el('label', text);
  control.id = `field-${++fieldId}`;
  caption.htmlFor = control.id;
  node.append(caption, control);
  return node;
}
function select(options: Record<string, string>, value: string | null, placeholder = 'Not recorded'): HTMLSelectElement {
  const control = el('select');
  for (const [key, text] of [['', placeholder], ...Object.entries(options)]) {
    const option = el('option', text);
    option.value = key;
    control.append(option);
  }
  control.value = value ?? '';
  return control;
}
function minutes(value: number | null): HTMLInputElement {
  const control = el('input');
  control.type = 'number';
  control.inputMode = 'numeric';
  control.min = '0';
  control.max = String(Number.MAX_SAFE_INTEGER);
  control.step = '1';
  control.placeholder = 'Not recorded';
  control.value = value === null ? '' : String(value);
  return control;
}
function notes(value: string | null, rows = 3): HTMLTextAreaElement {
  const control = el('textarea');
  control.value = value ?? '';
  control.rows = rows;
  return control;
}
function optionalText(control: HTMLTextAreaElement, original: string | null): string | null {
  return control.value === '' && original !== '' ? null : control.value;
}

const header = el('header', undefined, 'app-header');
const wordmark = el('div');
wordmark.append(el('p', 'Zymptom', 'wordmark'), el('p', 'LOCAL JOURNAL · SANDBOX TRANSPORT', 'eyebrow'));
header.append(wordmark);
const main = el('main');
const notice = el('p', '', 'notice');
notice.setAttribute('role', 'status');
const footer = el('footer');
const logo = el('img');
logo.src = '/icons/icon-192.png';
logo.alt = '';
logo.width = 48;
logo.height = 48;
footer.append(logo, el('span', 'TaskPuppy Kreations'));
const about = el('details', undefined, 'proof-info');
const shellStatus = el('p', 'Offline use is available after the production shell is ready.');
shellStatus.id = 'shell-status';
shellStatus.setAttribute('role', 'status');
about.append(el('summary', 'About this mobile proof'),
  el('p', 'Use disposable test entries only. Sandbox Sync can manually share these entries between PWA installations. The desktop journal is not connected.'),
  shellStatus,
  el('p', 'To install: Android browser menu → Install app. On iPhone: Share → Add to Home Screen. Installation needs HTTPS (or localhost on this computer).'));
const sandboxPanel = new SandboxSyncPanel();
app.replaceChildren(header, notice, main, about, sandboxPanel.element, footer);

let store: EpisodeStore;
let mode: 'today' | 'editor' = 'today';
let todayDate = '';
let renderRequest = 0;
let busy = false;

async function today(message = ''): Promise<void> {
  mode = 'today';
  const request = ++renderRequest;
  notice.textContent = message;
  try {
    const records = await store.list();
    if (mode !== 'today' || request !== renderRequest) return;
    todayDate = localDate(new Date());
    const entries = records.filter((record) => localDate(record.episode!.occurred_at) === todayDate);
    const heading = el('div', undefined, 'today-heading');
    const title = el('div');
    title.append(el('h1', 'Today'), el('p', new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' }).format(new Date()), 'date-header'));
    heading.append(title);
    const log = button('＋ Log Something', () => editor(), 'primary log-button');
    const count = el('p', `${entries.length} ${entries.length === 1 ? 'entry' : 'entries'}`, 'section-label');
    const list = el('div', undefined, 'episode-list');
    if (!entries.length) {
      const empty = el('section', undefined, 'empty-state');
      empty.append(el('div', '✦', 'empty-mark'), el('h2', 'Every attempt counts.'),
        el('p', 'Record something you did, found difficult, or couldn’t finish. A few words are enough.'));
      list.append(empty);
    }
    for (const record of entries) {
      const episode = record.episode!;
      const card = button('', () => editor(record), 'episode-card');
      card.setAttribute('aria-label', `Edit ${episode.activity}`);
      const meta = el('div', undefined, 'card-meta');
      const time = el('time', new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(episode.occurred_at)));
      time.dateTime = episode.occurred_at;
      meta.append(time, el('span', episode.duration_minutes === null ? 'Edit entry ↗' : `${episode.duration_minutes} min`));
      card.append(meta, el('h2', episode.activity), el('p', contract.outcomes[episode.outcome], 'outcome'));
      list.append(card);
    }
    main.replaceChildren(heading, log, count, list);
    if (message) main.querySelector('h1')?.setAttribute('tabindex', '-1');
    if (message) main.querySelector<HTMLElement>('h1')?.focus();
  } catch (error) {
    notice.textContent = error instanceof Error ? error.message : 'Local records could not be read.';
    main.replaceChildren(button('Try again', () => { void today(); }));
  }
}

function editor(original?: LocalRecord): void {
  mode = 'editor';
  ++renderRequest;
  notice.textContent = '';
  const source = original?.episode ?? blankEpisode();
  const draft = structuredClone(source);
  const form = el('form');
  form.className = 'editor';
  const error = el('p', '', 'error-message');
  error.setAttribute('role', 'alert');
  error.tabIndex = -1;
  const heading = el('div', undefined, 'editor-heading');
  heading.append(button('← Today', () => { if (!busy) void today(); }, 'text-button'), el('h1', original ? 'Edit entry' : 'Log Something'));
  const activity = notes(source.activity, 2);
  activity.required = true;
  activity.placeholder = 'For example, making lunch or going for a walk';
  activity.autocomplete = 'on';
  const when = el('input');
  when.type = 'datetime-local';
  when.required = true;
  when.value = localInput(source.occurred_at);
  const duration = minutes(source.duration_minutes);
  const outcome = select(contract.outcomes, source.outcome, 'Choose an outcome');
  outcome.required = true;
  const note = notes(source.notes);
  note.placeholder = 'Anything you want to remember';
  const timing = el('div', undefined, 'two-fields');
  timing.append(label('When? · local time', when), label('Duration · minutes', duration));
  form.append(label('What happened?', activity), timing, label('Outcome', outcome), label('Notes', note));

  const optional = el('details', undefined, 'more-details');
  optional.append(el('summary', 'More details'));
  const pickers = new Map<string, TagPicker>();
  for (const field of tagFields) {
    const picker = new TagPicker(field, contract.tags[field], source[field]);
    pickers.set(field, picker);
    optional.append(picker.element);
  }
  const symptomNotes = notes(source.symptom_notes);
  const help = select(contract.help_levels, source.help_level);
  const helpNotes = notes(source.help_notes);
  const recovery = select({ yes: 'Yes', no: 'No' }, source.recovery_required === null ? null : source.recovery_required ? 'yes' : 'no');
  const recoveryMinutes = minutes(source.recovery_minutes);
  const resume = select(contract.resume_abilities, source.resume_ability);
  optional.append(label('Symptom notes', symptomNotes), label('Help', help), label('Help notes', helpNotes),
    label('Recovery required?', recovery), label('Recovery · minutes', recoveryMinutes), label('Able to resume?', resume));
  form.append(optional, error);

  const actions = el('div', undefined, 'form-actions');
  const cancel = button('Cancel', () => { if (!busy) void today(); });
  const save = el('button', 'Save entry', 'primary');
  save.type = 'submit';
  actions.append(cancel, save);
  form.append(actions);
  if (original) {
    const danger = el('div', undefined, 'danger-zone');
    danger.append(button('Delete entry…', () => confirmDelete(original, error), 'danger'));
    form.append(danger);
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (busy) return;
    void (async () => {
      busy = true;
      save.disabled = true;
      error.textContent = '';
      try {
        draft.activity = activity.value;
        draft.occurred_at = occurredFromInput(when.value, source.occurred_at);
        draft.duration_minutes = duration.value === '' ? null : Number(duration.value);
        draft.outcome = outcome.value as Outcome;
        draft.notes = optionalText(note, source.notes);
        draft.symptom_notes = optionalText(symptomNotes, source.symptom_notes);
        draft.help_notes = optionalText(helpNotes, source.help_notes);
        draft.help_level = (help.value || null) as HelpLevel | null;
        draft.recovery_required = recovery.value === '' ? null : recovery.value === 'yes';
        draft.recovery_minutes = recoveryMinutes.value === '' ? null : Number(recoveryMinutes.value);
        draft.resume_ability = (resume.value || null) as ResumeAbility | null;
        for (const field of tagFields) draft[field] = [...pickers.get(field)!.values];
        if (original) await store.update(draft as Episode, original.local_revision, original.local_change_id);
        else await store.create(draft as EpisodeInput);
        await today('Entry saved on this browser.');
      } catch (failure) {
        error.textContent = failure instanceof Error ? failure.message : 'The entry could not be saved. Please retry.';
        error.focus();
      } finally { busy = false; save.disabled = false; }
    })();
  });
  main.replaceChildren(heading, el('p', 'Record an attempt, even if you stopped or couldn’t start.', 'editor-intro'), form);
  window.scrollTo({ top: 0 });
  // No autofocus: let the phone keyboard open only when the person chooses a field.
}

function confirmDelete(original: LocalRecord, error: HTMLElement): void {
  if (busy) return;
  const dialog = el('dialog', undefined, 'delete-dialog');
  const heading = el('h2', 'Delete this entry?');
  heading.id = 'delete-heading';
  dialog.setAttribute('aria-labelledby', heading.id);
  const episode = original.episode!;
  dialog.append(heading, el('p', episode.activity, 'delete-context'),
    el('p', new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'short' }).format(new Date(episode.occurred_at))),
    el('p', 'This removes the entry from Today. Manual sync shares the deletion with other proof installations.'));
  const actions = el('div', undefined, 'dialog-actions');
  const cancel = button('Keep entry', () => dialog.close());
  cancel.autofocus = true;
  const remove = button('Delete entry', () => {
    if (busy) return;
    busy = true;
    remove.disabled = true;
    cancel.disabled = true;
    void store.delete(original.record_id, original.local_revision, original.local_change_id).then(async () => {
      dialog.close();
      await today('Entry deleted from this browser.');
    }).catch((failure: unknown) => {
      dialog.close();
      error.textContent = failure instanceof Error ? failure.message : 'Deletion failed. The entry is unchanged.';
      error.focus();
    }).finally(() => { busy = false; });
  }, 'danger');
  actions.append(cancel, remove);
  dialog.append(actions);
  dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
}

async function offlineShell(): Promise<void> {
  if (import.meta.env.DEV) {
    shellStatus.textContent = 'Development mode. Use a production preview to test installation and offline reopening.';
    return;
  }
  if (!('serviceWorker' in navigator) || !window.isSecureContext) {
    shellStatus.textContent = 'Offline installation requires HTTPS or localhost in a supporting browser.';
    return;
  }
  try {
    await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) await new Promise<void>((resolve) => {
      navigator.serviceWorker.addEventListener('controllerchange', () => resolve(), { once: true });
    });
    shellStatus.textContent = 'Offline app shell ready. Entries are stored on this browser.';
  } catch {
    shellStatus.textContent = 'Offline setup could not finish. Local entries are still available; reload online to retry the shell installation.';
  }
}

try {
  if (!window.isSecureContext || !crypto.randomUUID) throw new Error('Open this proof over HTTPS (or localhost). Plain HTTP on a LAN address cannot provide the installable offline proof.');
  store = await EpisodeStore.open();
  await store.deviceId();
  sandboxPanel.attachStore(store, async () => { if (mode === 'today') await today(); });
  await today();
  void offlineShell();
  void sandboxPanel.start(import.meta.env);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && mode === 'today') void today();
  });
  setInterval(() => {
    if (mode === 'today' && todayDate !== localDate(new Date())) void today();
  }, 30_000);
  if (import.meta.env.DEV) {
    // Explicit console-only developer reset; preserve the installation identity.
    Object.assign(window, { resetZymptomProof: async () => {
      if (confirm('Delete all disposable Zymptom PWA M0 entries in this browser?')) {
        await store.resetDisposableData();
        await today('Disposable entries cleared.');
      }
    } });
  }
} catch (error) {
  notice.textContent = error instanceof Error ? error.message : 'Local storage is unavailable.';
  main.replaceChildren(el('h1', 'Could not open the local proof'), el('p', 'No entries were changed. Check browser permissions and reopen this page.'));
}
