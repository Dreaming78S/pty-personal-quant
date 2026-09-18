from quant.utils.codes import board_of


def test_board_of_prefixes():
    assert board_of("600000.SH") == "main"
    assert board_of("000001.SZ") == "main"
    assert board_of("300750.SZ") == "gem"
    assert board_of("301001.SZ") == "gem"
    assert board_of("688981.SH") == "star"
    assert board_of("430047.BJ") == "bse"
