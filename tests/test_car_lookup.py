from simcoach.car_lookup import resolve_car


def test_known_entry_resolves_to_model():
    assert resolve_car("Iron Dames 2025 #85:ELMS", "GT3") == "Porsche 911 GT3 R"
    assert resolve_car("Racing Team Turkey by TF 2026 #34:WEC", "GT3") == "Chevrolet Corvette Z06 GT3.R"


def test_unknown_entry_falls_back_to_class():
    assert resolve_car("Some New Team #7:WEC", "GT3") == "GT3 car"


def test_unknown_entry_no_class_returns_raw():
    assert resolve_car("Some New Team #7", None) == "Some New Team #7"
