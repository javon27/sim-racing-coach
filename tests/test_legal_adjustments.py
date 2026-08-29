from simcoach.tiers import legal_adjustments


def test_restricted_returns_free_labels():
    setup = {
        "VM_BRAKE_BALANCE": {"stringValue": "50:50", "isFreeSetting": True},
        "VM_ANTILOCKBRAKESYSTEMMAP": {"stringValue": "3", "isFreeSetting": True},
        "VM_REAR_WING": {"stringValue": "6", "isFreeSetting": False},
        "carClass": "GT3",  # metadata ignored
    }
    got = legal_adjustments(setup)
    assert got == ["Brake Balance", "Antilockbrakesystemmap"]


def test_open_setup_returns_none():
    setup = {
        "VM_A": {"stringValue": "1", "isFreeSetting": True},
        "VM_B": {"stringValue": "2", "isFreeSetting": True},
    }
    assert legal_adjustments(setup) is None


def test_nothing_free_returns_none():
    setup = {"VM_A": {"stringValue": "1", "isFreeSetting": False}}
    assert legal_adjustments(setup) is None
