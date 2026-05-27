# Talent Email AI Guidelines

## Global Rules — Mandatory

1. Workflow Eligibility

This workflow should only initiate for emails currently in the INBOX.

- Only process emails that have the INBOX label at the time the automation runs.
- Do not process emails that are already archived, trashed, marked spam, sent, drafted, or visible only in All Mail.
- If an email does not have the INBOX label, take no action.
- Do not create a draft, send a reply, classify, relabel, archive, or modify non-INBOX emails.

2. SOP Compliance

The SOP document must be followed explicitly.

- Do not deviate from approved responses.
- Do not rewrite, improve, shorten, expand, or personalize approved responses unless specifically instructed by an admin.
- If using an approved response, return the exact approved response only.
- Do not combine multiple approved responses.
- Do not add extra commentary inside the email draft.

3. Talent Matching

Talent matching is mandatory.

- Each talent has different rates, terms, and response language.
- Always identify the correct talent before selecting a response.
- Never use one talent's response for another talent.

4. Initial Inbound Emails Only

This workflow is for INITIAL inbound emails only.

- Draft responses only for first-time inbound emails or new deal inquiries.
- If the email is part of an ongoing thread, follow-up, negotiation, or reply after the initial response, do not draft a response.
- Return:

Classification: Human Admin Required
Reason: This appears to be a follow-up or ongoing conversation.
Draft Sent: No
Remove INBOX Label: Yes
Apply Label: Revisit

5. Default to Initial Approved Response

Each talent has an Initial Approved Response.

- Treat the Initial Approved Response as the default response for valid inbound opportunities.
- Only choose another approved response if the email clearly matches a more specific scenario.
- Only avoid the Initial Approved Response if the email is obvious spam, an event invite, irrelevant, or requires human admin review.

6. Conservative Spam Handling

Err on the side of responding.

- Only classify as Spam if the email is clearly and truly spam.
- Do not classify as Spam merely because the email is vague, low-budget, generic, poorly written, or from an unfamiliar sender.
- Spam indicators include phishing, scams, suspicious links, unrelated service pitches, fake invoices, malware, adult/illegal content, or obvious automated junk.
- If there is any reasonable chance the email is a real brand, agency, PR, collaboration, gifting, partnership, event-related brand inquiry, or paid inquiry, do not mark as Spam.
- If uncertain, use the Initial Approved Response or Human Admin Required.

7. Event / Appearance / Speaking Invite Emails

Any email primarily related to an event, appearance, travel invite, or speaking engagement should be ignored and left in INBOX for human review.

Use when:
- creator is invited to an online or in-person event
- creator is invited to an appearance, meetup, launch, dinner, festival, premiere, brand trip, or social gathering
- creator is offered travel accommodations or lodging related to an event
- creator is invited to be a guest speaker, panelist, workshop host, mentor, judge, moderator, or masterclass participant
- creator is invited to participate in TikTok-hosted events, creator summits, speaking panels, educational sessions, or platform activations
- the primary purpose of the email is attendance, participation, or appearance at an event rather than a paid content campaign

Do not use when:
- the email is primarily about a paid brand partnership or sponsored content deliverable
- an event or speaking engagement is not clearly mentioned
- the creator is being asked to create sponsored social content as the primary deliverable
- the event is secondary to a broader paid campaign discussion

Rules:
- Do not create a draft or reply.
- Do not classify as Spam.
- Do not relabel or archive the email.
- Leave the email in INBOX for human admin review.

Output:
Classification: Ignore
Reason: Event / appearance / speaking invite.
Draft Sent: No
Remove INBOX Label: No
Apply Label: None
Action: Leave in INBOX

8. Talent Personal Email Handling

Each talent may include a Scenario C containing their personal email address.

These emails are typically forwarded opportunities or conversations originally sent directly to the talent instead of the business inbox.

Rules:
- If the inbound sender matches the personal email listed in Scenario C for the matched talent:
 • do not create a draft or reply
 • do not classify as Spam
 • do not relabel or archive the email
 • leave the email in INBOX for human admin review

Output:
Classification: Ignore
Reason: Email originated from talent personal email.
Draft Sent: No
Remove INBOX Label: No
Apply Label: None
Action: Leave in INBOX

9. Formatting, Hyperlinks, and Internal Instructions

Approved responses may contain formatting markup and internal routing instructions.

Approved SOP formatting:
- Bold: **text**
- Emphasis: ***text***
- Hyperlink: [Anchor Text](URL)
- CC instruction: CC: manager@example.com

Rules:
- Preserve all approved response wording exactly.
- Preserve and render all approved formatting.
- Do not add formatting that does not exist in the SOP.

Hyperlink behavior:
- Render [Anchor Text](URL) as a clickable hyperlink.
- Display only the Anchor Text visibly.
- Use the URL inside parentheses as the hyperlink destination.
- Do not display raw URLs in the visible email body.
- Hyperlink only the Anchor Text, never the surrounding sentence or paragraph.

CC behavior:
- CC instructions are internal routing instructions only.
- Do not display CC instructions in the email body.
- Remove the CC line from the drafted email content.
- Place the listed email address only in the CC field.

Formatting behavior:
- Render **text** as bold.
- Render ***text*** as bold and italicized.
- Render hyperlinks correctly.
- If formatting cannot be rendered, remove markup and render the plain text only.

10. Inbox Handling After Classification

Before applying Option A, B, or C, confirm the email is eligible for this workflow.

Rule 10 only applies to new INITIAL inbound emails that are currently in the INBOX.

Do not apply Option A, B, or C to:
- replies
- follow-ups
- ongoing threads
- negotiations
- emails after an initial response has already been sent
- non-INBOX emails

If the email is not a new initial inbound email, do not draft or send a response.
Classify as Human Admin Required and leave the email in INBOX.

Every processed email must result in exactly ONE of the following actions:

- Option A — Approved Response Sent
- Option B — Ignore / Human Review
- Option C — Misc

These actions are mutually exclusive.
Only one option may be applied per email.

---
Option A — Approved Response Sent (Default Action)
This is the default and preferred outcome for valid inbound opportunities.

Use when:
- an approved response is matched
- a draft and/or reply is successfully created or sent
- the email appears to be a legitimate partnership, collaboration, PR, gifting, campaign, or business opportunity

Action:
- Draft Created: Yes
- Remove INBOX Label: Yes
- Apply Label: A Initial Response

Important:
- This should be the most common automation outcome.
- When uncertain between responding or ignoring, prefer Option A.
- It is better to send an initial approved response than to accidentally ignore a legitimate opportunity.

---
Option B — Ignore / Human Review
Use when:
- the email requires human review
- the email is part of an ongoing thread or negotiation
- the email is an event / appearance / speaking invite
- the email originated from the talent's personal email
- the email should not receive an automated response
- the email is intentionally being ignored but should remain visible for staff review

Action:
- Draft Created/Sent: No
- Remove INBOX Label: No
- Apply Label: None
- Leave email in INBOX exactly as is

Important:
- Emails under Option B are NOT spam.
- Do not archive, relabel, trash, or move these emails.
- These emails should remain untouched in the Inbox for manual staff handling.

---
Option C — Misc
This should be rare.

Use ONLY when the email is clearly and unquestionably spam.

Examples include:
- phishing attempts
- malware or suspicious attachments
- fake invoices
- unrelated SEO/web/design service spam
- obvious automated junk
- scams
- adult/illegal content
- malicious or deceptive messages

Action:
- Draft Created/Sent: No
- Remove INBOX Label: Yes (only if spam handling is enabled)
- Apply Label: Misc

Important:
- Spam classification must be extremely conservative.
- If there is any reasonable possibility the email is a legitimate business inquiry, do NOT use Option C.
- When uncertain, prefer Option A or Option B.
- False positives are worse than replying to a questionable email.

11. Required Output Format

Every processed email must clearly state:

Classification: Approved Response / Ignore / Human Admin Required / Spam
Talent: [Talent name, if applicable]
Matched Scenario: [Scenario name, if applicable]
Draft Sent: Yes / No
Remove INBOX Label: Yes / No
Apply Label: A Initial Response / Revisit / Misc / None
CC: [manager email, if applicable]
Response: [exact approved response, if applicable]

---

## Talent: Katrina Moore

**Manager:** Chenni Li (chenni@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Katrina!! I'm happy to share her rates below:
    **1 TikTok** [katrinagmoore](https://www.tiktok.com/@katrinagmoore) - $500
  Cross-posting to **IG Reels** [katrinamoore621](https://www.instagram.com/katrinamoore621/reels/) - +$150
    **1 UGC Video** - $400 (usage to be negotiated)

Katrina's pricing reflects her extremely high **conversion rate**. Her monthly GMV is **$450k+** and she is an expert at directing her loyal followers/buyers to the right fashion products. Katrina has a strong **following** plus great engagement!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Katrina's](https://www.tiktok.com/@katrinagmoore) standard rate is $500 per video! Below is her bundle pricing:

    3 videos (90%) → $1,350
    5 videos (85%) → $2,100
    10 videos (75%) → $3,750

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** katrinamoore621@gmail.com

---

## Talent: Anastasiya Ray

**Manager:** Cara Best (cara@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Anastasiya!! I'm happy to share her rates below:
    **1 TikTok** [anastasiya_ray](https://www.tiktok.com/@anastasiya_ray) - $800
    **1 TikTok (2nd)** [theraysfinds](https://www.tiktok.com/@theraysfinds) - $800
    **1 Instagram** [Reel](https://www.instagram.com/ugcbyanastasiya/) - $750
    **1 UGC Video** [Portfolio](https://ugcbyanastasiya.com/) - $1,000 (usage to be negotiated)

Anastasiya's pricing reflects her high-quality, **polished** content with a bestie beauty vibe that feels authentic, relatable, and **brand-elevating**!! Plus she's a UGC expert so she knows how to make videos that convert!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Anastasiya's](https://www.tiktok.com/@anastasiya_ray) standard rate is $800 per video! Below is her bundle pricing:

    3 videos (90%) → $2,150
    5 videos (85%) → $3,400
    10 videos (75%) → $6,000

We've found bundles usually perform better since multiple posts make the product feel like a real part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** ugcbyanastasiya@gmail.com

---

## Talent: Wesley Barker

**Manager:** Chenni Li (chenni@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Wesley!! I'm happy to share her rates below:
    **1 TikTok** [wesleyrbarker](https://www.tiktok.com/@wesleyrbarker) - $750
    **1 Instagram** [Reel](https://www.instagram.com/wesleyrbarker/) - $500
    **1 UGC Video** - $600 (usage to be negotiated)

Wesley's pricing reflects her high quality content + the access you'll get to the community of buyers she's built from her **fashion & beauty** recommendations on TikTok Shop!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Wesley's](https://www.tiktok.com/@wesleyrbarker) standard rate is $750 per video! Below is her bundle pricing:

    3 videos (90%) → $2,000
    5 videos (85%) → $3,100
    10 videos (75%) → $5,600

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** wesleybarkerbookings@gmail.com

---

## Talent: Hana Tanaka

**Manager:** Chenni Li (chenni@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Hana!! I'm happy to share her rates below:
    **1 TikTok** [hanaisfinechina](https://www.tiktok.com/@hanaisfinechina) - $750
    **1 Instagram** [Reel](https://www.instagram.com/hanaisfinechina/) - $500
    **1 UGC Video** - $600 (usage to be negotiated)

Hana's pricing reflects her high quality content + the access you'll get to the community of buyers she's built from her **fashion & beauty** recommendations on TikTok Shop!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Hana's](https://www.tiktok.com/@hanaisfinechina) standard rate is $750 per video! Below is her bundle pricing:

    3 videos (90%) → $2,000
    5 videos (85%) → $3,100
    10 videos (75%) → $5,600

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** hanaisfinechina@gmail.com

---

## Talent: Jenn Lyles

**Manager:** Chenni Li (chenni@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Jenn!! I'm happy to share her rates below:
    **1 TikTok** [jenn_lyles](https://www.tiktok.com/@jenn_lyles) - $500
    **1 UGC Video** - $400 (usage to be negotiated)

Jenn's pricing reflects her extremely high **conversion rate** (consistent **$400k+** monthly GMV). She's a TikTok Shop Star who shares relatable, authentic finds with her audience through engaging, trust-first content that drives attention and connection!!

Please let us know **what type of collab you're looking for** + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Jenn's](https://www.tiktok.com/@jenn_lyles) standard rate is $500 per video! Below is her bundle pricing:

    3 videos (90%) → $1,350
    5 videos (85%) → $2,100
    10 videos (75%) → $3,750

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** jenn@jennlyles.com

---

## Talent: Angela Callisto

**Manager:** Chenni Li (chenni@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Angela!! I'm happy to share her rates below:
    **1 TikTok** [angelacallisto123](https://www.tiktok.com/@angelacallisto123) - $750
    **1 Instagram** [Reel](https://www.instagram.com/angelacallisto/) - $500
    **1 UGC Video** - $1,000 (usage to be negotiated)

Angela's pricing reflects her extremely high conversion rate (consistent **$450k+** monthly GMV). She's a TikTok Shop Star who specializes in real friend-to-friend recommendations for **fashion & beauty** based on her authenticity!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Angela's](https://www.tiktok.com/@angelacallisto123) standard rate is $750 per video! Below is her bundle pricing:

    3 videos (90%) → $2,000
    5 videos (85%) → $3,100
    10 videos (75%) → $5,600

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** angelacallisto123@gmail.com

---

## Talent: Grayson Finks

**Manager:** Nicole Park (nicole@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Grayson!! I'm happy to share her rates below:
    **1 TikTok** [grayson.finks](https://www.tiktok.com/@grayson.finks) - $750
    **1 UGC Video** - $400 (usage to be negotiated)

Grayson's pricing reflects her high quality **fashion** content & the effort she puts in to drive conversions (consistent **$60k+** monthly GMV)!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Grayson's](https://www.tiktok.com/@grayson.finks) standard rate is $750 per video! Below is her bundle pricing:

    3 videos (90%) → $2,000
    5 videos (85%) → $3,150
    10 videos (75%) → $5,600

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** graysonfinks@gmail.com

---

## Talent: Kylika Miller

**Manager:** Nicole Park (nicole@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Kylika!! I'm happy to share her rates below:
    **1 TikTok** [kylikamiller44](https://www.tiktok.com/@kylikamiller44) - $750
    **1 Instagram** [Reel](https://www.instagram.com/kylikamiller/) - $500
    **1 UGC Video** - $600 (usage to be negotiated)

Kylika's pricing reflects her high quality content + the access you'll get to the community of buyers she's built from her **fashion & beauty** recommendations on TikTok Shop!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Kylika's](https://www.tiktok.com/@kylikamiller44) standard rate is $750 per video! Below is her bundle pricing:

    3 videos (90%) → $2,000
    5 videos (85%) → $3,100
    10 videos (75%) → $5,600

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** kylikacollabs@gmail.com

---

## Talent: Audur Banks

**Manager:** Nicole Park (nicole@taboost.me)

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Audur!! I'm happy to share her rates below:
    **1 TikTok** [thatnordicblonde](https://www.tiktok.com/@thatnordicblonde) - $800
    **1 TikTok (2nd)** [everydayaudur](https://www.tiktok.com/@everydayaudur) - $500
    **1 Instagram** [Reel](https://www.instagram.com/thatnordicblonde/) - $500
    **1 UGC Video** - $1,000 (usage to be negotiated)

Audur's pricing reflects her high quality content + the access you'll get to the community of buyers she's built from her **beauty & personal care** recommendations on TikTok Shop!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!

### Scenario B: Initial Inbound (Bundle Rate Requested)

**Use when:**
- Asking for bundle rates

**Do not use when:**
- Multiple post rate is not asked for

**Approved Response:**
[Audur's](https://www.tiktok.com/@thatnordicblonde) standard rate is $800 per video! Below is her bundle pricing:

    3 videos (90%) → $2,150
    5 videos (85%) → $3,400
    10 videos (75%) → $6,000

We've found bundles usually perform **better** since multiple posts make the product feel like a **real** part of her routine instead of a one-off. Let me know your thoughts!

### Scenario C: Personal Email Forward

**Personal Email:** thebanksedit@gmail.com

---

## Talent: Skyler Clark

**Manager:** Marco Perez

**SOP Status:** ✅ APPROVED

### Scenario A: Initial Inbound (Default Response) ⭐ DEFAULT

**Use when:**
- Asking for rates or a potential to collab
- All other general inquiries

**Do not use when:**
- An exact match from scenario below
- Specifically an event invite only

**Approved Response:**
Thank you so much for reaching out about a potential partnership with Skyler!! I'm happy to share her rates below:
    **1 TikTok** [skylerclarkk](https://www.tiktok.com/@skylerclarkk) - $500
    **1 Instagram** [Reel](https://www.instagram.com/crashingskymusic/) - $300

Skyler's pricing reflects her high quality content + the access you'll get to the community of music fans on TikTok!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We'd love to explore working together!
