import SSF from 'ssf';

// This function is also the benchmark's independently testable display seam.
export function formatNumber(format, value, options = {}) {
  let normalized = '';
  let quoted = false;
  for (let i = 0; i < format.length;) {
    const ch = format[i];
    // Escapes, spacing and repeat markers consume the next literal character.
    if (!quoted && ['\\', '_', '*'].includes(ch)) {
      normalized += format.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (ch === '"') quoted = !quoted;
    const marker = !quoted && /^\[\$-([0-9a-f]{1,8})\]/i.exec(format.slice(i));
    if (marker) {
      const locale = Number.parseInt(marker[1], 16);
      if ((locale & 0xffff) !== 0x0409 || (locale >>> 24) !== 0 || ((locale >>> 16) & 0xff) > 1) {
        throw new Error(`Unsupported locale-only format marker: ${marker[0]}`);
      }
      i += marker[0].length;
    } else {
      normalized += ch;
      i += 1;
    }
  }
  return SSF.format(normalized, value, options);
}
