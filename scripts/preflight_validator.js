#!/usr/bin/env node
/**
 * Preflight Validator — Talent Inbox Automation
 *
 * Run this script before activating any Make scenario to catch
 * missing config values, mismatched tab names, bad connection names,
 * and incomplete SOP data before they cause a live failure.
 *
 * Usage:
 *   node scripts/preflight_validator.js
 *
 * Requirements:
 *   Node.js 16+. No additional npm packages required.
 *
 * Exit codes:
 *   0 — all checks passed
 *   1 — one or more checks failed (details printed above)
 */

const fs = require("fs");
const path = require("path");

// ─── Load files ──────────────────────────────────────────────────────────────

const ROOT = path.resolve(__dirname, "..");

function loadJSON(relPath) {
  const absPath = path.join(ROOT, relPath);
  if (!fs.existsSync(absPath)) {
    return { _missing: true, _path: relPath };
  }
  try {
    return JSON.parse(fs.readFileSync(absPath, "utf8"));
  } catch (e) {
    return { _parseError: true, _path: relPath, _message: e.message };
  }
}

const config  = loadJSON("config/settings.json");
const sopData = loadJSON("sheets/sop_data.json");

// ─── Helpers ─────────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;
const errors = [];
const warnings = [];

function ok(label) {
  console.log(`  \u2705  ${label}`);
  passed++;
}

function fail(label, detail) {
  console.log(`  \u274c  ${label}`);
  if (detail) console.log(`       \u2192 ${detail}`);
  errors.push({ label, detail });
  failed++;
}

function warn(label, detail) {
  console.log(`  \u26a0\ufe0f   ${label}`);
  if (detail) console.log(`       \u2192 ${detail}`);
  warnings.push({ label, detail });
}

function section(title) {
  console.log(`\n\u2500\u2500 ${title} ${"─".repeat(Math.max(0, 55 - title.length))}`);
}

// ─── Valid values ─────────────────────────────────────────────────────────────

const VALID_TRIAGE_MODELS   = ["gpt-4o-mini", "gpt-4o"];
const VALID_REPLY_MODELS    = ["gpt-4o"];
const VALID_RATE_UNITS      = ["per video", "per hour", "per post", "per reel"];
const VALID_MANAGERS        = ["Cara", "Chenni", "Nicole"];
const VALID_OFFER_TYPES     = [
  "Sponsored Post", "Story", "UGC", "Affiliate", "Event Appearance", "Other",
];
const VALID_AUTO_FLAGS      = ["YES", "NO", "ESCALATE"];
const VALID_MAKE_ZONES      = ["us1.make.com", "eu1.make.com", "us2.make.com"];
const EMAIL_REGEX           = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ─── Check 1: File loading ────────────────────────────────────────────────────

section("File Loading");

if (config._missing) {
  fail("config/settings.json exists", `File not found at ${config._path}`);
  console.log("\n\uD83D\uDED1 Cannot continue — config/settings.json is required.\n");
  process.exit(1);
} else if (config._parseError) {
  fail("config/settings.json is valid JSON", config._message);
  process.exit(1);
} else {
  ok("config/settings.json loaded");
}

if (sopData._missing) {
  warn("sheets/sop_data.json exists", "SOP data file not found — SOP checks will be skipped.");
} else if (sopData._parseError) {
  fail("sheets/sop_data.json is valid JSON", sopData._message);
} else {
  ok("sheets/sop_data.json loaded");
}

// ─── Check 2: OpenAI settings ─────────────────────────────────────────────────

section("OpenAI Settings");

const ai = config.openai || {};

if (!ai.triage_model) {
  fail("openai.triage_model is set");
} else if (!VALID_TRIAGE_MODELS.includes(ai.triage_model)) {
  fail(
    `openai.triage_model is a known model`,
    `Got "${ai.triage_model}". Expected one of: ${VALID_TRIAGE_MODELS.join(", ")}`
  );
} else {
  ok(`openai.triage_model = "${ai.triage_model}"`);
}

if (!ai.reply_model) {
  fail("openai.reply_model is set");
} else if (!VALID_REPLY_MODELS.includes(ai.reply_model)) {
  fail(
    `openai.reply_model is "${VALID_REPLY_MODELS[0]}"`,
    `Got "${ai.reply_model}". Reply drafting requires gpt-4o — do NOT use gpt-4o-mini here.`
  );
} else {
  ok(`openai.reply_model = "${ai.reply_model}"`);
}

if (ai.max_tokens_triage === undefined || ai.max_tokens_triage < 100) {
  warn("openai.max_tokens_triage", `Value: ${ai.max_tokens_triage}. Recommend 200.`);
} else {
  ok(`openai.max_tokens_triage = ${ai.max_tokens_triage}`);
}

if (ai.max_tokens_reply === undefined || ai.max_tokens_reply < 400) {
  warn("openai.max_tokens_reply", `Value: ${ai.max_tokens_reply}. Recommend 800.`);
} else {
  ok(`openai.max_tokens_reply = ${ai.max_tokens_reply}`);
}

if (ai.temperature_triage === undefined || ai.temperature_triage > 0.3) {
  warn(
    "openai.temperature_triage",
    `Value: ${ai.temperature_triage}. Should be <= 0.2 for deterministic classification.`
  );
} else {
  ok(`openai.temperature_triage = ${ai.temperature_triage}`);
}

// ─── Check 3: Reply send delay ────────────────────────────────────────────────

section("Reply Send Delay");

const reply = config.reply || {};

if (reply.send_delay_enabled === undefined) {
  fail("reply.send_delay_enabled is set");
} else if (reply.send_delay_enabled !== true) {
  warn(
    "reply.send_delay_enabled is TRUE",
    "Send delay is currently OFF. Keep it enabled during Phase 2 testing and QA period."
  );
} else {
  ok("reply.send_delay_enabled = true");
}

if (reply.send_delay_minutes === undefined) {
  fail("reply.send_delay_minutes is set");
} else if (reply.send_delay_minutes < 15) {
  warn(
    "reply.send_delay_minutes >= 15",
    `Currently ${reply.send_delay_minutes} min. Keep at 15 min during Phase 2 QA.`
  );
} else {
  ok(`reply.send_delay_minutes = ${reply.send_delay_minutes}`);
}

// ─── Check 4: Digest settings ─────────────────────────────────────────────────

section("Daily Digest Settings");

const digest = config.digest || {};

if (!digest.recipient_email || digest.recipient_email.trim() === "") {
  fail("digest.recipient_email is set", "TODO: Fill in supervisor email address.");
} else if (!EMAIL_REGEX.test(digest.recipient_email.split(",")[0].trim())) {
  fail(
    "digest.recipient_email looks like a valid email",
    `Got: "${digest.recipient_email}"`
  );
} else {
  ok(`digest.recipient_email = "${digest.recipient_email}"`);
}

if (!digest.send_time) {
  fail("digest.send_time is set");
} else {
  ok(`digest.send_time = "${digest.send_time}"`);
}

if (!digest.timezone) {
  fail("digest.timezone is set");
} else {
  ok(`digest.timezone = "${digest.timezone}"`);
}

if (!digest.subject_template) {
  fail("digest.subject_template is set");
} else {
  ok("digest.subject_template is set");
}

// ─── Check 5: Google Sheets ───────────────────────────────────────────────────

section("Google Sheets IDs");

const sheets = config.google_sheets || {};

if (!sheets.master_log_sheet_id || sheets.master_log_sheet_id.trim() === "") {
  fail(
    "google_sheets.master_log_sheet_id is set",
    "TODO: Paste the Google Sheet ID from the master log URL."
  );
} else {
  ok(`google_sheets.master_log_sheet_id = "${sheets.master_log_sheet_id}"`);
}

if (!sheets.sop_matrix_sheet_id || sheets.sop_matrix_sheet_id.trim() === "") {
  fail(
    "google_sheets.sop_matrix_sheet_id is set",
    "TODO: Paste the Google Sheet ID from Britney's SOP matrix URL."
  );
} else {
  ok(`google_sheets.sop_matrix_sheet_id = "${sheets.sop_matrix_sheet_id}"`);
}

if (!sheets.master_log_tab_name) {
  fail("google_sheets.master_log_tab_name is set");
} else {
  ok(`google_sheets.master_log_tab_name = "${sheets.master_log_tab_name}"`);
}

// ─── Check 6: Make settings ───────────────────────────────────────────────────

section("Make Workspace Settings");

const make = config.make || {};

if (!make.workspace_zone) {
  fail("make.workspace_zone is set");
} else if (!VALID_MAKE_ZONES.includes(make.workspace_zone)) {
  warn(
    `make.workspace_zone is a known zone`,
    `Got "${make.workspace_zone}". Known zones: ${VALID_MAKE_ZONES.join(", ")}`
  );
} else {
  ok(`make.workspace_zone = "${make.workspace_zone}"`);
}

if (!make.error_alert_email || make.error_alert_email.trim() === "") {
  fail(
    "make.error_alert_email is set",
    "TODO: Fill in email address for immediate scenario failure alerts."
  );
} else if (!EMAIL_REGEX.test(make.error_alert_email.split(",")[0].trim())) {
  fail("make.error_alert_email looks valid", `Got: "${make.error_alert_email}"`);
} else {
  ok(`make.error_alert_email = "${make.error_alert_email}"`);
}

// ─── Check 7: Talent list ─────────────────────────────────────────────────────

section("Talent List");

const talents = config.talents || [];

if (!Array.isArray(talents) || talents.length === 0) {
  fail("config.talents array is populated");
} else {
  ok(`${talents.length} talent(s) found`);
}

const EXPECTED_MIN_TALENTS = 14;
if (talents.length < EXPECTED_MIN_TALENTS) {
  warn(
    `At least ${EXPECTED_MIN_TALENTS} talents configured`,
    `Only ${talents.length} found. Blueprint targets 15-20 inboxes.`
  );
}

const talentKeys = new Set();
const gmailConnections = new Set();

for (const talent of talents) {
  const name = talent.full_name || talent.key || "(unnamed)";

  if (!talent.key) { fail(`Talent "${name}" has a key`); continue; }
  if (!talent.full_name) fail(`Talent "${name}" has a full_name`);
  if (!talent.gmail_connection_name) {
    fail(`Talent "${name}" has gmail_connection_name`);
  } else {
    const expectedFormat = `Gmail - ${talent.key}`;
    if (talent.gmail_connection_name !== expectedFormat) {
      warn(
        `Talent "${name}" Gmail connection name format`,
        `Got "${talent.gmail_connection_name}", expected "${expectedFormat}". ` +
        `Phase 2 dynamic routing requires exact "Gmail - [Key]" format.`
      );
    }
  }

  if (!talent.sop_tab_name) fail(`Talent "${name}" has sop_tab_name`);

  if (!talent.manager) {
    fail(`Talent "${name}" has a manager`);
  } else if (!VALID_MANAGERS.includes(talent.manager)) {
    warn(
      `Talent "${name}" manager is a known manager`,
      `Got "${talent.manager}". Known managers: ${VALID_MANAGERS.join(", ")}`
    );
  }

  if (talent.minimum_rate_usd === undefined || talent.minimum_rate_usd < 0) {
    fail(`Talent "${name}" has a valid minimum_rate_usd`);
  }

  if (!talent.rate_unit) {
    fail(`Talent "${name}" has a rate_unit`);
  } else if (!VALID_RATE_UNITS.includes(talent.rate_unit)) {
    warn(
      `Talent "${name}" rate_unit is a known unit`,
      `Got "${talent.rate_unit}". Known: ${VALID_RATE_UNITS.join(", ")}`
    );
  }

  if (talentKeys.has(talent.key)) {
    fail(`Talent key "${talent.key}" is unique`, "Duplicate key detected.");
  } else {
    talentKeys.add(talent.key);
  }

  if (gmailConnections.has(talent.gmail_connection_name)) {
    fail(
      `Gmail connection "${talent.gmail_connection_name}" is unique`,
      "Two talents share the same Gmail connection — this will cause inbox collisions."
    );
  } else if (talent.gmail_connection_name) {
    gmailConnections.add(talent.gmail_connection_name);
  }
}

// ─── Check 8: SOP tab name alignment ─────────────────────────────────────────

section("SOP Tab Name Alignment (config <-> sop_data.json)");

if (!sopData._missing && !sopData._parseError) {
  const sopKeys = Object.keys(sopData);

  for (const talent of talents) {
    const key = talent.key;
    if (!sopData[key]) {
      fail(
        `Talent key "${key}" found in sop_data.json`,
        `No matching entry in sheets/sop_data.json. SOP data may be missing for this talent.`
      );
    } else {
      const sopEntry = sopData[key];
      if (sopEntry.full_name && talent.full_name && sopEntry.full_name !== talent.full_name) {
        warn(
          `Talent "${key}" full_name matches between config and SOP data`,
          `config: "${talent.full_name}", sop_data: "${sopEntry.full_name}"`
        );
      }

      if (
        sopEntry.min_rate_usd !== undefined &&
        talent.minimum_rate_usd !== undefined &&
        sopEntry.min_rate_usd !== talent.minimum_rate_usd
      ) {
        warn(
          `Talent "${key}" minimum rate matches between config and SOP data`,
          `config: $${talent.minimum_rate_usd}, sop_data: $${sopEntry.min_rate_usd}. ` +
          `Make sure the triage prompt uses the config value as the authoritative source.`
        );
      }

      if (!sopEntry.rules || sopEntry.rules.length === 0) {
        fail(`Talent "${key}" has SOP rules defined in sop_data.json`);
      } else {
        ok(`Talent "${key}" — ${sopEntry.rules.length} SOP rule(s) found`);
      }

      if (key === "KatrinaD" && talent.rate_unit !== "per hour") {
        fail(
          `KatrinaD rate_unit is "per hour"`,
          "KatrinaD is a livestream talent — her rate is hourly, not per video."
        );
      }
    }
  }

  for (const sopKey of sopKeys) {
    const found = talents.some((t) => t.key === sopKey);
    if (!found) {
      warn(
        `SOP data key "${sopKey}" has a matching talent in config`,
        "This talent has SOP data but is not in config/settings.json talent list."
      );
    }
  }
} else {
  warn("SOP tab name alignment check", "Skipped — sop_data.json unavailable.");
}

// ─── Check 9: Per-talent Make scenario files ──────────────────────────────────

section("Per-Talent Phase 1 Scenario Files");

const scenariosDir = path.join(ROOT, "make", "scenarios");
if (!fs.existsSync(scenariosDir)) {
  fail("make/scenarios/ directory exists");
} else {
  for (const talent of talents) {
    const fileName = `phase1_${talent.key}.json`;
    const filePath = path.join(scenariosDir, fileName);
    if (!fs.existsSync(filePath)) {
      fail(
        `make/scenarios/${fileName} exists`,
        `Scenario file missing for talent "${talent.key}". Create by cloning phase1_triage_scenario.json.`
      );
    } else {
      const raw = fs.readFileSync(filePath, "utf8");
      if (!raw.includes(talent.key)) {
        warn(
          `make/scenarios/${fileName} contains talent key "${talent.key}"`,
          "The scenario file may not have been updated from the template."
        );
      } else {
        ok(`make/scenarios/${fileName} — present and contains talent key`);
      }
    }
  }
}

// ─── Check 10: Required prompt files ─────────────────────────────────────────

section("Prompt Files");

const REQUIRED_PROMPTS = ["prompts/triage.md", "prompts/reply.md"];

for (const pFile of REQUIRED_PROMPTS) {
  const fPath = path.join(ROOT, pFile);
  if (!fs.existsSync(fPath)) {
    fail(`${pFile} exists`);
  } else {
    const content = fs.readFileSync(fPath, "utf8");
    if (content.trim().length < 100) {
      warn(`${pFile} has substantial content`, "File appears nearly empty.");
    } else {
      ok(`${pFile} — present (${content.length} chars)`);
    }
  }
}

// ─── Check 11: SOP template compliance ───────────────────────────────────────

section("SOP Matrix Template Columns");

const SOP_TEMPLATE_PATH = path.join(ROOT, "sheets", "sop_matrix_template.csv");
if (!fs.existsSync(SOP_TEMPLATE_PATH)) {
  fail("sheets/sop_matrix_template.csv exists");
} else {
  const csv = fs.readFileSync(SOP_TEMPLATE_PATH, "utf8");
  const headerRow = csv.split("\n")[0];
  const REQUIRED_COLUMNS = [
    "Trigger / Scenario", "Response / Action",
  ];
  for (const col of REQUIRED_COLUMNS) {
    if (!headerRow.includes(col)) {
      fail(`SOP template has column "${col}"`, `Column missing from sheets/sop_matrix_template.csv header.`);
    } else {
      ok(`SOP template column "${col}" present`);
    }
  }
}

// ─── Check 12: Master log template columns ────────────────────────────────────

section("Master Log Template Columns");

const LOG_TEMPLATE_PATH = path.join(ROOT, "sheets", "master_log_template.csv");
if (!fs.existsSync(LOG_TEMPLATE_PATH)) {
  fail("sheets/master_log_template.csv exists");
} else {
  const csv = fs.readFileSync(LOG_TEMPLATE_PATH, "utf8");
  const headerRow = csv.split("\n")[0];
  const REQUIRED_COLUMNS = [
    "Timestamp", "Talent Name", "Sender Email", "Sender Domain", "Subject",
    "AI Score", "AI Score Label", "Offer Type", "Brand Name", "Proposed Rate (USD)",
    "Action Taken", "Reply Sent", "Gmail Thread Link", "Notes",
  ];
  for (const col of REQUIRED_COLUMNS) {
    if (!headerRow.includes(col)) {
      fail(`Master log template has column "${col}"`, `Column missing from sheets/master_log_template.csv.`);
    } else {
      ok(`Master log column "${col}" present`);
    }
  }
}

// ─── Summary ──────────────────────────────────────────────────────────────────

console.log("\n" + "=".repeat(60));
console.log("  PREFLIGHT VALIDATOR SUMMARY");
console.log("=".repeat(60));
console.log(`  Passed  : ${passed}`);
console.log(`  Failed  : ${failed}`);
console.log(`  Warnings: ${warnings.length}`);
console.log("=".repeat(60));

if (failed > 0) {
  console.log("\nPREFLIGHT FAILED — Do not activate any Make scenarios until all");
  console.log("failures above are resolved. Warnings are safe to proceed with.");
  console.log("");
  process.exit(1);
} else if (warnings.length > 0) {
  console.log("\nPREFLIGHT PASSED with warnings.");
  console.log("Review warnings before activating. They will not block the system");
  console.log("but may cause unexpected behavior in edge cases.");
  console.log("");
  process.exit(0);
} else {
  console.log("\nPREFLIGHT PASSED — All checks green. Safe to activate Phase 1.");
  console.log("");
  process.exit(0);
}
