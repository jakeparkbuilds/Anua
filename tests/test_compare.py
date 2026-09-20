from bench.assertions.compare import compare

def test_bool_coercion():
    assert compare("bool", False, "invalid")[0]
    assert compare("bool", True, "yes")[0]
    assert not compare("bool", False, True)[0]

def test_extraction_failure_is_fail_not_pass():
    ok, ev = compare("bool", False, None)
    assert not ok and "does not state a value" in ev

def test_set_overlap():
    assert compare("set_overlap", ["1.1.1.1", "2.2.2.2"], ["2.2.2.2"])[0]
    assert not compare("set_overlap", ["1.1.1.1"], ["9.9.9.9"])[0]
    assert compare("set_overlap", [], [])[0]

def test_date_close():
    assert compare("date_close", "2027-01-01", "2027-01-02T00:00:00Z")[0]
    assert not compare("date_close", "2027-01-01", "2027-03-01")[0]


def test_set_comparators_treat_fqdn_trailing_dot_as_the_same_name():
    # dnsdoc answered ['dns1.p08.nsone.net.', ...]; the RDAP oracle says ['dns1.p08.nsone.net', ...].
    # Same nameservers, DNS notation. This was a HIGH failure the agent never made.
    ok, ev = compare("set_overlap", ["dns1.p08.nsone.net", "ns-520.awsdns-01.net"], ["DNS1.p08.nsone.net.", "other."])
    assert ok, ev
    assert compare("set_eq", ["ns3.cloudflare.com", "ns4.cloudflare.com"], ["ns4.cloudflare.com.", "NS3.cloudflare.com."])[0]
    assert not compare("set_overlap", ["ns1.a.net"], ["ns1.b.net."])[0]
