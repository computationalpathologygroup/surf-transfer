"""Pure-function tests: no network, no disk, no openslide."""

import pytest

from surf_transfer.parsing import (
    check_completeness,
    compare_checksums,
    group_into_slides,
    iter_tiles,
    parse_oc_checksums,
    parse_propfind,
    parse_slidedat,
    sample_tile_coords,
)
from surf_transfer.util import fits_free_space, safe_relpath

SLIDEDAT = """\
[GENERAL]
SLIDE_ID = abc
[DATAFILE]
FILE_COUNT = 3
FILE_0 = Data0000.dat
FILE_1 = Data0001.dat
FILE_2 = Data0002.dat
[HIERARCHICAL]
NONHIER_COUNT = 1
"""


# --- Slidedat.ini -----------------------------------------------------------


def test_parse_slidedat_reads_file_list():
    info = parse_slidedat(SLIDEDAT)
    assert info.file_count == 3
    assert info.files == ["Data0000.dat", "Data0001.dat", "Data0002.dat"]


def test_parse_slidedat_reads_the_index_file_from_hierarchical():
    text = SLIDEDAT.replace("[HIERARCHICAL]", "[HIERARCHICAL]\nINDEXFILE = Index.dat")
    assert parse_slidedat(text).index_file == "Index.dat"
    assert parse_slidedat(SLIDEDAT).index_file is None  # optional: not every file declares it


def test_parse_slidedat_tolerates_bom_and_percent_signs():
    info = parse_slidedat("\ufeff[DATAFILE]\nFILE_COUNT=1\nFILE_0=Data0000.dat\n[X]\nA = 100%\n")
    assert info.files == ["Data0000.dat"]


def test_parse_slidedat_without_datafile_section_raises():
    with pytest.raises(ValueError, match="DATAFILE"):
        parse_slidedat("[GENERAL]\nA=1\n")


def test_parse_slidedat_missing_listed_key_raises():
    with pytest.raises(ValueError, match="FILE_1"):
        parse_slidedat("[DATAFILE]\nFILE_COUNT=2\nFILE_0=Data0000.dat\n")


def test_completeness_all_present():
    c = check_completeness(
        ["Data0000.dat", "Data0001.dat"], ["Data0001.dat", "Data0000.dat", "Slidedat.ini"]
    )
    assert c.ok and c.missing == [] and c.unexpected == []


def test_completeness_one_dat_missing():
    c = check_completeness(["Data0000.dat", "Data0001.dat"], ["Data0000.dat", "Slidedat.ini"])
    assert not c.ok
    assert c.missing == ["Data0001.dat"]
    assert c.unexpected == []


def test_completeness_unexpected_extra_dat():
    c = check_completeness(
        ["Data0000.dat"], ["Data0000.dat", "Data0007.dat", "Slidedat.ini", "notes.txt"]
    )
    assert not c.ok
    assert c.unexpected == ["Data0007.dat"]  # only .dat files count as unexpected
    assert c.missing == []


def test_completeness_expected_index_file_is_not_unexpected_and_must_be_present():
    expected = ["Data0000.dat", "Index.dat"]
    assert check_completeness(expected, ["Data0000.dat", "Index.dat"]).ok
    c = check_completeness(expected, ["Data0000.dat"])
    assert c.missing == ["Index.dat"] and c.unexpected == []


def test_completeness_reports_both_directions_sorted():
    c = check_completeness(["Data0002.dat", "Data0000.dat"], ["Data0001.dat", "Data0003.dat"])
    assert c.missing == ["Data0000.dat", "Data0002.dat"]
    assert c.unexpected == ["Data0001.dat", "Data0003.dat"]


# --- oc:checksums -----------------------------------------------------------


def test_parse_oc_checksums_multiple_algorithms():
    parsed = parse_oc_checksums("SHA1:ABCDEF12 MD5:0011 ADLER32:0a0b0c0d")
    assert parsed == {"sha1": "abcdef12", "md5": "0011", "adler32": "0a0b0c0d"}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_oc_checksums_absent(value):
    assert parse_oc_checksums(value) == {}


def test_parse_oc_checksums_ignores_garbage_tokens():
    assert parse_oc_checksums("nonsense SHA1:ab") == {"sha1": "ab"}


def test_compare_checksums_match():
    assert (
        compare_checksums({"sha1": "ab", "md5": "cd"}, {"sha1": "ab", "md5": "cd"}) == "sha1:match"
    )


def test_compare_checksums_mismatch():
    assert compare_checksums({"sha1": "ab"}, {"sha1": "ff"}) == "sha1:mismatch"


def test_compare_checksums_absent():
    assert compare_checksums({}, {"sha1": "ab"}) == "absent"


def test_compare_checksums_unsupported_algorithm_is_not_a_failure():
    assert compare_checksums({"crc64": "ab"}, {"sha1": "ab"}) == "unsupported:crc64"


def test_compare_checksums_prefers_strongest_algorithm():
    assert compare_checksums({"md5": "x", "sha1": "ab"}, {"md5": "x", "sha1": "ab"}) == "sha1:match"


# --- grouping ---------------------------------------------------------------


def _keys(groups):
    return {g.name: (g.kind, sorted(g.member_keys)) for g in groups}


def test_group_mrxs_with_its_folder():
    files = [
        ("a", "T/X.mrxs"),
        ("b", "T/X/Slidedat.ini"),
        ("c", "T/X/Data0000.dat"),
        ("d", "T/notes.txt"),
    ]
    groups = _keys(group_into_slides(files))
    assert groups["X"] == ("mrxs", ["a", "b", "c"])
    assert groups["notes"] == ("file", ["d"])


def test_group_two_slides_with_same_dat_names_do_not_collide():
    files = [
        ("1", "T/A.mrxs"),
        ("2", "T/A/Data0000.dat"),
        ("3", "T/A/Slidedat.ini"),
        ("4", "T/B.mrxs"),
        ("5", "T/B/Data0000.dat"),
        ("6", "T/B/Slidedat.ini"),
    ]
    groups = _keys(group_into_slides(files))
    assert groups["A"][1] == ["1", "2", "3"]
    assert groups["B"][1] == ["4", "5", "6"]


def test_group_mrxs_without_folder_is_still_a_slide_group():
    groups = group_into_slides([("a", "X.mrxs")])
    assert groups[0].kind == "mrxs" and groups[0].member_keys == ["a"]


def test_group_folder_without_mrxs_is_flagged_as_mrxs_group_missing_index():
    groups = group_into_slides([("b", "X/Slidedat.ini"), ("c", "X/Data0000.dat")])
    assert len(groups) == 1
    assert groups[0].kind == "mrxs"
    assert groups[0].index_key is None


def test_group_single_file_formats_and_archives():
    groups = _keys(
        group_into_slides([("a", "s1.svs"), ("b", "s2.TIFF"), ("c", "z.zip"), ("d", "r.csv")])
    )
    assert groups["s1"][0] == "single"
    assert groups["s2"][0] == "single"
    assert groups["z"][0] == "archive"
    assert groups["r"][0] == "file"


def test_group_other_archive_formats_are_archives_too():
    groups = _keys(group_into_slides([("a", "z.7z"), ("b", "y.tar.gz")]))
    assert groups["z"][0] == "archive" and groups["y"][0] == "archive"


def test_group_extension_match_is_case_insensitive():
    assert group_into_slides([("a", "X.MRXS")])[0].kind == "mrxs"


# --- tile sampling ----------------------------------------------------------


def test_sample_tile_coords_deterministic_for_seed():
    a = sample_tile_coords((100_000, 80_000), 64, seed=7)
    assert a == sample_tile_coords((100_000, 80_000), 64, seed=7)
    assert a != sample_tile_coords((100_000, 80_000), 64, seed=8)
    assert len(a) == 64


def test_sample_tile_coords_stay_inside_slide():
    for x, y in sample_tile_coords((5_000, 3_000), 200, seed=1, tile=512):
        assert 0 <= x <= 5_000 - 512 and 0 <= y <= 3_000 - 512


def test_sample_tile_coords_slide_smaller_than_tile():
    assert set(sample_tile_coords((100, 100), 5, seed=1, tile=512)) == {(0, 0)}


def test_iter_tiles_covers_every_pixel_once():
    tiles = list(iter_tiles((1000, 700), 512))
    assert sum(w * h for _, _, w, h in tiles) == 1000 * 700
    assert len(tiles) == 4


# --- PROPFIND ---------------------------------------------------------------

PROPFIND = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
<d:response><d:href>/public.php/webdav/S/</d:href><d:propstat><d:prop>
 <d:resourcetype><d:collection/></d:resourcetype><oc:fileid>1</oc:fileid></d:prop>
 <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/webdav/S/a%20b.mrxs</d:href><d:propstat><d:prop>
 <d:getcontentlength>889019</d:getcontentlength><d:getetag>&quot;e1&quot;</d:getetag>
 <d:resourcetype/><oc:fileid>42</oc:fileid>
 <oc:checksums><oc:checksum>SHA1:AB MD5:CD</oc:checksum></oc:checksums></d:prop>
 <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/public.php/webdav/S/nochk.dat</d:href>
 <d:propstat><d:prop><d:getcontentlength>5</d:getcontentlength><d:resourcetype/></d:prop>
 <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 <d:propstat><d:prop><oc:checksums/><oc:fileid/></d:prop>
 <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>
</d:multistatus>"""


def test_parse_propfind_entries():
    entries = parse_propfind(PROPFIND, "/public.php/webdav/")
    by_path = {e.path: e for e in entries}
    assert by_path["S"].is_dir
    f = by_path["S/a b.mrxs"]
    assert (f.size, f.file_id, f.etag) == (889019, "42", "e1")
    assert f.checksums == {"sha1": "ab", "md5": "cd"}
    g = by_path["S/nochk.dat"]
    assert g.checksums == {} and g.file_id is None and g.size == 5


# --- misc -------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../../b", "C:\\x", ""])
def test_safe_relpath_rejects_escapes(bad):
    with pytest.raises(ValueError):
        safe_relpath(bad)


def test_safe_relpath_normalises():
    assert safe_relpath("a\\b//c/./d.txt") == "a/b/c/d.txt"


def test_fits_free_space():
    assert fits_free_space(100, 60, 40)
    assert not fits_free_space(100, 61, 40)
