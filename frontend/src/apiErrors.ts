export async function apiErrorMessage(response: Response) {
  const fallback = `HTTP ${response.status}`
  try {
    const payload = (await response.json()) as { detail?: unknown }
    return detailToMessage(payload.detail) ?? fallback
  } catch {
    return fallback
  }
}

function detailToMessage(detail: unknown) {
  if (typeof detail === 'string') return detail
  if (!detail || typeof detail !== 'object') return null
  const message = 'message' in detail ? detail.message : undefined
  return typeof message === 'string' && message.trim() ? message : null
}
