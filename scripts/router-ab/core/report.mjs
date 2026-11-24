// core/report.mjs - the verdict table (Markdown, so it can be pasted into a PR or a runbook record).

/** pairing: [{ env, account }], the accounts the direct arms' logins are on (the router arms are held to them). */
export function render({ image, startedAt, providers, pairing = [] }) {
  const paired = pairing.length ? [`Paired accounts: ${pairing.map((x) => `${x.env}=${x.account ?? '(unset)'}`).join(', ')}`, ''] : [];
  const out = [`# Router release gate: ${startedAt}`, '', `Router image: \`${image ?? 'unknown'}\``, '', ...paired];
  const head = out.length;
  const summary = [];
  for (const p of providers) {
    out.push(`## ${p.name}`, '');
    out.push(`**Layer (a): ${p.layerA.verdict}**`, '');
    for (const c of p.layerA.checks) out.push(`- ${c.pass ? 'pass' : 'FAIL'}: ${c.name}${c.detail ? `: ${c.detail}` : ''}`);
    out.push('');
    for (const t of p.tasks) {
      out.push(`**Task \`${t.id}\` (${t.pairs} pairs): ${t.verdict}**`, '', '| Metric | Direct | Router | Margin (§9.6) | Status |', '|---|---|---|---|---|');
      for (const r of t.rows) out.push(`| ${r.label} | ${r.direct} | ${r.router} | ${r.margin} | ${r.status} |`);
      if (t.notes?.length) out.push('', ...t.notes.map((n) => `- ${n}`));
      out.push('');
      summary.push({ provider: p.name, task: t.id, verdict: t.verdict });
    }
    summary.push({ provider: p.name, task: 'Layer (a)', verdict: p.layerA.verdict });
  }
  const overall = summary.some((s) => s.verdict.startsWith('FAIL')) ? 'FAIL' : summary.some((s) => s.verdict.startsWith('INVALID')) ? 'INCOMPLETE'
    : summary.some((s) => s.verdict.startsWith('RERUN')) ? 'RERUN' : 'PASS';
  out.splice(head, 0, `**Overall: ${overall}**`, '', '| Provider | Check | Verdict |', '|---|---|---|', ...summary.map((s) => `| ${s.provider} | ${s.task} | ${s.verdict} |`), '');
  return { text: out.join('\n'), overall };
}
