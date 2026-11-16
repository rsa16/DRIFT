export interface WindowInfo {
  lo: number;
  hi: number;
  s: number;
  sha256: string;
}

export interface TargetMode {
  enabled: boolean;
  available?: boolean;
  reason: string;
}

export interface TargetSummary {
  id: string;
  name?: string;
  kind?: string;
  ra: number | null;
  dec: number | null;
  labels: string[];
  fixture?: boolean;
  snapshot_id?: string;
  epoch_count: number;
  visit_count?: number;
  frame_count: number;
  modes: Record<string, TargetMode>;
}

export interface EpochDetail {
  epoch_id: string;
  release: string;
  survey: number | null;
  mjd_min: number | null;
  mjd_max: number | null;
  frames?: unknown[];
}

export interface TimelineInfo {
  default_band: number | null;
  default_detector?: string | null;
  visits: number;
  span_days: number | null;
  band_reason?: string | null;
  bands?: number[];
}

export interface TargetDetail extends TargetSummary {
  epochs: EpochDetail[];
  timeline?: TimelineInfo | null;
  caveats?: string[];
  pixel_scale_arcsec: number;
  units: string;
  window: WindowInfo | null;
}

export interface TargetsResponse {
  snapshot_id: string;
  fixture: boolean;
  targets: TargetSummary[];
}

export interface ManifestSummary {
  schema_version: number;
  snapshot_id: string;
  generated_at: string | null;
  fixture: boolean;
  status: string | null;
  processing?: Record<string, unknown>;
  window?: WindowInfo | null;
  citation?: string | null;
}

export interface Health {
  snapshot_id: string | null;
  snapshot_age_s: number | null;
  plane_count: number;
  disk_free_bytes: number;
}

export interface PlaneMeta {
  index: number;
  band_index?: number;
  band: number;
  detector?: string | null;
  visit_id?: string;
  mjd?: number | null;
  date?: string | null;
  collection?: string | null;
  obs_publisher_did?: string | null;
  epoch_id?: string;
  release: string;
  survey: number | null;
  mjd_min: number | null;
  mjd_max: number | null;
  lambda_um: number;
  lambda_lo_um: number;
  lambda_hi_um: number;
  dlambda_um: number;
  width: number;
  height: number;
  pixel_scale_arcsec?: number;
  dtype: string;
  href: string;
  static_href: string;
  bytes: number;
  sha256: string;
  source_url?: string | null;
  saturated?: boolean | null;
  sigma?: number | null;
  sigma_squared?: number;
  source_sigma?: number | null;
  reference_sigma?: number | null;
  channel?: "r" | "g" | "b";
  role?: "difference" | "rgb";
  difference?: boolean;
  reference_visit_id?: string;
  reference_saturated?: boolean;
  cross_release?: boolean;
  reference_lambda_um?: number | null;
  bandpass_delta_um?: number;
  bandpass_fraction?: number;
  flag_bad_frac?: number | null;
  resolution_reduced?: boolean;
  window_sha256: string;
}

export interface ReferenceVisit {
  visit_id: string;
  mjd: number | null;
  date: string | null;
  release: string;
  detector?: string | null;
  band?: number | null;
  sigma?: number | null;
  href?: string;
  index?: number;
}

export interface FramesResponse {
  snapshot_id: string;
  fixture: boolean;
  target_id: string;
  mode: string;
  bins?: number;
  requested_bins?: number;
  band?: number;
  detector?: string | null;
  default_band?: number | null;
  visits?: number;
  visits_requested?: number;
  available_visits?: number;
  plane_count?: number;
  compressed_bytes?: number;
  degraded?: boolean;
  degraded_header?: boolean;
  resolution_reduced?: boolean;
  frames_omitted?: string[];
  refused_frames?: string[];
  span_days?: number;
  visits_meta?: VisitMeta[];
  epochs?: EpochDetail[];
  reference?: ReferenceVisit;
  channels?: string[];
  cross_release?: boolean;
  max_bandpass_fraction?: number;
  window: WindowInfo;
  pixel_scale_arcsec: number;
  source_plane_px?: number;
  units: string;
  planes: PlaneMeta[];
}

export interface VisitMeta {
  visit_id: string;
  mjd: number | null;
  date: string | null;
  release: string;
  survey: number | null;
  collection: string | null;
  obs_publisher_did: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const TIMEOUT_MS = 30_000;

async function request(path: string): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    throw new ApiError(0, `network request failed: ${String(error)}`);
  }
  if (!response.ok) {
    let detail = `request returned ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep the status-based message when the body is not JSON.
    }
    throw new ApiError(response.status, detail);
  }
  return response;
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path);
  try {
    return (await response.json()) as T;
  } catch (error) {
    throw new ApiError(response.status, `invalid JSON response: ${String(error)}`);
  }
}

export function fetchManifest(): Promise<ManifestSummary> {
  return fetchJson<ManifestSummary>("/api/v1/manifest");
}

export function fetchTargets(): Promise<TargetsResponse> {
  return fetchJson<TargetsResponse>("/api/v1/targets");
}

export function fetchTarget(id: string): Promise<TargetDetail> {
  return fetchJson<TargetDetail>(`/api/v1/targets/${encodeURIComponent(id)}`);
}

export interface FrameRequest {
  mode?: "timeline" | "epoch" | "diff" | "rgb";
  band?: number | null;
  visits?: number;
  bins?: number;
  reference?: string | number;
  visit?: string | number;
  epochs?: string;
}

export async function fetchFrames(
  id: string,
  request_: FrameRequest = {},
): Promise<FramesResponse> {
  const params = new URLSearchParams({ mode: request_.mode ?? "timeline" });
  if (request_.band !== null && request_.band !== undefined) {
    params.set("band", String(request_.band));
  }
  if (request_.visits !== undefined) params.set("visits", String(request_.visits));
  if (request_.bins !== undefined) params.set("bins", String(request_.bins));
  if (request_.reference !== undefined) {
    params.set("reference", String(request_.reference));
  }
  if (request_.visit !== undefined) params.set("visit", String(request_.visit));
  if (request_.epochs !== undefined) params.set("epochs", request_.epochs);
  const response = await request(
    `/api/v1/targets/${encodeURIComponent(id)}/frames?${params.toString()}`,
  );
  let body: FramesResponse;
  try {
    body = (await response.json()) as FramesResponse;
  } catch (error) {
    throw new ApiError(
      response.status,
      `invalid JSON response: ${String(error)}`,
    );
  }
  body.degraded_header = response.headers.get("x-spx-degraded") === "1";
  return body;
}

export function fetchHealth(): Promise<Health> {
  return fetchJson<Health>("/api/v1/health");
}

export async function fetchPlane(href: string): Promise<ArrayBuffer> {
  const response = await request(href);
  return response.arrayBuffer();
}
