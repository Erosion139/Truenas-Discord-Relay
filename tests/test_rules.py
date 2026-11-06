import pytest

from app.rules import RuleError, evaluate_filters, normalize_rule, safe_webhook_name, split_text

SRC = "111111111111111111"
DST = "222222222222222222"
ALICE = "333333333333333333"
ROLE = "444444444444444444"


def rule(**kw):
    return normalize_rule({"name": "t", "source_channel_ids": [SRC], "destination_channel_ids": [DST], **kw})


def test_normalize_defaults_and_mentions():
    r = rule(author_user_ids=f"<@{ALICE}>, {ALICE}", ping_role_id=f"<@&{ROLE}>")
    assert r["author_user_ids"] == [ALICE]
    assert r["ping_role_id"] == ROLE
    assert r["mode"] == "webhook" and r["enabled"] is True


@pytest.mark.parametrize("bad", [
    {"source_channel_ids": []},
    {"destination_channel_ids": [SRC]},
    {"author_user_ids": ["abc"]},
    {"mode": "carrier-pigeon"},
])
def test_normalize_rejects(bad):
    with pytest.raises(RuleError):
        rule(**bad)


def test_anyone_but_bots_by_default():
    r = rule()
    assert evaluate_filters(r, author_id=1, role_ids=[], is_bot=False, text="hi")[0]
    assert not evaluate_filters(r, author_id=1, role_ids=[], is_bot=True, text="hi")[0]
    assert evaluate_filters(rule(include_bots=True), author_id=1, role_ids=[], is_bot=True, text="hi")[0]


def test_author_and_role_filters():
    r = rule(author_user_ids=[ALICE], author_role_ids=[ROLE])
    assert evaluate_filters(r, author_id=int(ALICE), role_ids=[], is_bot=False, text="")[0]
    assert evaluate_filters(r, author_id=5, role_ids=[int(ROLE)], is_bot=False, text="")[0]
    ok, reason = evaluate_filters(r, author_id=5, role_ids=[], is_bot=False, text="")
    assert not ok and "author" in reason
    # an explicitly listed bot is allowed even with include_bots off
    assert evaluate_filters(r, author_id=ALICE, role_ids=[], is_bot=True, text="")[0]


def test_keywords():
    r = rule(keywords_include="patch\nupdate", keywords_exclude=["test"])
    assert evaluate_filters(r, author_id=1, role_ids=[], is_bot=False, text="New PATCH notes")[0]
    assert not evaluate_filters(r, author_id=1, role_ids=[], is_bot=False, text="hello")[0]
    assert not evaluate_filters(r, author_id=1, role_ids=[], is_bot=False, text="update test")[0]


def test_split_text():
    assert split_text("") == []
    parts = split_text(("line " * 100 + "\n") * 10, 2000)
    assert all(len(p) <= 2000 for p in parts) and len(parts) >= 3
    assert split_text("x" * 4500) == ["x" * 2000, "x" * 2000, "x" * 500]


def test_webhook_name():
    assert safe_webhook_name("Discord Mod • Clyde HQ") == "Disc0rd Mod • Clyd3 HQ"
    assert safe_webhook_name("") == "Relay"
    assert len(safe_webhook_name("a" * 200)) == 80
