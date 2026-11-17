// JSON API client. The CSRF token lives only in memory (never storage); it is
// re-read from GET /api/auth/session after each page load.

let csrf: string | null = null;
const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export function setCsrf(value: string | null) {
  csrf = value;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = opts.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (UNSAFE.has(method) && csrf) headers['X-CSRF-Token'] = csrf;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch {
    throw new ApiError(0, 'Could not reach kWattson. Check your connection and try again.');
  }
  const text = await response.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    let detail = `Request failed (HTTP ${response.status})`;
    if (typeof data?.detail === 'string') detail = data.detail;
    else if (Array.isArray(data?.detail)) detail = data.detail.map((d: any) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      window.dispatchEvent(new CustomEvent('kwattson:auth-required'));
    }
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

export const get = <T = any>(path: string) => api<T>(path);
export const post = <T = any>(path: string, body: unknown = {}) => api<T>(path, { method: 'POST', body });
export const put = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body });
export const del = <T = any>(path: string) => api<T>(path, { method: 'DELETE' });

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));
