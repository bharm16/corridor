"""The shared statement-ordering signals, tested where they are defined.

Both the Evidence Investigator's shortlist and the guided statement screen
read these functions, and both previously reached them through a caller whose
tests could pass while the arithmetic drifted. These exercise the arithmetic
directly: what counts as station containment, what row text a verbatim term is
sought in, and how the rank weight combines the two.
"""

from corridor.models import Dependency
from corridor.statement_matching import (
    STATION_CONTAINMENT_WEIGHT,
    dependency_haystack,
    dependency_match_signals,
    dependency_station_contains,
    match_score,
    normalize_match_text,
    station_contains,
)


def _dependency(**overrides) -> Dependency:
    fields = {
        "ref_code": "UC-1",
        "source_ref": "SR-9",
        "title": "Buried 12-inch water main",
        "location_desc": "Nance Street crossing",
        "station_from": "1149+00",
        "station_to": "1153+17",
    }
    fields.update(overrides)
    return Dependency(**fields)


def test_match_text_casefolds_and_collapses_every_run_of_whitespace():
    assert normalize_match_text("  AT&T \n Texas\t(SWBT) ") == "at&t texas (swbt)"


def test_match_text_reads_a_missing_value_as_the_empty_string():
    assert normalize_match_text(None) == ""


def test_a_station_inside_the_range_is_contained_whichever_way_it_is_recorded():
    assert station_contains(1150.0, 1149.0, 1153.0) is True
    # A row whose two stations were entered in descending order still bounds
    # the same stretch of the corridor.
    assert station_contains(1150.0, 1153.0, 1149.0) is True
    assert station_contains(1160.0, 1149.0, 1153.0) is False


def test_the_range_endpoints_are_inside_it():
    assert station_contains(1149.0, 1149.0, 1153.0) is True
    assert station_contains(1153.0, 1149.0, 1153.0) is True


def test_a_row_with_one_recorded_station_is_a_point_not_an_open_range():
    assert station_contains(1149.0, 1149.0, None) is True
    assert station_contains(1150.0, 1149.0, None) is False
    assert station_contains(1153.0, None, 1153.0) is True


def test_a_row_with_no_recorded_station_contains_nothing():
    assert station_contains(1149.0, None, None) is False


def test_a_dependencys_own_stationing_is_parsed_before_it_is_compared():
    dependency = _dependency()
    assert dependency_station_contains(dependency, 115000.0) is True
    assert dependency_station_contains(dependency, 120000.0) is False
    assert dependency_station_contains(_dependency(station_from=None, station_to=None), 1.0) is False


def test_the_haystack_is_the_rows_four_identifying_columns_normalized():
    assert dependency_haystack(_dependency()) == (
        "uc-1 sr-9 buried 12-inch water main nance street crossing"
    )


def test_the_haystack_omits_a_column_the_row_never_filled_in():
    assert dependency_haystack(
        _dependency(source_ref=None, location_desc=None)
    ) == "uc-1 buried 12-inch water main"


def test_signals_name_party_station_and_verbatim_terms_in_that_order():
    signals, term_hits = dependency_match_signals(
        _dependency(),
        source_stations=(115000.0,),
        term_keys=("water", "nance", "gas"),
    )
    assert signals == (
        "registered_party_match",
        "station_overlap",
        "source_term_match",
    )
    assert term_hits == 2


def test_a_term_absent_from_the_row_text_earns_no_signal():
    signals, term_hits = dependency_match_signals(
        _dependency(),
        source_stations=(),
        term_keys=("telecom",),
    )
    assert signals == ("registered_party_match",)
    assert term_hits == 0


def test_any_one_statement_station_inside_the_row_earns_the_signal():
    signals, _ = dependency_match_signals(
        _dependency(),
        source_stations=(120000.0, 115000.0),
        term_keys=(),
    )
    assert "station_overlap" in signals


def test_station_containment_outweighs_every_reachable_term_count():
    assert match_score(station_containment=True, term_hits=0) == (
        STATION_CONTAINMENT_WEIGHT
    )
    assert match_score(station_containment=False, term_hits=3) == 3
    assert match_score(station_containment=True, term_hits=3) == (
        STATION_CONTAINMENT_WEIGHT + 3
    )
    # The ordering claim the weight exists to make: one contained station
    # ranks above a row that only shares words.
    assert match_score(station_containment=True, term_hits=0) > match_score(
        station_containment=False, term_hits=9
    )
