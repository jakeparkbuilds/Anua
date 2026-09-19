from bench.assertions.compare import compare

def test_bool_coercion():
    assert compare("bool", False, "invalid")[0]
    assert compare("bool", True, "yes")[0]
    assert not compare("bool", False, True)[0]

def test_extraction_failure_is_fail_not_pass():
    ok, ev = compare("bool", False, None)
    assert not ok and "extraction failed" in ev

def test_set_overlap():
    assert compare("set_overlap", ["1.1.1.1", "2.2.2.2"], ["2.2.2.2"])[0]
    assert not compare("set_overlap", ["1.1.1.1"], ["9.9.9.9"])[0]
    assert compare("set_overlap", [], [])[0]

def test_date_close():
    assert compare("date_close", "2027-01-01", "2027-01-02T00:00:00Z")[0]
    assert not compare("date_close", "2027-01-01", "2027-03-01")[0]
