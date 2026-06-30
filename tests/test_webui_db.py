"""Tests for the web-dashboard data layer: the member/role mirror, the unified
audit log, the ``audit_live`` gate (so a startup backfill doesn't flood the
log), and the audited edit helpers the dashboard calls.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.db import Database


@dataclass
class _Lift:
    equipment: str
    weight_kg: float
    bodyweight_add: bool = False
    raw: str = ""
    reps: int | None = None


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def _seed_guild(db):
    db.set_guild_meta(1, "Gym Server", 2)
    db.sync_guild_roles(1, [
        {"id": 10, "name": "Admin", "color": 0xFF0000, "position": 5,
         "managed": False},
        {"id": 11, "name": "Member", "color": 0, "position": 1,
         "managed": False},
    ])
    db.upsert_member(1, 100, "alice", "Alice", present=True,
                     avatar="https://cdn.example/100.png")
    db.upsert_member(1, 200, "bob", "Bob", present=True)
    db.set_member_roles(1, 100, [10, 11])
    db.set_member_roles(1, 200, [11])


# ---- mirror ---------------------------------------------------------------

def test_roles_carry_member_counts(db):
    _seed_guild(db)
    roles = {r["name"]: r["members"] for r in db.list_guild_roles(1)}
    assert roles == {"Admin": 1, "Member": 2}


def test_members_list_includes_role_count(db):
    _seed_guild(db)
    members = {m["display_name"]: m["role_count"] for m in db.list_members(1)}
    assert members == {"Alice": 2, "Bob": 1}


def test_set_member_roles_replaces_wholesale(db):
    _seed_guild(db)
    db.set_member_roles(1, 100, [11])  # drop Admin
    names = [r["name"] for r in db.member_role_names(1, 100)]
    assert names == ["Member"]
    assert {r["name"]: r["members"] for r in db.list_guild_roles(1)}["Admin"] == 0


def test_members_with_role(db):
    _seed_guild(db)
    holders = {m["display_name"] for m in db.members_with_role(1, 11)}
    assert holders == {"Alice", "Bob"}


def test_avatar_stored_and_surfaced(db):
    _seed_guild(db)
    # list_members and members_with_role carry the avatar URL.
    alice = next(m for m in db.list_members(1) if m["display_name"] == "Alice")
    assert alice["avatar"] == "https://cdn.example/100.png"
    holder = next(m for m in db.members_with_role(1, 10))
    assert holder["avatar"] == "https://cdn.example/100.png"
    # An update without an avatar must not wipe the stored one (COALESCE).
    db.upsert_member(1, 100, "alice", "Alice2", present=True)
    again = next(m for m in db.list_members(1) if m["user_id"] == 100)
    assert again["avatar"] == "https://cdn.example/100.png"


def test_audit_join_carries_subject_avatar(db):
    _seed_guild(db)
    db.add_audit(1, "role", "role_add", subject_id=100, subject_name="Alice")
    row = db.list_audit(1, category="role")[0]
    assert row["subject_avatar"] == "https://cdn.example/100.png"


def test_delete_role_removes_edges(db):
    _seed_guild(db)
    db.delete_role(1, 10)
    assert all(r["name"] != "Admin" for r in db.list_guild_roles(1))
    assert [r["name"] for r in db.member_role_names(1, 100)] == ["Member"]


def test_absent_member_kept_for_history(db):
    _seed_guild(db)
    db.set_member_present(1, 200, False)
    rows = {m["display_name"]: m["present"] for m in db.list_members(1)}
    assert rows["Bob"] == 0
    # Still excluded when asked for present-only.
    present = [m["display_name"] for m in db.list_members(1, include_absent=False)]
    assert present == ["Alice"]


def test_list_guilds_unions_sources(db):
    db.set_guild_meta(1, "Named", 1)
    db.add_lifts(2, 100, "u100", [_Lift("bench", 100)], message_id=1)
    ids = {r["guild_id"]: r["name"] for r in db.list_guilds()}
    assert ids[1] == "Named"
    assert 2 in ids  # appears via lifts even without metadata


# ---- audit log ------------------------------------------------------------

def test_add_and_filter_audit(db):
    db.add_audit(1, "role", "role_add", subject_id=100,
                 subject_name="Alice", detail="gained Admin")
    db.add_audit(1, "member", "join", subject_id=200, subject_name="Bob")
    db.add_audit(1, "data", "lift_add", subject_id=100, subject_name="Alice")

    assert db.count_audit(1) == 3
    assert db.count_audit(1, category="role") == 1
    assert db.count_audit(1, subject_id=100) == 2
    roles = db.list_audit(1, category="role")
    assert len(roles) == 1 and roles[0]["detail"] == "gained Admin"


def test_audit_newest_first(db):
    for i in range(3):
        db.add_audit(1, "data", f"a{i}", subject_id=1)
    actions = [r["action"] for r in db.list_audit(1)]
    assert actions == ["a2", "a1", "a0"]


# ---- audit_live gate ------------------------------------------------------

def test_data_changes_not_audited_during_backfill(db):
    # audit_live defaults False: a backfill import logs nothing.
    db.calorie_add(1, 100, "Alice", 500)
    db.protein_add(1, 100, "Alice", 40)
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100)], message_id=1)
    assert db.count_audit(1, category="data") == 0


def test_data_changes_audited_when_live(db):
    db.audit_live = True
    db.calorie_add(1, 100, "Alice", 500, note="lunch")
    db.protein_add(1, 100, "Alice", 40)
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100)], message_id=1)
    assert db.count_audit(1, category="data") == 3
    actions = {r["action"] for r in db.list_audit(1, category="data")}
    assert actions == {"calorie_add", "protein_add", "lift_add"}


# ---- audited edit helpers -------------------------------------------------

def test_web_delete_lift_audits(db):
    ids = db.add_lifts_returning_ids(1, 100, "Alice", [_Lift("bench", 100)])
    assert db.web_delete_lift(1, ids[0], "web:1.2.3.4") is True
    # Row gone, deletion audited and attributed to the web actor.
    assert db.web_list_lifts(1) == []
    row = db.list_audit(1, category="data")[0]
    assert row["action"] == "lift_delete"
    assert row["actor_name"] == "web:1.2.3.4"
    assert row["subject_id"] == 100


def test_web_update_lift_audits_change(db):
    ids = db.add_lifts_returning_ids(1, 100, "Alice", [_Lift("bench", 100)])
    ok = db.web_update_lift(
        1, ids[0], weight_kg=110, reps=5, equipment="bench press",
        actor_name="web:op",
    )
    assert ok
    rows = db.web_list_lifts(1)
    assert rows[0]["weight_kg"] == 110 and rows[0]["equipment"] == "bench press"
    assert db.list_audit(1, category="data")[0]["action"] == "lift_edit"


def test_web_delete_missing_is_false(db):
    assert db.web_delete_lift(1, 999, "web:op") is False
    assert db.web_delete_calorie(1, 999, "web:op") is False
    assert db.web_delete_protein(1, 999, "web:op") is False


def test_web_delete_calorie_and_protein(db):
    cid = db.calorie_add(1, 100, "Alice", 500)
    pid = db.protein_add(1, 100, "Alice", 40)
    assert db.web_delete_calorie(1, cid, "web:op") is True
    assert db.web_delete_protein(1, pid, "web:op") is True
    assert db.count_audit(1, category="data") == 2


def test_web_food_set_and_delete_audited(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.web_food_set(
        1, 100, "Alice", name="eggs", display="Eggs", kcal=140,
        protein_g=12, actor_name="web:op",
    )
    foods = db.calorie_food_list(1, 100)
    assert foods[0]["display"] == "Eggs" and foods[0]["protein_g"] == 12
    row = db.list_audit(1, category="data")[0]
    assert row["action"] == "food_set" and "12g protein" in row["detail"]
    # Delete is audited and returns True only when something was removed.
    assert db.web_food_delete(1, 100, "Alice", "eggs", "web:op") is True
    assert db.web_food_delete(1, 100, "Alice", "eggs", "web:op") is False
    assert db.list_audit(1, category="data")[0]["action"] == "food_delete"


def test_leaderboard_orders_by_best(db):
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100)])
    db.add_lifts(1, 200, "Bob", [_Lift("bench", 120)])
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 110)])  # Alice PR
    rows = db.leaderboard(1, "bench")
    assert [(r["username"], r["best"]) for r in rows] == [("Bob", 120), ("Alice", 110)]


def test_reverts_are_audited_with_actor(db):
    db.audit_live = True
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.upsert_member(1, 999, "mod", "Mod", present=True)
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100)])
    cid = db.calorie_add(1, 100, "Alice", 500)
    pid = db.protein_add(1, 100, "Alice", 40)
    # Reaction/command undo paths pass the triggering user as actor_id.
    db.pop_last_for_user(1, 100, actor_id=999)
    db.delete_calorie_entry(1, 100, cid, actor_id=999)
    db.delete_protein_entry(1, 100, pid, actor_id=999)
    actions = [r["action"] for r in db.list_audit(1, category="data")]
    assert "lift_undo" in actions
    assert "calorie_undo" in actions
    assert "protein_undo" in actions
    undo = next(r for r in db.list_audit(1) if r["action"] == "lift_undo")
    # Actor + subject names resolve from the member mirror via the join.
    assert undo["actor_name"] == "Mod"
    assert undo["subject_name"] == "Alice"


def test_goals_and_bodyweight_audited(db):
    db.audit_live = True
    db.goal_set(1, 100, "bench", 120, False)
    db.calorie_goal_set(1, 100, "Alice", 2200)
    db.protein_goal_set(1, 100, "Alice", 180)
    db.set_bodyweight(1, 100, 82.5)
    db.goal_remove(1, 100, "bench")
    db.calorie_goal_remove(1, 100)
    db.protein_goal_remove(1, 100)
    actions = {r["action"] for r in db.list_audit(1, category="data")}
    assert {
        "goal_set", "calorie_goal_set", "protein_goal_set", "bodyweight_log",
        "goal_remove", "calorie_goal_remove", "protein_goal_remove",
    } <= actions


def test_backfill_adds_not_audited_but_goals_during_backfill_skip(db):
    # While audit_live is False (startup backfill), data writes don't audit.
    db.goal_set(1, 100, "bench", 120, False)
    db.set_bodyweight(1, 100, 80)
    assert db.count_audit(1, category="data") == 0


def test_member_overview_aggregates(db):
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100), _Lift("squat", 140)])
    db.calorie_add(1, 100, "Alice", 500)
    db.calorie_add(1, 100, "Alice", 300)
    db.set_bodyweight(1, 100, 82.5)
    ov = db.web_member_overview(1, 100)
    assert ov["lifts"]["n"] == 2 and ov["lifts"]["equip"] == 2
    assert ov["calories"]["total"] == 800
    assert ov["bodyweight"]["weight_kg"] == 82.5


def test_member_overview_nutrition_is_global(db):
    # Calorie/protein totals span every server; lifts stay guild-scoped.
    db.calorie_add(1, 100, "Alice", 500)
    db.calorie_add(2, 100, "Alice", 300)
    db.protein_add(1, 100, "Alice", 40)
    db.protein_add(2, 100, "Alice", 30)
    db.add_lifts(1, 100, "Alice", [_Lift("bench", 100)])
    db.add_lifts(2, 100, "Alice", [_Lift("squat", 140)])
    ov = db.web_member_overview(1, 100)
    assert ov["calories"]["total"] == 800   # both servers
    assert ov["protein"]["total"] == 70     # both servers
    assert ov["lifts"]["n"] == 1            # only guild 1's lift


def test_web_list_calories_global_per_user_but_guild_for_all(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.upsert_member(2, 100, "alice", "Alice", present=True)
    db.upsert_member(2, 200, "bob", "Bob", present=True)
    db.calorie_add(1, 100, "Alice", 500)
    db.calorie_add(2, 100, "Alice", 300)   # logged in another server
    db.calorie_add(2, 200, "Bob", 250)
    # Per-user: every server's entries, regardless of which guild is selected.
    alice = db.web_list_calories(1, user_id=100)
    assert sorted(r["kcal"] for r in alice) == [300, 500]
    # Guild-wide: only entries from members of that guild. Alice (member of
    # guild 1) shows her cross-server entries; Bob (not in guild 1) doesn't.
    g1 = db.web_list_calories(1)
    assert sorted(r["kcal"] for r in g1) == [300, 500]
    g2 = db.web_list_calories(2)
    assert sorted(r["kcal"] for r in g2) == [250, 300, 500]


def test_web_list_protein_global_per_user(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.protein_add(1, 100, "Alice", 40)
    db.protein_add(2, 100, "Alice", 30)
    rows = db.web_list_protein(5, user_id=100)  # any guild id
    assert sorted(r["grams"] for r in rows) == [30, 40]


def test_web_delete_entry_works_across_guilds(db):
    # An entry logged in guild 2 is deletable from guild 1's dashboard, since
    # entries are global; the audit is recorded under the acting guild.
    cid = db.calorie_add(2, 100, "Alice", 500)
    db.audit_live = True
    assert db.web_delete_calorie(1, cid, "web:op") is True
    assert db.web_list_calories(2, user_id=100) == []
    assert db.list_audit(1, category="data")[0]["action"] == "calorie_delete"
