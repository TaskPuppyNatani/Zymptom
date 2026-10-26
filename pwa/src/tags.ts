import { addSelected, type TagField } from './model';

export const tagTitles: Record<TagField, string> = {
  functional_demands: 'Functional demands', symptoms: 'Symptoms', adaptations: 'Adaptations',
  consequences: 'Consequences', recovery_actions: 'Recovery actions',
};

export class TagPicker {
  readonly element = document.createElement('fieldset');
  values: string[];
  private selected: string[] = [];

  constructor(field: TagField, labels: Record<string, string>, initial: string[]) {
    this.values = [...initial];
    this.element.className = 'tag-field';
    this.element.dataset.field = field;
    const legend = document.createElement('legend');
    legend.textContent = tagTitles[field];
    const chips = document.createElement('div');
    chips.className = 'chips';
    const choices = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Choose values';
    const options = document.createElement('div');
    options.className = 'tag-options';
    const add = document.createElement('button');
    add.type = 'button';
    add.textContent = 'Add selected';
    add.disabled = true;
    add.className = 'small-button';
    for (const [value, text] of Object.entries(labels)) {
      const label = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = value;
      checkbox.addEventListener('change', () => {
        this.selected = checkbox.checked ? [...this.selected, value] : this.selected.filter((item) => item !== value);
        add.disabled = !this.selected.length;
      });
      label.append(checkbox, document.createTextNode(text));
      options.append(label);
    }
    const render = () => {
      chips.replaceChildren();
      if (!this.values.length) {
        const empty = document.createElement('span');
        empty.className = 'muted';
        empty.textContent = 'None recorded';
        chips.append(empty);
      }
      this.values.forEach((value, index) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chip';
        chip.textContent = `${labels[value] ?? value} ×`;
        chip.setAttribute('aria-label', `Remove ${labels[value] ?? value} from ${tagTitles[field]}`);
        chip.addEventListener('click', () => {
          this.values.splice(index, 1);
          render();
        });
        chips.append(chip);
      });
    };
    add.addEventListener('click', () => {
      this.values = addSelected(this.values, this.selected);
      this.selected = [];
      options.querySelectorAll('input').forEach((input) => { input.checked = false; });
      add.disabled = true;
      render();
    });
    choices.append(summary, options, add);
    this.element.append(legend, chips, choices);
    render();
  }
}
