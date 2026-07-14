"""Ingestion: mailbox → chronological email stream; PBC list PDF → runtime config.

Nothing engagement-specific is hardcoded — the PBC list and client profile are
parsed at runtime and can be swapped at the review.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import mailbox
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


@dataclass
class Attachment:
    filename: str
    path: str          # extracted to disk
    sha256: str
    size: int


@dataclass
class Email:
    email_id: str
    thread_id: str
    from_addr: str
    from_name: str
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str
    date: float        # unix timestamp
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_message(msg: email.message.Message, attachments_dir: Path) -> Email:
    body_parts: list[str] = []
    attachments: list[Attachment] = []

    def addr_list(header: str) -> list[str]:
        raw = msg.get(header, "") or ""
        return [a for _, a in email.utils.getaddresses([raw]) if a]

    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    msg_id = (msg.get("Message-ID") or "").strip()
    date_tuple = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    date_ts = date_tuple.timestamp() if date_tuple else 0.0

    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename:
            payload = part.get_payload(decode=True) or b""
            digest = _sha256(payload)
            safe_name = os.path.basename(filename)
            out_dir = attachments_dir / digest[:12]
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / safe_name
            if not out_path.exists():
                out_path.write_bytes(payload)
            attachments.append(Attachment(safe_name, str(out_path), digest, len(payload)))
        elif part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))

    if not msg_id:
        msg_id = "<synthetic-%s>" % _sha256(
            (msg.get("From", "") + msg.get("Date", "") + msg.get("Subject", "")).encode()
        )[:16]

    # Thread id: root of the References chain, else In-Reply-To, else own id.
    refs = (msg.get("References") or "").split()
    thread_id = refs[0] if refs else (msg.get("In-Reply-To") or msg_id).strip()

    return Email(
        email_id=msg_id,
        thread_id=thread_id,
        from_addr=from_addr,
        from_name=from_name or from_addr,
        to_addrs=addr_list("To"),
        cc_addrs=addr_list("Cc"),
        subject=msg.get("Subject", "") or "",
        date=date_ts,
        body="\n".join(body_parts).strip(),
        attachments=attachments,
    )


def load_mailbox(path: str, attachments_dir: str = "data/attachments") -> list[Email]:
    """Load an .mbox file or a directory of .eml files, sorted chronologically."""
    adir = Path(attachments_dir)
    adir.mkdir(parents=True, exist_ok=True)
    emails: list[Email] = []
    p = Path(path)
    if p.is_dir():
        for f in sorted(p.glob("*.eml")):
            with open(f, "rb") as fh:
                msg = email.message_from_binary_file(fh, policy=email.policy.compat32)
            emails.append(_parse_message(msg, adir))
    else:
        for msg in mailbox.mbox(str(p)):
            emails.append(_parse_message(msg, adir))
    emails.sort(key=lambda e: e.date)
    return emails


# ---------------------------------------------------------------- PBC list PDF

_ITEM_HEADER = re.compile(r"^(?P<id>[A-Z]{2,5}-\d+)\s+\[(?P<cat>[^\]]+)\]\s+Priority:\s*(?P<pri>\w+)")


def pdf_text(path: str) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def parse_pbc_list(path: str, llm_fallback=None) -> tuple[list[dict], str]:
    """Parse the PBC list PDF into item dicts.

    Returns (items, header_text). The format is regex-parsed; if that yields
    almost nothing (a differently formatted list at the review), an optional
    `llm_fallback(text) -> list[dict]` is used instead.
    """
    text = pdf_text(path)
    lines = text.splitlines()
    items: list[dict] = []
    header_lines: list[str] = []
    cur: dict | None = None
    section = None  # description | acceptance | expected

    for raw in lines:
        line = raw.strip()
        m = _ITEM_HEADER.match(line)
        if m:
            if cur:
                items.append(cur)
            cur = {
                "item_id": m.group("id"), "category": m.group("cat"),
                "priority": m.group("pri"), "description": "",
                "acceptance": "", "expected_docs": "",
            }
            section = "description"
            continue
        if cur is None:
            header_lines.append(line)
            continue
        if line.lower().startswith("acceptance:"):
            cur["acceptance"] += (" " if cur["acceptance"] else "") + line[len("Acceptance:"):].strip()
            section = "acceptance"
        elif line.lower().startswith("expected documents:"):
            cur["expected_docs"] = line[len("Expected documents:"):].strip()
            section = "expected"
        elif line.startswith("---") or line.lower().startswith("confidential"):
            continue
        elif line:
            if section == "description":
                cur["description"] += (" " if cur["description"] else "") + line
            elif section == "acceptance":
                cur["acceptance"] += " " + line
            elif section == "expected":
                cur["expected_docs"] += " " + line
    if cur:
        items.append(cur)

    if len(items) < 3 and llm_fallback is not None:
        items = llm_fallback(text)

    return items, "\n".join(header_lines[:20])


def load_profile(path: str) -> str:
    """Client profile is passed to the agent as raw text — nothing hardcoded."""
    return pdf_text(path)
