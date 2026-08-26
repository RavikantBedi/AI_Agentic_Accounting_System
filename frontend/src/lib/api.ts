/**
 * API client.
 *
 * Money crosses the wire as a *string* so it never touches a JavaScript number
 * (which is a float, and would corrupt paise exactly as it would server-side).
 * Parsing happens deliberately, at the point of display or arithmetic, via the
 * helpers in `money.ts`.
 */

const BASE = '/api/v1'

const ACCESS_KEY = 'ailedger.access'
const REFRESH_KEY = 'ailedger.refresh'
const COMPANY_KEY = 'ailedger.company'

export interface ApiError extends Error {
  code: string
  status: number
  details?: unknown
  requestId?: string
}

export const tokens = {
  get access() {
    return localStorage.getItem(ACCESS_KEY)
  },
  get refresh() {
    return localStorage.getItem(REFRESH_KEY)
  },
  get companyId() {
    return localStorage.getItem(COMPANY_KEY)
  },
  set(access: string, refresh: string, companyId?: string | null) {
    localStorage.setItem(ACCESS_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
    if (companyId) localStorage.setItem(COMPANY_KEY, companyId)
  },
  setCompany(companyId: string) {
    localStorage.setItem(COMPANY_KEY, companyId)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
    localStorage.removeItem(COMPANY_KEY)
  },
}

function buildError(status: number, body: any): ApiError {
  const error = new Error(
    body?.message || `Request failed with status ${status}`,
  ) as ApiError
  error.code = body?.code || 'error'
  error.status = status
  error.details = body?.details
  error.requestId = body?.request_id
  return error
}

let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  const refresh = tokens.refresh
  if (!refresh) return false

  // Collapse concurrent 401s into a single refresh attempt.
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${BASE}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refresh }),
        })
        if (!response.ok) return false
        const data = await response.json()
        tokens.set(data.access_token, data.refresh_token, data.company?.id)
        return true
      } catch {
        return false
      } finally {
        refreshInFlight = null
      }
    })()
  }
  return refreshInFlight
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  raw?: boolean
}

export async function request<T = any>(
  path: string,
  options: RequestOptions = {},
  isRetry = false,
): Promise<T> {
  const { body, query, raw, headers, ...rest } = options

  let url = path.startsWith('http') ? path : `${BASE}${path}`
  if (query) {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        params.append(key, String(value))
      }
    }
    const queryString = params.toString()
    if (queryString) url += `?${queryString}`
  }

  const requestHeaders: Record<string, string> = { ...(headers as any) }
  const isFormData = body instanceof FormData
  if (body !== undefined && !isFormData) {
    requestHeaders['Content-Type'] = 'application/json'
  }
  if (tokens.access) requestHeaders.Authorization = `Bearer ${tokens.access}`
  if (tokens.companyId) requestHeaders['X-Company-Id'] = tokens.companyId

  const response = await fetch(url, {
    ...rest,
    headers: requestHeaders,
    body: body === undefined ? undefined : isFormData ? body : JSON.stringify(body),
  })

  if (response.status === 401 && !isRetry && tokens.refresh) {
    if (await refreshSession()) {
      return request<T>(path, options, true)
    }
    tokens.clear()
    if (!location.pathname.startsWith('/login')) location.href = '/login'
  }

  if (raw) {
    if (!response.ok) throw buildError(response.status, await safeJson(response))
    return response as unknown as T
  }

  if (response.status === 204) return undefined as T

  const payload = await safeJson(response)
  if (!response.ok) throw buildError(response.status, payload)
  return payload as T
}

async function safeJson(response: Response) {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { message: text.slice(0, 500) }
  }
}

export const api = {
  get: <T = any>(path: string, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'GET', query }),
  post: <T = any>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'POST', body, query }),
  put: <T = any>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T = any>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'PATCH', body, query }),
  delete: <T = any>(path: string) => request<T>(path, { method: 'DELETE' }),

  upload: <T = any>(path: string, file: File, query?: RequestOptions['query']) => {
    const form = new FormData()
    form.append('file', file)
    return request<T>(path, { method: 'POST', body: form, query })
  },

  /** Trigger a browser download for an endpoint that returns a file. */
  download: async (path: string, filename: string, query?: RequestOptions['query']) => {
    const response = await request<Response>(path, { method: 'GET', query, raw: true })
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  },
}

export async function login(email: string, password: string) {
  const data = await api.post('/auth/login', { email, password })
  tokens.set(data.access_token, data.refresh_token, data.company?.id)
  return data
}

export async function logout() {
  try {
    await api.post('/auth/logout')
  } catch {
    // Signing out locally must succeed even if the server call fails.
  }
  tokens.clear()
}

export function isAuthenticated() {
  return Boolean(tokens.access)
}
