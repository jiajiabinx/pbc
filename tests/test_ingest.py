from pathlib import Path

import pytest

import ingest

SAMPLE = Path(__file__).resolve().parent.parent / "input" / "sample"


@pytest.fixture(scope="module")
def emails(tmp_path_factory):
    adir = tmp_path_factory.mktemp("atts")
    return ingest.load_mailbox(str(SAMPLE / "sample_mailbox.mbox"), str(adir))


def test_email_count(emails):
    assert len(emails) == 15


def test_chronological_order(emails):
    dates = [e.date for e in emails]
    assert dates == sorted(dates)


def test_attachments_extracted(emails):
    names = {a.filename for e in emails for a in e.attachments}
    assert "IMG_2847_wb_recon.jpg" in names
    assert "Customer_Confirmations_Batch1.zip" in names
    for e in emails:
        for a in e.attachments:
            p = Path(a.path)
            assert p.exists() and p.stat().st_size == a.size


def test_threading(emails):
    threads = {e.thread_id for e in emails}
    assert len(threads) == 4


def test_eml_dir_equivalent(tmp_path, emails):
    from_dir = ingest.load_mailbox(str(SAMPLE / "emails"), str(tmp_path))
    assert len(from_dir) == len(emails)
    assert [e.subject for e in from_dir] == [e.subject for e in emails]


def test_pbc_list_parses_30_items():
    items, header = ingest.parse_pbc_list(str(SAMPLE.parent / "PBC_List_FY2026.pdf"))
    assert len(items) == 30
    assert items[0]["item_id"] == "PBC-01"
    assert "2026-06-30" in items[0]["acceptance"]
    by_id = {i["item_id"]: i for i in items}
    assert by_id["PBC-11"]["acceptance"].find("min_customers=10") >= 0
    assert "excel" in by_id["PBC-07"]["expected_docs"]
    assert "Northwind" in header


def test_profile_text():
    text = ingest.load_profile(str(SAMPLE.parent / "Client_Profile.pdf"))
    assert "Cascade Cold Brew" in text
    assert "2026-06-30" in text
