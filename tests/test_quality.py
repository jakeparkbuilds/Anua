from bench.assertions.quality import flesch, grounding, actionability, score

def test_flesch_bounds():
    assert 0 <= flesch("The cat sat on the mat. It was warm.") <= 100
    assert flesch("") == 0.0

def test_grounding_detects_invented_values():
    text = "Cert expires 2027-01-01, issued by Let's Encrypt."
    assert grounding(text, ["2027-01-01", "Let's Encrypt"]) == 1.0
    assert grounding(text, ["2099-12-31"]) == 0.0
    assert grounding(text, []) == 1.0

def test_actionability():
    assert actionability("You should renew the certificate. The sky is blue.") == 0.5

def test_score_never_has_pass_fail():
    q = score("Renew it.", ["x"])
    assert q.confidence == "low" and q.method == "classical"
