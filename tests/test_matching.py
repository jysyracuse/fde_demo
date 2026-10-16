from arfollow.sync import domain, norm_account_number, norm_name


def test_name_variants_normalise_to_one_key():
    key = norm_name("Harbour Physio Pty Ltd")
    for variant in ("HARBOR PHYSIO PTY. LTD.", "The Harbour Physio", "Harbour Physio (AU)"):
        assert norm_name(variant) == key
    assert norm_name("Kauri Coffee Company") == norm_name("Kauri Coffee Co Limited")


def test_account_number_formats():
    assert norm_account_number("CUS1042") == norm_account_number("CUS-01042") == "CUS-01042"
    assert norm_account_number(None) is None
    assert norm_account_number("ACME-7") is None


def test_domain():
    assert domain("https://www.kauricoffee.co.nz") == domain("ann@kauricoffee.co.nz")
