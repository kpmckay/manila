import { tracked } from "./status.js";

const jsonHeaders = { "Content-Type": "application/json" };

async function request(method, path, body) {
  const options = { method, headers: jsonHeaders };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${method} ${path} failed`);
  return payload;
}

export const api = {
  /* The project every scoped call belongs to. Set once when a project is
   * opened; api.p() is the only way row and folder URLs are built, so a stale
   * or missing id fails loudly instead of hitting the wrong project. */
  project: null,
  p(path) {
    if (!this.project) throw new Error("no project open");
    return `/api/p/${this.project}${path}`;
  },
  get:   (path)       => request("GET", path),
  /* Documents go up as their own bytes, with the name in a header. No
   * multipart, no encoding, and the server writes exactly what was sent. */
  upload: (path, file) => tracked(async () => {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream",
                 "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `upload of ${file.name} failed`);
    return payload;
  }),
  /* Every write is tracked here, so the header's save status covers all of
   * them -- not only the ones a caller remembered to wrap. */
  post:  (path, body) => tracked(() => request("POST", path, body ?? {})),
  patch: (path, body) => tracked(() => request("PATCH", path, body)),
};

let toastTimer = null;
export function toast(message) {
  document.querySelector(".toast")?.remove();
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.append(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 4000);
}
