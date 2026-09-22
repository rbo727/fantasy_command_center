const base = ''

async function request(path, options) {
  const res = await fetch(`${base}${path}`, {
    headers: { 'content-type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail
    try {
      detail = (await res.json()).detail
    } catch {
      detail = res.statusText
    }
    throw new Error(detail || `Request failed (${res.status})`)
  }
  return res.json()
}

export const api = {
  overview: () => request('/api/overview'),
  leagues: () => request('/api/leagues'),
  roster: (key) => request(`/api/leagues/${encodeURIComponent(key)}/roster`),
  faab: (key, position) =>
    request(
      `/api/leagues/${encodeURIComponent(key)}/faab` +
        (position ? `?position=${encodeURIComponent(position)}` : '')
    ),
  actions: () => request('/api/actions?limit=100'),
  approve: (id) => request(`/api/actions/${id}/approve`, { method: 'POST' }),
  reject: (id, reason) =>
    request(`/api/actions/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
}
