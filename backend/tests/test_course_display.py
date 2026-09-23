import pytest

from app.services.course_terms import normalize_course_display


@pytest.mark.parametrize("season", ["Fall", "Winter", "Spring", "Summer"])
@pytest.mark.parametrize("term", [None, "Fall 2026"])
def test_term_prefixed_canvas_code_skips_season_year(season, term):
    raw = f"{season} 2026-ASTR 405-Planetary Systems"
    display = normalize_course_display(raw, raw, term)
    assert display.course_code == "ASTR 405"
    assert display.name == "Planetary Systems"
    assert display.section is None


@pytest.mark.parametrize("code", ["astr_404_120268_264098", "ASTR404", "ASTR-404"])
def test_existing_department_codes_keep_their_number(code):
    display = normalize_course_display(
        code, "ASTR 404 - Stellar Astrophysics - Section 1 - Fall 2026", "Fall 2026"
    )
    assert display.course_code == "ASTR 404"
    assert display.name == "Stellar Astrophysics"
    assert display.section == "Section 1"


def test_unstructured_course_code_is_not_discarded():
    display = normalize_course_display("Independent Study", "Independent Study")
    assert display.course_code == "Independent Study"
    assert display.name == "Independent Study"
