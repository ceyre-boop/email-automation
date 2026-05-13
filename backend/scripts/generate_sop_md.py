"""
Generate sheets/sop.md — the AI's source of truth for all reply drafting.

This file is what GPT reads before writing any response.
Run from repo root: python -m backend.scripts.generate_sop_md

Also called automatically by generate_sop_docx.py so both stay in sync.
"""
from __future__ import annotations
import json, pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOP_JSON  = REPO_ROOT / "sheets" / "sop_data.json"
OUT_MD    = REPO_ROOT / "sheets" / "sop.md"

GLOBAL_RULES = """# Talent Email AI Guidelines

## Global Rules — Mandatory

1. **Follow the SOP explicitly.** Do not deviate from approved responses. Do not rewrite, improve, shorten, expand, or personalize approved responses unless specifically instructed by an admin.

2. **Talent matching is mandatory.** Each talent has different rates, terms, and response language. Always identify the correct talent before selecting a response. Never use one talent's response for another talent.

3. **Initial inbound emails only.** Draft responses only for first-time inbound emails or new deal inquiries. If the email is part of an ongoing thread, follow-up, negotiation, or reply after the initial response, do not draft a response. Instead return:
   - Classification: Human Admin Required
   - Reason: This appears to be a follow-up or ongoing conversation.

4. **Default to the Initial Approved Response.** Each talent has an Initial Approved Response. Use it as the default for all valid inbound opportunities. Only choose another scenario if the email clearly and specifically matches it.

5. **Err on the side of responding.** Only classify as Spam/Trash if the email is clearly and truly spam. If there is any reasonable chance it is a real brand, agency, PR, event, collaboration, gifting, partnership, or paid inquiry — use the Initial Approved Response. Missing a real opportunity is worse than sending an extra reply.

6. **Spam handling must be conservative.** Do not classify as Spam because an email is vague, low-budget, poorly written, or from an unfamiliar sender. Non-English emails (e.g. Chinese market) are NOT spam. Classify Spam only for: phishing, scams, suspicious links, SEO/web/design pitches, fake invoices, malware, adult/illegal content. Known spam senders: Superordinary, Grail, Nextwave.

7. **Output must clearly state the action.** Use exactly one of:
   - `Approved Response`
   - `Human Admin Required`
   - `Spam`
   - `Ignore`

8. **Return approved responses verbatim.** Return the exact approved response only. Do not modify, combine, shorten, expand, or add commentary of any kind.

---
"""

def build_md(sop_data: dict) -> str:
    lines = [GLOBAL_RULES]

    approved = [k for k, v in sop_data.items() if v.get("sop_status") == "approved"]
    pending  = [k for k, v in sop_data.items() if v.get("sop_status") != "approved"]

    lines.append("## SOP Status\n")
    lines.append(f"**✅ AI may draft ({len(approved)}):** {', '.join(sop_data[k].get('full_name', k) for k in approved)}\n")
    lines.append(f"**⏳ Pending — Human Admin Required ({len(pending)}):** {', '.join(sop_data[k].get('full_name', k) for k in pending)}\n")
    lines.append("\n---\n")

    for key, talent in sop_data.items():
        full_name    = talent.get("full_name", key)
        manager      = talent.get("manager", "")
        manager_email= talent.get("manager_email", "")
        status       = talent.get("sop_status", "pending")
        rules        = talent.get("rules", [])
        mgr_str      = f"{manager} ({manager_email})" if manager_email else manager

        lines.append(f"## Talent: {full_name}\n")
        lines.append(f"**Manager:** {mgr_str}  \n")
        lines.append(f"**SOP Status:** {'✅ APPROVED' if status == 'approved' else '⏳ PENDING — do not draft, return Human Admin Required'}\n\n")

        if status != "approved":
            lines.append("---\n")
            continue

        # New scenario format
        if rules and "scenario" in rules[0]:
            for rule in rules:
                scenario   = rule.get("scenario", "")
                label      = rule.get("label", "")
                is_default = rule.get("is_default", False)
                use_when   = rule.get("use_when", [])
                dont_use   = rule.get("do_not_use_when", [])
                cc         = rule.get("cc")
                response   = rule.get("response", "").strip()

                default_tag = " ⭐ DEFAULT" if is_default else ""
                lines.append(f"### Scenario {scenario}: {label}{default_tag}\n")

                if use_when:
                    lines.append("**Use when:**")
                    for u in use_when:
                        lines.append(f"- {u}")
                    lines.append("")

                if dont_use:
                    lines.append("**Do not use when:**")
                    for d in dont_use:
                        lines.append(f"- {d}")
                    lines.append("")

                if cc:
                    lines.append(f"**CC:** {cc}\n")

                lines.append("**Approved Response:**")
                lines.append("```")
                lines.append(response)
                lines.append("```")
                lines.append("")

        lines.append("---\n")

    return "\n".join(lines)


def main():
    sop_data = json.loads(SOP_JSON.read_text(encoding="utf-8"))
    md = build_md(sop_data)
    OUT_MD.write_text(md, encoding="utf-8")
    approved = sum(1 for v in sop_data.values() if v.get("sop_status") == "approved")
    print(f"✓ SOP markdown written to {OUT_MD}")
    print(f"  Talents: {len(sop_data)} total, {approved} approved")


if __name__ == "__main__":
    main()
