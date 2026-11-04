// Detects publication credentials using explicit registrations and per-file baseline token counts for Pi and Deep CI.
export const PUBLICATION_SECRET_CONTRACT = 'astral-publication-secrets/v1';

export function containsPublicationSecret(value, baseline = '', registeredSecrets = []) {
  const text = String(value);
  for (const secret of registeredSecrets) {
    if (typeof secret === 'string' && secret.length && text.includes(secret)) return true;
  }
  if (/-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----/.test(text)) return true;
  const pattern = /\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b/g;
  const existing = new Map();
  for (const match of String(baseline).matchAll(pattern)) existing.set(match[0], (existing.get(match[0]) ?? 0) + 1);
  for (const match of text.matchAll(pattern)) {
    const count = existing.get(match[0]) ?? 0;
    if (!count) return true;
    existing.set(match[0], count - 1);
  }
  return false;
}
