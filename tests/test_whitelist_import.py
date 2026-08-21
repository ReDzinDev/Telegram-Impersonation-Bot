"""
CSV whitelist import validation (B-3).

Three problems in one handler:
  - upsert_whitelisted_user's return value was discarded, so with the database
    down every write failed and the admin was still told "Imported N user(s)" —
    a silent lie in the documented recovery path for a wiped whitelist
  - user_type came straight from the CSV, so a hand-edited file could forge
    'admin' rows, which remove_stale_admin_whitelist then manages and can prune
  - user_id accepted negatives, which collide with the synthetic negative id
    space /protect allocates for external identities
"""
from src.handlers.commands import (
    _ALLOWED_USER_TYPES,
    _IMPORT_ROW_LIMIT,
    _parse_whitelist_csv,
)

HEADER = "user_id,username,first_name,last_name,user_type,is_bot\n"


def test_valid_row_is_accepted():
    rows, errors, truncated = _parse_whitelist_csv(
        HEADER + "12345,alice,Alice,Smith,manual,false\n")
    assert errors == [] and truncated is False
    assert rows[0]["user_id"] == 12345
    assert rows[0]["first_name"] == "Alice"
    assert rows[0]["user_type"] == "manual"
    assert rows[0]["is_bot"] is False


def test_non_numeric_user_id_is_rejected_with_a_row_reference():
    rows, errors, _ = _parse_whitelist_csv(HEADER + "notanid,a,A,,manual,false\n")
    assert rows == []
    assert len(errors) == 1 and "Row 2" in errors[0]


def test_negative_and_zero_user_ids_are_rejected():
    """Negative ids collide with /protect's synthetic identity space."""
    rows, errors, _ = _parse_whitelist_csv(
        HEADER + "-999999999999,x,X,,manual,false\n0,y,Y,,manual,false\n")
    assert rows == []
    assert len(errors) == 2


def test_forged_user_type_is_rejected_not_silently_accepted():
    rows, errors, _ = _parse_whitelist_csv(HEADER + "111,a,A,,admin,false\n")
    assert rows[0]["user_type"] == "admin"          # 'admin' is legitimate
    rows, errors, _ = _parse_whitelist_csv(HEADER + "111,a,A,,superuser,false\n")
    assert rows == []
    assert len(errors) == 1 and "user_type" in errors[0]


def test_allowed_user_types_match_what_the_code_actually_uses():
    assert _ALLOWED_USER_TYPES == frozenset({"manual", "admin", "protected"})


def test_missing_first_name_falls_back_rather_than_failing():
    rows, errors, _ = _parse_whitelist_csv(HEADER + "222,bob,,,manual,false\n")
    assert rows[0]["first_name"] == "Unknown"
    assert errors == []


def test_is_bot_truthy_spellings_are_parsed():
    rows, _, _ = _parse_whitelist_csv(
        HEADER + "1,a,A,,manual,TRUE\n2,b,B,,manual,yes\n3,c,C,,manual,1\n"
                 "4,d,D,,manual,false\n")
    assert [r["is_bot"] for r in rows] == [True, True, True, False]


def test_row_count_is_capped_and_reported():
    body = "".join(f"{i},u{i},U{i},,manual,false\n" for i in range(1, _IMPORT_ROW_LIMIT + 50))
    rows, _, truncated = _parse_whitelist_csv(HEADER + body)
    assert truncated is True
    assert len(rows) == _IMPORT_ROW_LIMIT


def test_duplicate_user_ids_within_one_file_are_collapsed():
    rows, errors, _ = _parse_whitelist_csv(
        HEADER + "555,a,A,,manual,false\n555,a2,A2,,manual,false\n")
    assert len(rows) == 1
