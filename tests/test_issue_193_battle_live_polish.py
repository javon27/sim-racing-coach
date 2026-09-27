"""#193 (#186 red-team round 3): get_battle live polish. A: a 'P4' rival is the live P4
car whenever it is not the lap's P4 car -- not only once the driver himself has moved.
B: a rival whose class place changed since the lap is tagged '(live)'."""
import pytest

from tests.test_issue_186_live_position import _battle, _ctx, _live, _snap_lap3

AT_LAP_3 = ["A", "B", "You", "C", "D"]


def _pair(tmp_path, order):
    battle = _battle(without=())
    return (_ctx(tmp_path, _live(order), _snap_lap3(battle)),
            _ctx(tmp_path, None, _snap_lap3(battle)))


@pytest.mark.parametrize("rival", ["P4", "p4", "the P4"])
def test_his_place_held_a_swap_behind_p4_is_the_live_p4_car(tmp_path, rival):
    """D passed C behind him; his P3 held. 'P4' is D now, never C (P5 now) labelled
    'the car you named'."""
    live_ctx, _ = _pair(tmp_path, ["A", "B", "You", "D", "C"])
    out = live_ctx.dispatch("get_battle", {"rival": rival})
    assert out.startswith("As of lap 3.\nRival (P4 in class now (live)): D (D Entry), "
                          "P4 in class.\n")
    assert "C (C Entry)" not in out and "now (live);" not in out
    assert out == live_ctx.dispatch("get_battle", {"position": 4})


@pytest.mark.parametrize("order", [AT_LAP_3, ["B", "A", "You", "C", "D"]],
                         ids=["nothing-moved", "swap-ahead-p4-same-car"])
@pytest.mark.parametrize("rival", ["P4", "p4", "the P4"])
def test_the_same_p4_car_is_byte_identical_to_no_live_frame(tmp_path, order, rival):
    live_ctx, plain_ctx = _pair(tmp_path, order)
    out = live_ctx.dispatch("get_battle", {"rival": rival})
    assert out == plain_ctx.dispatch("get_battle", {"rival": rival})
    assert "Rival (the car you named): C (C Entry), P4 in class.\n" in out


def test_a_reused_mid_slot_at_p1_is_not_the_laps_p1_car(tmp_path):
    """A left and E took A's mID slot at P1; his P3 held. The lap's P1 (A) is not E:
    matched by mID AND entry, so A's timing is never stated for 'P1'."""
    live_ctx, _ = _pair(tmp_path, ["E", "B", "You", "C", "D"])
    for args in ({"rival": "P1"}, {"position": 1}):
        assert live_ctx.dispatch("get_battle", args) == (
            "As of lap 3. Rival (P1 in class now (live)): E (E Entry), P1 in class, was "
            "not timed on lap 3: it is not in that lap's field."), args


def test_his_place_held_a_rival_who_moved_is_tagged_live(tmp_path):
    """The issue's example: B passed A ahead of him. A is P2 now, said in the present."""
    live_ctx, _ = _pair(tmp_path, ["B", "A", "You", "C", "D"])
    assert ("As of lap 3.\nRival (the car you named): A (A Entry), P2 in class now (live).\n"
            in live_ctx.dispatch("get_battle", {"rival": "A"}))
    # C held P4 while others swapped: no tag.
    assert ("Rival (the car you named): C (C Entry), P4 in class.\n"
            in live_ctx.dispatch("get_battle", {"rival": "C"}))


@pytest.mark.parametrize("args", [{}, {"rival": "A"}, {"rival": "C"}, {"rival": "Zed"},
                                  {"direction": "ahead"}, {"position": 4}])
def test_nothing_moved_no_live_tag_byte_identical(tmp_path, args):
    live_ctx, plain_ctx = _pair(tmp_path, AT_LAP_3)
    out = live_ctx.dispatch("get_battle", args)
    assert out == plain_ctx.dispatch("get_battle", args)
    assert "(live)" not in out
