from national import claimants as c


def names(raw, kind="individual"):
    return [(p["first"], p["middle"], p["last"], p["suffix"], p["role"]) for p in c.claimants_for(raw, raw, kind)]


def test_people():
    assert names("CHESBROUGH, DANIEL") == [("Daniel", "", "Chesbrough", "", "Former owner")]
    assert names("RICHARD A MIKOL") == [("Richard", "A", "Mikol", "", "Former owner")]
    assert names("HESTON JULIE A") == [("Julie", "A", "Heston", "", "Former owner")]
    assert names("WILLIE JONES JR") == [("Willie", "", "Jones", "Jr", "Former owner")]
    assert names("JOHN SMITH & MARY SMITH")[1] == ("Mary", "", "Smith", "", "Co-owner")
    assert names("SMITH, JOHN AND MARY")[1] == ("Mary", "", "Smith", "", "Co-owner")
    assert names("SARAH GALKO, TRUSTEE, OR SUCCESSOR TRUSTEE , ET AL") == [("Sarah", "", "Galko", "", "Former owner")]
    assert names("ACME HOLDINGS LLC") == []


def test_heirs():
    assert names("UNKNOWN HEIRS OF JANET CASIAS ET AL", "estate") == [("Janet", "", "Casias", "", "Heir of deceased owner")]
    assert names("UNK. HEIRS ETC. OF GEORGE L KISS AKA GEORGE LASZLO KISS", "estate")[0][:3] == ("George", "L", "Kiss")
    assert names("UNK HEIRS NAME(S) UNK UNK HEIRS DEVISEES LEGATEES BENEFICIARIES OF", "estate") == []
