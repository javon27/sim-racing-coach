def test_fake_model_records_call():
    from simcoach.model.fake import FakeModel
    m = FakeModel(reply="brake later")
    out = m.complete("sys", [{"role": "user", "content": "hi"}])
    assert out == "brake later"
    assert m.last_system == "sys"
    assert m.last_messages == [{"role": "user", "content": "hi"}]
