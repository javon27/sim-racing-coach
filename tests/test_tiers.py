from simcoach.tiers import Tier, adjustable_from_setup, tier_adjustable


def test_bronze_adjustable():
    assert tier_adjustable(Tier.BRONZE) == ["traction control", "brake bias", "ABS"]


def test_gold_is_open():
    assert tier_adjustable(Tier.GOLD) is None


def test_adjustable_from_setup_filters_free_settings():
    setup = {
        "A": {"isFreeSetting": True},
        "B": {"isFreeSetting": False},
        "C": {"isFreeSetting": True},
    }
    assert adjustable_from_setup(setup) == ["A", "C"]
