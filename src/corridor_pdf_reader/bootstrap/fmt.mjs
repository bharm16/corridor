// Apply each workbook's own number format with SSF; stdin [{f, v, d}] -> stdout [display|null].
//
// Reference correction, logged in bootstrap/README.md: ssf 0.11.2 drops the
// integer zero padding of formats like "00.000000" (Excel prints 00.260000, SSF
// 0.260000) whenever the format also has decimals. The pad is restored here from
// the first format section; formats without decimals already format correctly.
import fs from 'node:fs';
import {formatNumber} from '../paired_trial/number_format.mjs';

function padIntegerZeros(format, text) {
  const section = format.split(';')[0].replace(/"[^"]*"|\[[^\]]*\]|[_*]./g, '');
  const match = /(?:^|[^0#,.])(0{2,})\.0/.exec(section);
  if (!match) return text;
  const width = match[1].length;
  return text.replace(/^(\D*?)(\d+)(?=\.)/, (whole, prefix, digits) =>
    digits.length >= width ? whole : prefix + digits.padStart(width, '0'));
}

// Reference correction, logged in bootstrap/README.md: Excel rounds a number
// for display as its 15-significant-digit decimal, half away from zero, so a
// stored 47647.854999999996 prints as 47,647.86. SSF rounds the exact binary
// value and prints .85. Round the way Excel does before formatting.
function excelRound(format, value) {
  const sections = format.split(';');
  const section = (value < 0 && sections.length > 1 ? sections[1] : sections[0])
    .replace(/"[^"]*"|\[[^\]]*\]|[_*]./g, '');
  if (/[dmyhsE\/]/i.test(section.replace(/General/i, ''))) return value;
  const match = /\.([0#?]+)/.exec(section);
  const decimals = match ? match[1].length : (/[0#]/.test(section) ? 0 : null);
  if (decimals === null) return value;
  const percent = /%/.test(section);
  const scaled = percent ? value * 100 : value;
  const digits = Math.abs(scaled).toPrecision(15);
  const [whole, fraction = ''] = (digits.includes('e') ? Number(digits).toFixed(20) : digits).split('.');
  let kept = whole + (fraction.slice(0, decimals));
  const next = fraction.charAt(decimals);
  let rounded = BigInt(kept || '0');
  if (next !== '' && Number(next) >= 5) rounded += 1n;
  const text = rounded.toString().padStart(decimals + 1, '0');
  const result = Number(decimals ? text.slice(0, -decimals) + '.' + text.slice(-decimals) : text);
  // Excel chooses the format section by the sign of the unrounded value, so a
  // small negative prints (0.00) and a small positive 0.00, never the zero
  // section. Keep that sign alive when rounding lands on zero.
  const magnitude = result === 0 && scaled !== 0 ? 1e-12 : result;
  const signed = scaled < 0 ? -magnitude : magnitude;
  return percent ? signed / 100 : signed;
}

const items = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = items.map(({f, v, d}) => {
  try {
    const value = typeof v === 'number' ? excelRound(f || 'General', v) : v;
    const text = formatNumber(f || 'General', value, {date1904: !!d});
    return typeof v === 'number' ? padIntegerZeros(f || 'General', text) : text;
  } catch (error) { return null; }
});
process.stdout.write(JSON.stringify(out));
