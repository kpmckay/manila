/* Inline SVG, drawn to match the sketch's row icons. */

const svg = (body, extra = "") =>
  `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" ${extra}>${body}</svg>`;

/* Folder with a down arrow -- archive. */
export const archive = svg(`
  <path d="M2.5 5.5a1 1 0 0 1 1-1h3.4l1.3 1.6h8.3a1 1 0 0 1 1 1v8.4a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1z"/>
  <path d="M10 8.6v4.2"/><path d="M8.2 11.1 10 12.9l1.8-1.8"/>`);

/* Folder with an up arrow -- bring an archived row back. */
export const unarchive = svg(`
  <path d="M2.5 5.5a1 1 0 0 1 1-1h3.4l1.3 1.6h8.3a1 1 0 0 1 1 1v8.4a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1z"/>
  <path d="M10 13v-4.2"/><path d="M8.2 10.5 10 8.7l1.8 1.8"/>`);

/* Trash -- permanent: there is no trash state to fish things out of. */
export const trash = svg(`
  <path d="M4 6h12"/><path d="M8 6V4.6a.6.6 0 0 1 .6-.6h2.8a.6.6 0 0 1 .6.6V6"/>
  <path d="M5.6 6l.7 9.4a1 1 0 0 0 1 .9h5.4a1 1 0 0 0 1-.9L15.4 6"/>
  <path d="M8.6 9v4.4"/><path d="M11.4 9v4.4"/>`);



export const grip = svg(`
  <circle cx="8" cy="6" r=".9" fill="currentColor" stroke="none"/>
  <circle cx="12" cy="6" r=".9" fill="currentColor" stroke="none"/>
  <circle cx="8" cy="10" r=".9" fill="currentColor" stroke="none"/>
  <circle cx="12" cy="10" r=".9" fill="currentColor" stroke="none"/>
  <circle cx="8" cy="14" r=".9" fill="currentColor" stroke="none"/>
  <circle cx="12" cy="14" r=".9" fill="currentColor" stroke="none"/>`);

/* The chip marker: an arrow leaving a box, as drawn in the sketch. */
export const chip = svg(`
  <path d="M9 4.5H5a1 1 0 0 0-1 1V15a1 1 0 0 0 1 1h9.5a1 1 0 0 0 1-1v-4"/>
  <path d="M11.5 4.5H16V9"/><path d="M16 4.5 9.6 10.9"/>`, 'stroke-width="1.8"');

/* A clock -- the whole entry, replayed. */
export const log = svg(`
  <circle cx="10" cy="10" r="6.4"/>
  <path d="M10 6.2V10l2.6 1.7"/>`);

/* Accept -- a suggestion becomes an edit you made. */
export const tick = svg(`<path d="M4.5 10.5 8 14l7.5-8"/>`, 'stroke-width="1.9"');

/* Reject -- it leaves no trace in the folder at all. */
export const cross = svg(`<path d="M5.5 5.5l9 9"/><path d="M14.5 5.5l-9 9"/>`,
                         'stroke-width="1.8"');

/* Edit before accepting: a pencil. */
export const pencil = svg(`
  <path d="M13.2 3.9a1.4 1.4 0 0 1 2 2L7.4 13.7l-2.7.7.7-2.7z"/>
  <path d="M11.9 5.2l2 2"/>`);
