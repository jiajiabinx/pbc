# PBC Work Trial — Sample Set

This bundle contains everything you need to start building.

## Contents

- **PBC_List_FY2026.pdf** — the 30-item PBC list (this is the config; your agent must ingest this dynamically, not hardcode it).
- **Client_Profile.pdf** — entity structure, fiscal year, subsidiaries, base currency.
- **sample/sample_mailbox.mbox** — 15 emails across 4 threads.
- **sample/emails/*.eml** — individual .eml files (same content as the mbox, easier to browse).
- **sample/attachments/** — 8 attachments (PDFs, XLSX, JPG, ZIP).
- **sample/sample_groundtruth.json** — expected per-item statuses so you can self-check. This is the ONLY ground truth we hand you.

## What's NOT here

At the review call, we will hand you a **held-out mailbox** that is ~6x larger, with the same shape and category mix as this sample, but including adversarial cases you have not seen. Your agent must run on it cold, from a clean checkout.

## Reminder of hard constraints

- $2 max per PBC list processed (measured, USD).
- Every status decision must have a full agent trace (plan → tool calls → verifier verdict) defensible to a PCAOB inspector.
- No hardcoded engagement-specific logic. PBC list is the config; we will swap it at the review.
