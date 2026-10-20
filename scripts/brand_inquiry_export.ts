// Aggregate, de-identified brand inquiry export. Spec: HANDOFF_brand_inquiry_export.md
//
// Read-only: this script never touches the database. It consumes JSON dumps of the
// aggregate SELECTs listed in HANDOFF_brand_inquiry_export.md (run against production
// through a read-only client) and writes the two CSVs. Counts only — no rate, body,
// subject, sender, reasoning, or talent identifier ever reaches the output.
//
// usage: bun scripts/brand_inquiry_export.ts <inputDir> <outDir>
//   inputDir/brand_week_talent_*.json   [[brand_norm, week, talent_idx, src, n3, n2, us, un, ss, sn, rs, rn], ...]
//   inputDir/brand_week_offer.json      [[brand_norm, week, offer_type, n3, n2], ...]
//   inputDir/coverage.json              per-week row-count aggregates
//   inputDir/roster.json                [{k:'talent'|'sop', a, b, c, d}, ...]
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const [inputDir, outDir] = process.argv.slice(2);
if (!inputDir || !outDir) {
  console.error("usage: bun scripts/brand_inquiry_export.ts <inputDir> <outDir>");
  process.exit(1);
}
mkdirSync(outDir, { recursive: true });

const FALLBACK_WEEK_THRESHOLD = 0.1; // date-fallback rows / week
const UNROUTED_THRESHOLD = 0.1; // to_address IS NULL rows / week
const BILLING_FALLBACK_THRESHOLD = 0.1; // 'Triage fallback' rows / week
const SIMILARITY_THRESHOLD = 0.85; // trigram Dice similarity for brand_unmatched review

function loadDump(path: string): any {
  let raw = readFileSync(path, "utf8");
  let parsed: any = JSON.parse(raw);
  if (Array.isArray(parsed) && parsed[0]?.text) parsed = JSON.parse(parsed[0].text);
  const m = /<untrusted-data-[^>]+>\s*(\[[\s\S]*\])\s*<\/untrusted-data-/.exec(parsed.result);
  if (!m) throw new Error(`no data block in ${path}`);
  return JSON.parse(m[1]);
}

// ── brand aliases (raw,normalized) — committed, not sensitive ─────────────────
const aliasPath = join(import.meta.dir, "..", "brand_aliases.csv");
const aliases = new Map<string, string>();
if (existsSync(aliasPath)) {
  for (const line of readFileSync(aliasPath, "utf8").split("\n").slice(1)) {
    const [raw, norm] = line.split(",").map((s) => s?.trim().toLowerCase());
    if (raw && norm) aliases.set(raw, norm);
  }
}

// ── load aggregates ───────────────────────────────────────────────────────────
type Row = [string, string, number, string, number, number, number, number, number, number, number, number];
const rows: Row[] = [];
for (const f of readdirSync(inputDir).filter((f) => f.startsWith("brand_week_talent_"))) {
  const d = loadDump(join(inputDir, f));
  rows.push(...(d[0].j as Row[]));
}
const offerRows: [string, string, string, number, number][] = loadDump(join(inputDir, "brand_week_offer.json"))[0].j;
const coverage: any[] = loadDump(join(inputDir, "coverage.json"));
const roster: any[] = loadDump(join(inputDir, "roster.json"));

const applyAlias = (b: string) => aliases.get(b) ?? b;

// ── brand_unmatched: near-duplicate names that no alias resolves ──────────────
const brandRows = new Map<string, number>();
for (const r of rows) {
  const b = applyAlias(r[0]);
  brandRows.set(b, (brandRows.get(b) ?? 0) + r[4] + r[5]);
}
const trigrams = (s: string) => {
  const p = `  ${s} `;
  const t = new Set<string>();
  for (let i = 0; i < p.length - 2; i++) t.add(p.slice(i, i + 3));
  return t;
};
const names = [...brandRows.keys()];
const tri = names.map(trigrams);
const index = new Map<string, number[]>();
tri.forEach((set, i) => set.forEach((g) => (index.get(g) ?? index.set(g, []).get(g)!).push(i)));
const unmatched = new Map<string, { partner: string; sim: number }>();
for (let i = 0; i < names.length; i++) {
  const shared = new Map<number, number>();
  for (const g of tri[i]) for (const j of index.get(g)!) if (j > i) shared.set(j, (shared.get(j) ?? 0) + 1);
  for (const [j, c] of shared) {
    const sim = (2 * c) / (tri[i].size + tri[j].size);
    if (sim >= SIMILARITY_THRESHOLD) {
      for (const [a, b] of [[i, j], [j, i]] as const) {
        const prev = unmatched.get(names[a]);
        if (!prev || sim > prev.sim) unmatched.set(names[a], { partner: names[b], sim });
      }
    }
  }
}
writeFileSync(
  join(outDir, "brand_unmatched.csv"),
  "brand_norm,closest_other_brand,similarity,rows_score2_3\n" +
    [...unmatched]
      .sort((x, y) => y[1].sim - x[1].sim)
      .map(([b, v]) => `${csv(b)},${csv(v.partner)},${v.sim.toFixed(3)},${brandRows.get(b)}`)
      .join("\n") +
    "\n",
);
function csv(s: string) {
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// ── brand × week ──────────────────────────────────────────────────────────────
type Agg = {
  talents3: Set<number>; n3: number; n2: number;
  us: number; un: number; ss: number; sn: number; rs: number; rn: number;
  srcE: number; srcF: number; offers: Map<string, number>;
};
const agg = new Map<string, Agg>();
const key = (b: string, w: string) => `${b}\u0000${w}`;
const get = (b: string, w: string) => {
  const k = key(b, w);
  let a = agg.get(k);
  if (!a) agg.set(k, (a = { talents3: new Set(), n3: 0, n2: 0, us: 0, un: 0, ss: 0, sn: 0, rs: 0, rn: 0, srcE: 0, srcF: 0, offers: new Map() }));
  return a;
};
let excludedRows = 0;
for (const [bn, wk, ti, src, n3, n2, us, un, ss, sn, rs, rn] of rows) {
  const b = applyAlias(bn);
  if (unmatched.has(b)) { excludedRows += n3 + n2; continue; }
  const a = get(b, wk);
  a.n3 += n3; a.n2 += n2;
  if (n3 > 0) a.talents3.add(ti);
  a.us += us; a.un += un; a.ss += ss; a.sn += sn; a.rs += rs; a.rn += rn;
  if (src === "e") a.srcE += n3 + n2; else a.srcF += n3 + n2;
}
for (const [bn, wk, ot, n3] of offerRows) {
  const b = applyAlias(bn);
  if (unmatched.has(b) || !ot || n3 === 0) continue;
  const a = agg.get(key(b, wk));
  if (a) a.offers.set(ot, (a.offers.get(ot) ?? 0) + n3);
}

const isoWeek = (d: string) => {
  const dt = new Date(`${d}T00:00:00Z`);
  const th = new Date(dt); th.setUTCDate(dt.getUTCDate() + 3);
  const y = th.getUTCFullYear();
  const jan4 = new Date(Date.UTC(y, 0, 4));
  const w = 1 + Math.round(((th.getTime() - jan4.getTime()) / 86400000 - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
  return `${y}-W${String(w).padStart(2, "0")}`;
};
const mode = (m: Map<string, number>) =>
  [...m].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? "";
const mean = (s: number, n: number) => (n ? (s / n).toFixed(3) : "");

const weeklyLines = [...agg]
  .map(([k, a]) => {
    const [brand, wk] = k.split("\u0000");
    return { brand, wk, a };
  })
  .sort((x, y) => x.wk.localeCompare(y.wk) || x.brand.localeCompare(y.brand))
  .map(({ brand, wk, a }) =>
    [
      csv(brand), isoWeek(wk), wk, a.n3 > 0 ? 1 : 0, a.talents3.size, a.n3, a.n2, csv(mode(a.offers)),
      mean(a.us, a.un), mean(a.ss, a.sn), mean(a.rs, a.rn), a.srcE, a.srcF,
    ].join(","),
  );
const weeklyHeader =
  "brand_norm,iso_week,week_start_date,campaigns,creators_contacted,inquiries_score3,inquiries_score2,offer_type_mode,mean_urgency,mean_sentiment,mean_risk,date_source_email_date,date_source_fallback";
writeFileSync(join(outDir, "brand_inquiry_weekly.csv"), weeklyHeader + "\n" + weeklyLines.join("\n") + "\n");

// ── coverage_weekly ───────────────────────────────────────────────────────────
const talentsRoster = roster.filter((r) => r.k === "talent");
const sops = roster.filter((r) => r.k === "sop" && r.d === "sop").sort((a, b) => a.a.localeCompare(b.a));
const addDays = (d: string, n: number) => new Date(new Date(`${d}T00:00:00Z`).getTime() + n * 86400000).toISOString().slice(0, 10);

type Cov = Record<string, any>;
const cov: Cov[] = coverage.map((c) => {
  const end = addDays(c.week_start_date, 6);
  const connected = talentsRoster.filter((t) => t.a <= end);
  const polledOk = talentsRoster.filter(
    (t) => t.c && t.c.slice(0, 10) >= c.week_start_date && t.c.slice(0, 10) <= end && t.d === "0",
  );
  const sopIdx = sops.filter((s) => s.a <= end).length;
  const sopThisWeek = sops.some((s) => s.a >= c.week_start_date && s.a <= end);
  return {
    iso_week: c.iso_week,
    week_start_date: c.week_start_date,
    active_talents: connected.filter((t) => t.b === "true").length,
    talents_connected: connected.length,
    talents_with_rows: c.talents_with_rows,
    talents_polled_ok: polledOk.length,
    total_rows_processed: c.total,
    rows_score3: c.s3,
    rows_score2: c.s2,
    rows_score1: c.s1,
    rows_other_score: c.other_score,
    distinct_brands: c.distinct_brands_s3,
    rows_null_brand: c.null_brand_rows,
    rows_unrouted: c.unrouted,
    rows_triage_fallback: c.fallback_rows,
    rows_date_email_date: c.ed_rows,
    rows_date_fallback: c.fb_rows,
    // SOP label text carries talent names, so only an ordinal + upload date is exported.
    sop_version_active: sopIdx ? `sop${String(sopIdx).padStart(2, "0")}@${sops[sopIdx - 1].a}` : "",
    triage_models_used: "", // triage_audit is empty in production — nothing to report
    config_changed_flag: sopThisWeek ? 1 : 0,
  };
});

// Stable roster = first week after which week-over-week change in talents_with_rows stays <= 20%.
let firstStable = cov.length ? cov[cov.length - 1].iso_week : "";
for (let i = cov.length - 1; i > 0; i--) {
  const prev = cov[i - 1].talents_with_rows, cur = cov[i].talents_with_rows;
  if (prev === 0 || Math.abs(cur - prev) / prev > 0.2) break;
  firstStable = cov[i - 1].iso_week;
}
const stableIdx = cov.findIndex((c) => c.iso_week === firstStable);

const rosterChangeWeeks: string[] = [];
cov.forEach((c, i) => {
  const flags: string[] = [];
  if (i < stableIdx) flags.push("ONBOARDING_RAMP");
  if (c.rows_unrouted / c.total_rows_processed > UNROUTED_THRESHOLD) flags.push("ALIAS_OUTAGE");
  const ratio = c.rows_score2 ? c.rows_score3 / c.rows_score2 : null;
  const prior = cov.slice(Math.max(0, i - 4), i).map((p) => (p.rows_score2 ? p.rows_score3 / p.rows_score2 : null)).filter((x): x is number => x !== null).sort((a, b) => a - b);
  const med = prior.length ? prior[Math.floor(prior.length / 2)] : null;
  const prevTotal = i ? cov[i - 1].total_rows_processed : null;
  const flatVolume = prevTotal ? Math.abs(c.total_rows_processed - prevTotal) / prevTotal <= 0.25 : false;
  if (c.rows_triage_fallback / c.total_rows_processed > BILLING_FALLBACK_THRESHOLD || (ratio !== null && med !== null && ratio < med * 0.5 && flatVolume)) flags.push("BILLING_GAP");
  c.break_flag = flags.join("|");
  c.date_fallback_flag = c.rows_date_fallback / c.total_rows_processed > FALLBACK_WEEK_THRESHOLD ? 1 : 0;
  c.score3_to_score2_ratio = ratio === null ? "" : ratio.toFixed(3);
  if (i && cov[i - 1].talents_with_rows && Math.abs(c.talents_with_rows - cov[i - 1].talents_with_rows) / cov[i - 1].talents_with_rows > 0.2) rosterChangeWeeks.push(c.iso_week);
});

const covCols = [
  "iso_week", "week_start_date", "active_talents", "talents_connected", "talents_with_rows", "talents_polled_ok",
  "total_rows_processed", "rows_score3", "rows_score2", "rows_score1", "rows_other_score", "distinct_brands",
  "rows_null_brand", "rows_unrouted", "rows_triage_fallback", "rows_date_email_date", "rows_date_fallback",
  "date_fallback_flag", "score3_to_score2_ratio", "sop_version_active", "triage_models_used", "config_changed_flag", "break_flag",
];
writeFileSync(join(outDir, "coverage_weekly.csv"), covCols.join(",") + "\n" + cov.map((c) => covCols.map((k) => csv(String(c[k]))).join(",")).join("\n") + "\n");

// ── run report (stdout + file) ────────────────────────────────────────────────
const sum = (k: string) => cov.reduce((s, c) => s + c[k], 0);
const distinctRaw = new Set(rows.map((r) => r[0])).size;
const flagged = (f: string) => cov.filter((c) => c.break_flag.includes(f)).map((c) => c.iso_week);
const report = [
  `Date range covered (coverage weeks): ${cov[0].week_start_date} .. ${cov[cov.length - 1].week_start_date}`,
  `Rows by score: 3=${sum("rows_score3")} 2=${sum("rows_score2")} 1(excluded)=${sum("rows_score1")} other=${sum("rows_other_score")} total=${sum("total_rows_processed")}`,
  `Rows with no brand_name (cannot be attributed to a brand): ${sum("rows_null_brand")}`,
  `Distinct brands before normalization/alias (post suffix-strip): ${distinctRaw}; after alias map: ${brandRows.size}; aliases loaded: ${aliases.size}`,
  `Unmatched (near-duplicate) brands held out for review: ${unmatched.size} (${excludedRows} score-2/3 rows excluded)`,
  `Weeks with date-fallback > 10%: ${cov.filter((c) => c.date_fallback_flag).map((c) => c.iso_week).join(", ") || "none"}`,
  `Weeks where talents_with_rows changed > 20% week-over-week: ${rosterChangeWeeks.join(", ") || "none"}`,
  `First stable roster week (talents_with_rows change <= 20% from here on): ${firstStable}`,
  `Break ONBOARDING_RAMP weeks: ${flagged("ONBOARDING_RAMP").join(", ") || "none"}`,
  `Break ALIAS_OUTAGE weeks (to_address NULL > 10%): ${flagged("ALIAS_OUTAGE").join(", ") || "none"}`,
  `Break BILLING_GAP weeks: ${flagged("BILLING_GAP").join(", ") || "none"}`,
  `First-contact separation possible: NO. is_thread is true on 99.8% of Score-3 rows (including subjects without a reply prefix), and no column marks TABOOST-initiated outreach, so campaigns_first_contact is not emitted. The campaigns number mixes unsolicited inquiries with replies to TABOOST outreach.`,
].join("\n");
console.log(report);
writeFileSync(join(outDir, "run_report.txt"), report + "\n");

// ── hashes ────────────────────────────────────────────────────────────────────
for (const f of ["brand_inquiry_weekly.csv", "coverage_weekly.csv"]) {
  const data = readFileSync(join(outDir, f));
  console.log(`sha256 ${createHash("sha256").update(data).digest("hex")}  ${f}  rows=${data.toString().trim().split("\n").length - 1}`);
}
