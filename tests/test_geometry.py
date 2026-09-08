"""Row formatting still used by the native workbook reader and old citations."""
from corridor.geometry import dedupe_hint, row_quote, row_to_fields, split_station_offset
from corridor.verify import quote_appears_on

CONFLICT_MAPPING = {
    0: "utility_id", 1: "external_org", 2: "utility_type", 3: "size",
    4: "material", 5: "oh_ug", 6: "baseline", 7: "orientation",
    8: "alignment", 9: "location_start", 10: "location_end",
    11: "station_from", 12: "offset_from", 13: "station_to", 14: "offset_to",
    15: "offset_side", 16: "potential_conflict", 17: "sue_level", 18: "notes",
}

INVENTORY_MAPPING = {
    0: "utility_id", 1: "utility_type", 2: "external_org", 3: "oh_ug",
    4: "size", 5: "orientation", 6: "baseline", 7: "station_from",
    8: "offset_from", 9: "station_to", 10: "offset_to", 11: "offset_side",
}

def test_row_fields_are_read_by_header_not_position():
    conflict_row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG",
                    "IH 69", "Crossing", "IH 69", "Schwartz Street",
                    "Eastex Freeway FR SB", "1149+00", "303", "1153+17", "309",
                    "L/R", "Y", "B", ""]
    inventory_row = ["WW1**", "Wastewater", "City of Houston", "UG", '18" PVC',
                     "Crossing", "IH10", "1143+44.01", "NA", "NA", "NA",
                     "Crossing CL"]

    a = row_to_fields(conflict_row, CONFLICT_MAPPING)
    b = row_to_fields(inventory_row, INVENTORY_MAPPING)

    assert a["external_org"] == "AT&T Texas (SWBT)"
    assert a["station_from"] == "1149+00"
    assert b["external_org"] == "City of Houston"
    assert b["station_from"] == "1143+44.01"
    # Same field, opposite column index in the two layouts.
    assert a["utility_type"] == "Telecom" and b["utility_type"] == "Wastewater"

def test_empty_cells_are_omitted_not_stored_as_blanks():
    row = ["FOC1-1", "AT&T", "Telecom", "", "", "", "", "", "", "", "",
           "1149+00", "", "", "", "", "", "", ""]
    fields = row_to_fields(row, CONFLICT_MAPPING)
    assert "size" not in fields
    assert fields["station_from"] == "1149+00"

def test_the_quote_is_the_whole_row():
    row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG"]
    assert row_quote(row) == "FOC1-1 AT&T Texas (SWBT) Telecom FOC UG"

def test_dedupe_hint_is_org_type_and_station_range():
    fields = {"external_org": "AT&T Texas (SWBT)", "utility_type": "Telecom",
              "station_from": "1149+00", "station_to": "1153+17"}
    assert dedupe_hint(fields) == "AT&T Texas (SWBT)|Telecom|1149+00-1153+17"

def test_a_combined_station_and_offset_column_is_split():
    """CDOT and FDOT put stationing and offset in one column.

    `135+58.68, 236.85' LT` is one cell. TxDOT spreads the same information
    across four, so a header map alone cannot reconcile them.
    """
    fields = split_station_offset(
        {"station_from": "135+58.68, 236.85' LT", "station_to": "138+44.23, 199.94' RT"}
    )
    assert fields["station_from"] == "135+58.68"
    assert fields["offset_from"] == "236.85"
    assert fields["station_to"] == "138+44.23"
    assert fields["offset_to"] == "199.94"
    # One column cannot carry two sides; the first wins and is recorded.
    assert fields["offset_side"] == "LT"

def test_txdot_stationing_is_untouched_by_the_split():
    """The split must be a no-op on the layout that already works."""
    original = {
        "station_from": "1149+00",
        "station_to": "1153+17",
        "offset_from": "303",
        "offset_to": "309",
        "offset_side": "R",
    }
    assert split_station_offset(dict(original)) == original

def test_a_station_without_an_offset_is_left_as_stationing():
    assert split_station_offset({"station_from": "1143+44.01"}) == {
        "station_from": "1143+44.01"
    }

def test_a_value_that_does_not_parse_is_left_whole_rather_than_guessed():
    """One CDOT row reads `169+83.31, 131+45' RT` — the offset is a station.

    Splitting on the comma alone would record an offset of 131 feet from a
    value that plainly does not mean that. Leaving it intact keeps the
    anomaly visible to a reviewer instead of laundering it into a number.
    """
    odd = {"station_from": "169+83.31, 131+45' RT"}
    assert split_station_offset(dict(odd)) == odd

def test_a_trailing_period_does_not_defeat_the_split():
    fields = split_station_offset({"station_from": "152+76.81, 160.66' LT."})
    assert fields["station_from"] == "152+76.81"
    assert fields["offset_from"] == "160.66"

def test_a_row_that_is_not_contiguous_falls_back_to_a_verifying_window():
    from corridor.geometry import MatrixRow, best_verifiable_quote

    # The real shape: a leading `Data Source` cell and the trailing station
    # cells both land elsewhere in reading order, so the row is present but
    # not contiguous.
    row = MatrixRow(
        fields={"utility_id": "E1", "external_org": "CenterPoint Energy"},
        page_no=1,
        quote=(
            "SUE E1 CenterPoint Energy Electric Dist OH IH 10 Crossing "
            "IH 10 Nance Street Buck Street 1142+69 267"
        ),
        cells=(),
    )
    page = (
        "E1 CenterPoint Energy Electric Dist OH IH 10 Crossing IH 10 Nance Street\n"
        + "\n".join(["unrelated legend content"] * 10)
        + "\nSUE\nBuck Street\n1142+69\n267"
    )

    quote, whole_row = best_verifiable_quote(row, page)
    assert whole_row is False
    assert quote_appears_on(quote, page)
    assert quote.startswith("E1 CenterPoint Energy")

def test_a_verifiable_whole_row_is_preferred():
    from corridor.geometry import MatrixRow, best_verifiable_quote

    row = MatrixRow(fields={}, page_no=1, quote="FOC1-1 AT&T Texas Telecom", cells=())
    quote, whole_row = best_verifiable_quote(row, "x FOC1-1 AT&T Texas Telecom y")
    assert whole_row is True
    assert quote == "FOC1-1 AT&T Texas Telecom"
