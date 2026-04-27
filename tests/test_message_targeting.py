from __future__ import annotations

from app.message_targeting import strip_leading_user_mention


def test_strip_leading_user_mention_standard_form():
    user_id, body = strip_leading_user_mention("<@12345> squat 55kg")
    assert user_id == 12345
    assert body == "squat 55kg"


def test_strip_leading_user_mention_nickname_form():
    user_id, body = strip_leading_user_mention(" <@!12345>   leg press 175kg")
    assert user_id == 12345
    assert body == "leg press 175kg"


def test_mentions_later_in_message_do_not_target_lifter():
    user_id, body = strip_leading_user_mention("squat 55kg with <@12345>")
    assert user_id is None
    assert body == "squat 55kg with <@12345>"
