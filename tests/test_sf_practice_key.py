from utils.sf_practice_key import practice_key


def test_space_vs_tight_hyphen_after_store_number():
    a = "2067 - Bessemer, AL"
    b = "2067- Bessemer, AL"
    assert practice_key(a) == practice_key(b) == "2067 bessemer al"


def test_unicode_dashes_fold_like_ascii_hyphen():
    # EM DASH, MINUS SIGN, FULLWIDTH HYPHEN — previously broke matching vs ASCII hyphen-minus.
    for sep in ("\u2014", "\u2212", "\uff0d"):
        a = f"2067{sep} Bessemer, AL"
        b = "2067 - Bessemer, AL"
        assert practice_key(a) == practice_key(b)
