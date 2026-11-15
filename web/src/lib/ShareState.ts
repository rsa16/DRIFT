export interface ViewState {
  ra: number;
  dec: number;
  fov: number;
  band?: number;
  visit?: number;
}

export const DEFAULT_FOV = 0.87;

function finite(value: string | null): number | null {
  if (value === null || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Parse shareable `?ra=&dec=&fov=&band=&visit=` view state. */
export function parseShareState(search: string): ViewState | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const ra = finite(params.get("ra"));
  const dec = finite(params.get("dec"));
  const fov = finite(params.get("fov"));
  if (ra === null || dec === null || fov === null) return null;
  if (ra < 0 || ra > 360 || dec < -90 || dec > 90 || fov <= 0 || fov > 90) {
    return null;
  }
  const band = finite(params.get("band"));
  const visit = finite(params.get("visit"));
  if (band !== null && (band < 1 || !Number.isInteger(band))) return null;
  if (visit !== null && (visit < 0 || !Number.isInteger(visit))) return null;
  const state: ViewState = { ra, dec, fov };
  if (band !== null) state.band = band;
  if (visit !== null) state.visit = visit;
  return state;
}

/** Serialise view state into query string */
export function formatShareQuery(state: ViewState): string {
  const params = new URLSearchParams({
    ra: String(state.ra),
    dec: String(state.dec),
    fov: String(state.fov),
  });
  if (state.band !== undefined) params.set("band", String(state.band));
  if (state.visit !== undefined) params.set("visit", String(state.visit));
  return params.toString();
}

/** Replace current URL query with shared view */
export function writeShareState(state: ViewState): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.search = formatShareQuery(state);
  window.history.replaceState(null, "", url);
}
