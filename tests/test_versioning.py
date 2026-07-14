import versioning


def test_final_v3_real_supersedes_chain():
    k1 = versioning.semantic_key("AR_Aging.xlsx")
    k2 = versioning.semantic_key("AR_Aging_v2.xlsx")
    k3 = versioning.semantic_key("AR_Aging_Final_v3_REAL.xlsx")
    assert k1 == k2 == k3


def test_paren_counter_and_copy():
    assert (versioning.semantic_key("trial balance (2).xlsx")
            == versioning.semantic_key("Trial Balance.xlsx")
            == versioning.semantic_key("trial_balance_copy.xlsx"))


def test_extension_family_distinguishes():
    assert versioning.semantic_key("recon.pdf") != versioning.semantic_key("recon.xlsx")


def test_dates_are_identity_not_noise():
    a = versioning.semantic_key("Board_Minutes_2025-09-18.pdf")
    b = versioning.semantic_key("Board_Minutes_2025-10-16.pdf")
    assert a != b
    # but a re-send of the same dated doc matches
    assert a == versioning.semantic_key("board minutes 2025-09-18 FINAL.pdf")


def test_different_documents_do_not_collide():
    assert (versioning.semantic_key("FixedAssetRegister_FY26.xlsx")
            != versioning.semantic_key("AR_Aging_YE_2026-06-30.xlsx"))


def test_ext_family():
    assert versioning.ext_family("IMG_2847.jpg") == "image"
    assert versioning.ext_family("batch.ZIP".lower()) == "archive"
    assert versioning.ext_family("memo.docx") == "doc"
