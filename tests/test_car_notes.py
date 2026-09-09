"""car_notes loader, the shared setup-concepts doc, and the get_car_notes coach tool."""
import json

from simcoach.car_notes import CARS_DIR, car_slug, load_car_notes, load_setup_concepts
from simcoach.coach_tools import TOOLS, CoachContext


def _write(d, slug, obj):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps(obj), encoding="utf-8")


def test_car_slug_matches_legacy_form():
    # Same slug the retired knowledge/<slug>.md loader used.
    assert car_slug("Ferrari 296 LMGT3") == "ferrari_296_lmgt3"
    assert car_slug("Porsche 911 GT3 R LMGT3") == "porsche_911_gt3_r_lmgt3"


def test_load_car_notes_absent_returns_empty(tmp_path):
    assert load_car_notes("No Such Car", cars_dir=tmp_path) == {}
    assert load_car_notes("", cars_dir=tmp_path) == {}


def test_load_car_notes_user_overlay_wins_per_field(tmp_path):
    cars, user = tmp_path / "cars", tmp_path / "user"
    _write(cars, "ferrari_296_lmgt3",
           {"name": "Ferrari", "notes": {"overview": "shipped", "handling": "H"}})
    _write(user, "ferrari_296_lmgt3", {"notes": {"overview": "user-edit"}})
    merged = load_car_notes("Ferrari 296 LMGT3", cars_dir=cars, user_dir=user)
    assert merged["overview"] == "user-edit"   # overlay wins
    assert merged["handling"] == "H"           # untouched shipped field kept


def test_load_car_notes_no_traversal(tmp_path):
    # A crafted name slugs to a plain stem; it can never escape the dir.
    assert car_slug("../../secret") == "secret"
    assert load_car_notes("../../secret", cars_dir=tmp_path) == {}


def test_shipped_car_notes_load_and_keep_provenance():
    notes = load_car_notes("Ferrari 296 LMGT3")   # from the shipped cars/
    assert notes.get("handling")
    assert "[LMU]" in notes["handling"] or "[general-sim]" in notes["handling"]


def test_setup_concepts_doc_is_present_and_concise():
    c = load_setup_concepts()
    body = c.get("notes", {})
    assert body.get("levers")                       # non-empty lever map
    assert "Brake bias" in body["levers"]
    # Concise, not the raw 20KB guide.
    assert len(json.dumps(body)) < 12000


def _ctx(**kw):
    return CoachContext(store=None, car="Ferrari 296 LMGT3", **kw)


def test_get_car_notes_with_authored_notes_includes_concepts():
    ctx = _ctx(car_notes={"overview": "Balanced.", "handling": "Understeer on entry.",
                          "driver_aids": "ABS 0-9 dial.", "sources": ["src"]},
               setup_concepts=load_setup_concepts())
    out = ctx.get_car_notes()
    assert "Authored notes for Ferrari 296 LMGT3" in out
    assert "Understeer on entry." in out
    assert "Shared LMU setup concepts" in out
    assert "Brake bias" in out


def test_get_car_notes_without_notes_falls_back_to_concepts():
    ctx = _ctx(car_notes={}, setup_concepts=load_setup_concepts())
    out = ctx.get_car_notes()
    assert "No car-specific notes for Ferrari 296 LMGT3 yet" in out
    assert "Brake bias" in out


def test_get_car_notes_registered_and_dispatchable():
    assert any(t["name"] == "get_car_notes" for t in TOOLS)
    ctx = _ctx(car_notes={"handling": "H"}, setup_concepts=load_setup_concepts())
    assert "H" in ctx.dispatch("get_car_notes", {})


def test_shipped_cars_dir_has_the_migrated_cars():
    for slug in ("ferrari_296_lmgt3", "porsche_911_gt3_r_lmgt3"):
        assert (CARS_DIR / f"{slug}.json").exists()
