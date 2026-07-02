Talent Email AI Guidelines
Part 1 — Global Workflow Rules

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

3. Draft Creation Restriction

A draft may be created ONLY when Classification = Approved Response.

If Classification = Ignore or Human Admin Required:

- do not create a draft
- do not create an email body
- do not place the reason into a draft
- do not send anything
- leave the email in INBOX
- apply no labels

Reasons, classifications, and internal notes are automation metadata only.
They must never be used as customer-facing email content. 4. Talent Matching

Talent matching is mandatory.

- Each talent has different rates, terms, and response language.
- Always identify the correct talent before selecting a response.
- Never use one talent’s response for another talent.

5. Initial Inbound Emails Only

This workflow is for initial inbound emails only.

Eligible initial inbound emails are determined by the automation trigger conditions before this workflow runs.

As an additional safeguard, the workflow should only process emails where:

- Gmail thread message count = 1

If Gmail thread message count is greater than 1:

- Classification: Human Admin Required
- Apply Option B under Rule 12

6. Default to Initial Approved Response

Each talent has an Initial Approved Response.

- Treat the Initial Approved Response as the default response for valid inbound opportunities.
- Only choose another approved response if the email clearly matches a more specific scenario.
- Only avoid the Initial Approved Response if the email matches a no-draft rule, such as Event Invite, Personal Email, Human Admin Required, or workflow ineligibility.

7. Spam Handling

Spam handling is managed by Google/Gmail, not by this automation.

- Do not classify emails as Spam.
- Do not move emails to Spam.
- Do not apply a Spam label.
- Do not trash or delete emails.
- If an email reaches this workflow, process it according to the normal workflow rules.

8. Event / Appearance / Speaking Invite Emails

Only classify an email as an event invite when the primary request is for the talent to attend, appear at, travel to, or speak at an event.

Use when the email clearly asks the talent to:

- attend an in-person or virtual event
- make an appearance at an event
- travel for an event, trip, activation, launch, panel, summit, conference, dinner, meetup, premiere, festival, or workshop
- speak, teach, moderate, judge, present, or participate in a panel, masterclass, webinar, summit, or workshop

Do NOT use this rule for normal brand partnership or content collaboration inquiries.

The words “collab,” “collaboration,” “campaign,” “partnership,” “creator,” “brand,” “TikTok,” “Instagram,” “content,” or “UGC” do NOT make an email an event invite by themselves.

If the email asks about sponsored content, paid deliverables, gifted product, rates, media kit, posts, videos, TikToks, Reels, Stories, UGC, usage rights, whitelisting, or campaign deliverables, it should be treated as a brand partnership inquiry unless there is a clear event attendance/speaking request.

If uncertain, do NOT classify as Event Invite.
Continue to response matching and use Scenario A by default.

If this rule applies, classify the email as Ignore. 9. Talent Personal Email Handling

Each talent may include a Scenario C containing one or more personal email addresses.

If the inbound sender email matches any email listed under Scenario C for the matched talent, classify the email as Ignore.

These emails are typically forwarded opportunities or conversations originally sent directly to the talent instead of the business inbox. 10. Repeat Client Handling

Some repeat clients should be ignored by this workflow because they are handled manually by the team.

If the inbound sender email domain matches any domain listed under Repeat Client Domains, classify the email as Ignore.

Repeat Client Domains:

- taboost.me
- favored.live
- nextwave-talent.com

If this rule applies, classify the email as Ignore.

Operational handling is controlled by Rule 12: Inbox Handling After Classification. 11. Formatting, Hyperlinks, and Internal Instructions

Approved responses may contain formatting markup and internal routing instructions.

Approved SOP formatting:

- Bold: **text**
- Emphasis: **_text_**
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
- Render **_text_** as bold and italicized.
- Render hyperlinks correctly.
- If formatting cannot be rendered, remove markup and render the plain text only.

12. Inbox Handling After Classification

This workflow applies only to eligible initial inbound emails currently in the INBOX.

Eligible emails are determined by the automation trigger conditions before this workflow runs.

Every processed email must result in exactly ONE of the following outcomes:

Option A — Draft Created
Option B — No Draft / Human Review

Classification-to-action mapping:

- Classification = Approved Response → Option A — Draft Created
- Classification = Ignore → Option B — No Draft / Human Review
- Classification = Human Admin Required → Option B — No Draft / Human Review

Operational actions are controlled only by Rule 12. Other rules and scenarios determine classification only.

These actions are mutually exclusive. Only one option may be applied per email.

No labels may be created, applied, inferred, or modified except the explicitly approved label:
A Initial Response

---

Option A — Draft Created
Use when:

- an approved response is matched
- an email draft is generated

Action at draft creation:

- Draft Created: Yes
- Remove INBOX Label: Yes
- Apply Label: None

Important:

- The INBOX label should be removed when an approved response draft is created.
- Do not apply A Initial Response at draft creation.
- A Initial Response may only be applied after the draft is successfully sent.
- No other labels may be applied.
  Option B — No Draft / Human Review
  Use when:
- no draft is generated
- the email is ignored
- the email requires human review
- the email is an event / appearance / speaking invite
- the email originated from the talent’s personal email
- the email should remain visible for staff review

Action:

- Draft Created: No
- Remove INBOX Label: No
- Apply Label: None
- Leave email in INBOX exactly as is

Important:

- Do not archive, relabel, trash, move, or modify these emails.
- Do not apply any other label.
- Leave the email untouched in the Inbox.

13. Required Output Format

The Required Output Format is automation metadata only and must never be used as the email draft body.

Every processed email must clearly state:

Classification: Approved Response / Ignore / Human Admin Required
Draft Created: Yes / No
Send Draft: Yes / No
Talent: [talent name, if applicable]
Matched Scenario: [A / C / Event Invite / Repeat Client / None]
Internal Reason: [internal only, never draft body]
Email Body: [only include when Classification = Approved Response]
Remove INBOX Label: Yes / No
Apply Label at Draft Creation: None
Apply Label After Successful Send: A Initial Response / None

Email Body must be blank unless Classification = Approved Response.
A Initial Response must only be applied after the draft is successfully sent.

Part 2 — Approved Response Matching

14. Response Matching Hierarchy

When selecting an approved response:

1. Apply all Global no-draft rules first, including Event Invite, Repeat Client, and Personal Email handling.
2. If no no-draft rule applies and the correct talent is identified, use Scenario A: Initial Inbound Default Response.

There should be no “no matching scenario” outcome after the correct talent has been identified.

Only return “no match” if:

- the correct talent cannot be identified
- the email is outside workflow eligibility
- the email matches a global no-draft rule

If the talent is identified and no no-draft rule applies, use Scenario A.
14A. Scenario A — Initial Inbound Default Response
Scenario A is the default and only approved response for eligible initial inbound emails when the correct talent is identified.

Use Scenario A when:

- the email is an eligible initial inbound inquiry
- the correct talent is identified
- no global no-draft rule applies
- Scenario C does not apply

If uncertain, use Scenario A.
14C. Scenario C — Personal Email
Scenario C applies when the sender email matches any personal email listed under that talent’s Scenario C section.

If Scenario C applies:

- classify the email as Ignore
- do not use Scenario A

Operational handling is controlled by Rule 12: Inbox Handling After Classification.

Part 3 — Talent Approved Responses

Talent: Katrina Moore
Key: Katrina
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Katrina
Min Rate: $300 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thanks for reaching out about working with Katrina!
[HERE](https://docs.google.com/spreadsheets/d/1tTl9RfJKWbPmSj0BSK-SxAEYxOv9GTXM7a9DlqFRhSk/) is our full TABOOST Talent roster below for your review.

Please let me know if there are any additional creators you'd like to explore, and I'd be happy to provide their specific rates.
Looking forward to hearing your thoughts!

Scenario C: Personal Email Forward
Personal Email:

- katrinamoore621@gmail.com

Talent: Anastasiya Ray
Key: Anastasiya
Manager: Cara Best <cara@taboost.me>
Gmail: Gmail - Anastasiya
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Anastasiya!! I’m happy to share her rates below:
**1 TikTok** [anastasiya_ray](https://www.tiktok.com/@anastasiya_ray) - $800
**1 TikTok (2nd)** [theraysfinds](https://www.tiktok.com/@theraysfinds) - $800
**1 Instagram** [Reel](https://www.instagram.com/ugcbyanastasiya/) - $750
**1 UGC Video** [Portfolio](https://ugcbyanastasiya.com/) - $1,000 (usage to be negotiated)

Anastasiya's pricing reflects her high-quality, **polished** content with a bestie beauty vibe that feels authentic, relatable, and **brand-elevating**!! Plus she's a UGC expert so she knows how to make videos that convert!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- ugcbyanastasiya@gmail.com
- anastasiyaraytts@gmail.com

Talent: Wesley Barker
Key: Wesley
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Wesley
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Wesley!! I’m happy to share her rates below:
**1 TikTok** [wesleyrbarker](https://www.tiktok.com/@wesleyrbarker) - $750
**1 Instagram** [Reel](https://www.instagram.com/wesleyrbarker/) - $600
**1 UGC Video** - $900 (usage to be negotiated)

Wesley's pricing reflects her strong following across both TikTok and Instagram. She specializes in **tall girl-friendly fashion**, beauty, and lifestyle content, creating relatable recommendations that make her content feel approachable and easy to trust!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- wesleybarkerbookings@gmail.com

Talent: Hana Tanaka
Key: Hana
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Hana
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Hana!! I’m happy to share her rates below:
**1 TikTok** [hanaisfinechina](https://www.tiktok.com/@hanaisfinechina) - $750
**1 Instagram** [Reel](https://www.instagram.com/hanaisfinechina/) - $500
**1 UGC Video** - $900 (usage to be negotiated)

Hana's pricing reflects her ability to create content that feels **genuine** and **unfiltered**. Known for her silly personality and authentic approach, she isn't afraid to show her audience the **real** her while sharing products she genuinely loves and uses in her everyday life.

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- hanaisfinechina@gmail.com

Talent: Jenn Lyles
Key: Jenn
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Jenn
Min Rate: $500 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Jenn!! I’m happy to share her rates below:
**1 TikTok** [jenn_lyles](https://www.tiktok.com/@jenn_lyles) - $500
**1 UGC Video** - $400 (usage to be negotiated)

Jenn's pricing reflects her extremely high **conversion rate** (consistent **$400k+** monthly GMV). She's a TikTok Shop Star who shares relatable, authentic finds with her audience through engaging, trust-first content that drives attention and connection!!

Please let us know **what type of collab you're looking for** + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- jenn@jennlyles.com

Talent: Angela Callisto
Key: Angela
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Angela
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Angela!! I’m happy to share her rates below:
**1 TikTok** [angelacallisto123](https://www.tiktok.com/@angelacallisto123) - $750
**1 Instagram** [Reel](https://www.instagram.com/angelacallisto/) - $500
**1 UGC Video** - $1,000 (usage to be negotiated)

Angela's pricing reflects her extremely high conversion rate (consistent **$450k+** monthly GMV). She's a TikTok Shop Star who specializes in real friend-to-friend recommendations for **fashion & beauty** based on her authenticity!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- angelacallisto123@gmail.com

Talent: Grayson Finks
Key: Grayson
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - Grayson
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Grayson!! I’m happy to share her rates below:
**1 TikTok** [grayson.finks](https://www.tiktok.com/@grayson.finks) - $750
**1 UGC Video** - $400 (usage to be negotiated)

Grayson's pricing reflects her high quality **fashion** content & the effort she puts in to drive conversions (consistent **$60k+** monthly GMV)!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- graysonfinks@gmail.com

Talent: Kylika Miller
Key: Kylika
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - Kylika
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thanks for reaching out about working with Kylika!
[HERE](https://docs.google.com/spreadsheets/d/1tTl9RfJKWbPmSj0BSK-SxAEYxOv9GTXM7a9DlqFRhSk/) is our full TABOOST Talent roster below for your review.

Please let me know if there are any additional creators you'd like to explore, and I'd be happy to provide their specific rates.
Looking forward to hearing your thoughts!
Scenario C: Personal Email Forward
Personal Email:

- kylikacollabs@gmail.com

Talent: Audur Banks
Key: Audur
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - Audur
Min Rate: $800 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Audur!! I’m happy to share her rates below:
**1 TikTok** [thatnordicblonde](https://www.tiktok.com/@thatnordicblonde) - $800
**1 TikTok (2nd)** [everydayaudur](https://www.tiktok.com/@everydayaudur) - $500
**1 Instagram** [Reel](https://www.instagram.com/thatnordicblonde/) - $500
**1 UGC Video** - $1,000 (usage to be negotiated)

Audur's pricing reflects her high quality content + the access you'll get to the community of buyers she's built from her **beauty & personal care** recommendations on TikTok Shop!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- thebanksedit@gmail.com

Talent: Skyler Clark
Key: Skyler
Manager: Marco Perez <marco@taboost.me>
Gmail: Gmail - Skyler
Min Rate: $300 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Skyler!! I’m happy to share her rates below:
**1 TikTok** [skylerclarkk](https://www.tiktok.com/@skylerclarkk) - $500
**1 Instagram** [Reel](https://www.instagram.com/crashingskymusic/) - $300

Skyler’s pricing reflects her high quality content + the access you'll get to the community of music fans on TikTok!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Email:

- crashingskydrummer@gmail.com

Talent: Stephanie Stimson
Key: Stephanie
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - Stephanie
Min Rate: $500 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Stephanie!! I’m happy to share her rates below:
**1 TikTok** [stephanie_stimson](https://www.tiktok.com/@stephanie_stimson) - $750
**1 Instagram** [Reel](https://www.instagram.com/stephaniestimson_) - $550
**1 UGC Video** - $600 (usage to be negotiated)

Stephanie's pricing reflects her authentic and relatable approach that allows her recommendations to feel natural, making her a trusted voice among her audience. She is known for her beauty content and practical everyday finds that make life a little easier!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- collaboratewithsteph@gmail.com
- stephaniestimson9@gmail.com

Talent: Jocelyn Chardon
Key: Jocelyn
Manager: Cara Best <cara@taboost.me>
Gmail: Gmail - Jocelyn
Min Rate: $800 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Jocelyn!! I’m happy to share her rates below:
**1 TikTok** [ohsoitsjocelyn](https://www.tiktok.com/@ohsoitsjocelyn) - $850
**1 Instagram** [Reel](https://www.instagram.com/ohsoitsjocelyn/) - $700
**1 UGC Video** - $1,000 (usage to be negotiated)

Jocelyn creates eye-catching fashion content that keeps her audience engaged and inspired. Her pricing reflects her strong following and ability to drive conversions!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- jocelynsagec@gmail.com

Talent: Brittanie Hammer
Key: Brittanie
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Brittanie
Min Rate: $1000 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Britt!! I’m happy to share her rates below:
**1 TikTok** [bestiebriitt](https://www.tiktok.com/@bestiebriitt) - $1,500
**1 UGC Video** - $1,000 (usage to be negotiated)

Britt's pricing reflects her extremely high **conversion rate** from content that truly sells. Her last month GMV was **$669k** & she was TikTok’s 2025 Home Creator of the Year!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- hammer.brittanie@gmail.com

Talent: Lizz Freixas
Key: Lizz
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Lizz
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Lizz!! I’m happy to share her rates below:
**1 TikTok** [lizzmi45](https://www.tiktok.com/@lizzmi45) - $750
**1 UGC Video** - $900 (usage to be negotiated)

Lizz's pricing reflects her extremely high **conversion rate** from content that truly sells. Her monthly GMV is **$550k+** and she is an expert at directing her loyal followers/buyers to the right fashion products. Lizz's engagement rate is also super **high** for a shop creator which is ideal for brand collabs.

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- lizzmilenafg45@gmail.com

Talent: Allee Baray
Key: Allee
Manager: Chenni Li <chenni@taboost.me>
Gmail: Gmail - Allee
Min Rate: $500 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Allee!! I’m happy to share her rates below:
**1 TikTok** [ababyandabulldog](https://www.tiktok.com/@ababyandabulldog) - $850
**1 TikTok** [shopaholicallee](https://www.tiktok.com/@shopaholicallee) - $700
**1 TikTok** [shopaholicallee2](https://www.tiktok.com/@shopaholicallee2) - $500
**1 UGC Video** - $800 (usage to be negotiated)

Allee's pricing reflects her extremely high **conversion rate** from content that truly sells. Her last month GMV was **$490k+** & that was just her main account. She is a TikTok Shop Star and has a great pulse on what her viewers are wanting to buy!!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- alleebaray@gmail.com

Talent: Alana Calviello
Key: Alana
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - Alana
Min Rate: $500 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Alana!! I’m happy to share her rates below:
**1 TikTok** [\_alanacalvs](https://www.tiktok.com/@_alanacalvs) - $750
**1 Instagram** [Reel](https://www.instagram.com/alanacalviello/) - $500
**1 UGC Video** - $500 (usage to be negotiated)

Alana's pricing reflects her high quality content & the effort she puts in to drive conversions (consistent **$250k+** monthly GMV). She is strong in the **fashion** category but has sales across beauty & health/wellness too!

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- arcalviello@gmail.com

Talent: Mahogany Lox
Key: Mahogany
Manager: Cara Best <cara@taboost.me>
Gmail: Gmail - Mahogany
Min Rate: $1000 per video
Auto Send: no
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Mahogany!! I’m happy to share her rates below:
**1 TikTok** [mahoganylox](https://www.tiktok.com/@mahoganylox) - $1,000
**1 Instagram** [Reel](https://www.instagram.com/mahoganylox) - $2,500
**1 UGC Video** - $1,500 (usage to be negotiated)

Mahogany's pricing reflects her impressive social presence, with **7.5M** TikTok and **1.2M** Instagram followers. She creates beauty, fashion, and lifestyle content with a strong focus on personality and **self-expression**, allowing her to seamlessly connect with her audience across platforms.

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!

Scenario C: Personal Email Forward
Personal Emails:

- bookmahoganylox@gmail.com
- booking@494ent.com
- booking@four9four.com

Talent: Brittany Kuhl
Key: BKuhl
Manager: Nicole Park <nicole@taboost.me>
Gmail: Gmail - BKuhl
Min Rate: $750 per video
Auto Send: yes
Paused: no

Scenario A: Initial Inbound (Default Response)
Approved Response:
Thank you so much for reaching out about a potential partnership with Brittany!! I’m happy to share her rates below:
**1 TikTok** [bkewwwl1507](https://www.tiktok.com/@bkewwwl1507) - $750
**1 UGC Video** - $600 (usage to be negotiated)

Brittany's pricing reflects her proven ability to consistently generate **$150K+ GMV** in monthly sales. As a mom, she shares home, beauty, and everyday lifestyle content through visually compelling, high-converting content.

Please let us know **what type of collab you're looking for** in your offer + if you have any questions moving forward. We’d love to explore working together!
Scenario C: Personal Email Forward
Personal Emails:

- brittanykuhl.tiktok@gmail.com
