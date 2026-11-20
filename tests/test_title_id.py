from switchagent import title_id


def test_valid_title_id_shape():
    assert title_id.is_valid_title_id("0100000000010000")
    assert title_id.is_valid_title_id("ABCDEF0123456789")


def test_invalid_title_id_shapes():
    assert not title_id.is_valid_title_id("")
    assert not title_id.is_valid_title_id("010000000001000")     # 15 chars
    assert not title_id.is_valid_title_id("01000000000100000")   # 17 chars
    assert not title_id.is_valid_title_id("0100000000010G00")    # non-hex char
    assert not title_id.is_valid_title_id("0100 0000 0001 0000")  # spaces


def test_normalize_uppercases_valid_and_rejects_invalid():
    assert title_id.normalize_title_id("0100000000010000") == "0100000000010000"
    assert title_id.normalize_title_id("abcdef0123456789") == "ABCDEF0123456789"
    assert title_id.normalize_title_id("not-a-title-id") is None


def test_from_filename_single_match():
    guess = title_id.from_filename("Game [0100000000010000][v0].nsp")
    assert guess.title_id == "0100000000010000"
    assert guess.source == "filename"
    assert guess.confident is False  # filenames are labels, never authoritative


def test_from_filename_no_match_does_not_guess():
    guess = title_id.from_filename("NoTitleIdHere.nsp")
    assert guess.title_id is None
    assert guess.source is None


def test_from_filename_ambiguous_multiple_matches_refuses_to_guess():
    guess = title_id.from_filename("Weird [0100000000010000] and [0100000000020000].nsp")
    assert guess.title_id is None, "must not arbitrarily pick one of two candidates"


def test_from_filename_base_tag_corrects_an_update_id():
    """Real release (Moomin Midsummer Madness): base XCI and 1.0.3 update
    NSP both labelled with the update's id."""
    base = title_id.from_filename("Moomin Midsummer Madness [010009e0263ca800][Base].xci")
    update = title_id.from_filename("Moomin Midsummer Madness [010009e0263ca800][v196608][UP v1.0.3].nsp")
    assert base.title_id == "010009E0263CA000"
    assert update.title_id == "010009E0263CA800"
    assert title_id.classify_title_variant(base.title_id).variant == "BASE"
    assert title_id.classify_title_variant(update.title_id).variant == "UPDATE"


def test_from_filename_update_word_corrects_a_base_id():
    assert title_id.from_filename("Game [0100000000010000][UPD].nsp").title_id == "0100000000010800"
    assert title_id.from_filename("Game [0100000000010000][Update v1.2].nsp").title_id == "0100000000010800"


def test_from_filename_version_tag_alone_is_not_a_role():
    # a base cartridge with its update merged in is labelled with the update's version
    assert title_id.from_filename("Game [0100000000010000][v131072].xci").title_id == "0100000000010000"
    assert title_id.from_filename("Game [0100000000010800][v0].nsp").title_id == "0100000000010800"


def test_from_filename_role_never_moves_dlc_or_contradicts_itself():
    assert title_id.from_filename("Game DLC [0100000000011001][Base].nsp").title_id == "0100000000011001"
    assert title_id.from_filename("Game [0100000000010800][Base][UPD].nsp").title_id == "0100000000010800"


def test_strip_release_tags_removes_role_tags():
    assert title_id.strip_release_tags("Moomin Midsummer Madness [010009e0263ca800][Base]") == "Moomin Midsummer Madness"
    assert title_id.strip_release_tags("Moomin Midsummer Madness [010009e0263ca800][v196608][UP v1.0.3]") == \
        "Moomin Midsummer Madness"


def test_from_atmosphere_path_is_confident():
    guess = title_id.from_atmosphere_path("atmosphere/contents/0100000000040000/romfs/text.bin")
    assert guess.title_id == "0100000000040000"
    assert guess.source == "atmosphere_path"
    assert guess.confident is True


def test_from_atmosphere_path_case_insensitive_atmosphere_segment():
    guess = title_id.from_atmosphere_path("Atmosphere/Contents/0100000000040000/romfs/text.bin")
    assert guess.title_id == "0100000000040000"


def test_from_atmosphere_path_no_match():
    guess = title_id.from_atmosphere_path("some/random/path/file.bin")
    assert guess.title_id is None
    assert guess.confident is False


def test_from_atmosphere_path_rejects_invalid_title_id_shape():
    guess = title_id.from_atmosphere_path("atmosphere/contents/NOTHEX000000000/romfs/x.bin")
    assert guess.title_id is None


# ---------------------------------------------------------------------------
# classify_title_variant -- base/update/DLC arithmetic. Shared by
# switchagent/web/services.py (Library grouping) and switchagent/
# queue_worker.py (install-order dependency enforcement) -- see
# docs/STATE.md's Quake II investigation for why the queue_worker use
# exists at all.
# ---------------------------------------------------------------------------

def test_classify_base():
    result = title_id.classify_title_variant("01003AF0200B0000")
    assert result.variant == "BASE"
    assert result.base_title_id == "01003AF0200B0000"


def test_classify_update_recovers_base_id():
    result = title_id.classify_title_variant("01003AF0200B0800")
    assert result.variant == "UPDATE"
    assert result.base_title_id == "01003AF0200B0000"


def test_classify_dlc_recovers_base_id():
    result = title_id.classify_title_variant("01003AF0200B1001")
    assert result.variant == "DLC"
    assert result.base_title_id == "01003AF0200B0000"


def test_classify_dlc_matches_real_gods_with_guns_example():
    """Real data from the connected library: base 010030B0289BC000, DLC
    entries 010030B0289BD001/D002/D003 -- (0x...D000 - 0x1000) must equal
    the actual base id exactly, not an approximation."""
    result = title_id.classify_title_variant("010030B0289BD003")
    assert result.variant == "DLC"
    assert result.base_title_id == "010030B0289BC000"


def test_classify_multiple_dlc_indices_share_the_same_base():
    bases = {title_id.classify_title_variant(f"01003AF0200B10{n:02X}").base_title_id for n in range(1, 16)}
    assert bases == {"01003AF0200B0000"}


def test_classify_real_quake2_example():
    """The exact pair involved in the Quake II install-order investigation
    (see docs/STATE.md) -- base ...8000, update ...8800."""
    result = title_id.classify_title_variant("010048F0195E8800")
    assert result.variant == "UPDATE"
    assert result.base_title_id == "010048F0195E8000"


def test_strip_release_tags_removes_bracketed_id_and_version():
    assert title_id.strip_release_tags("Bread and Fred [0100AF401B6A4000][v0]") == "Bread and Fred"
    assert title_id.strip_release_tags("Rune Factory Guardians of Azuma [01003AF0200B0000][v0]") == \
        "Rune Factory Guardians of Azuma"


def test_strip_release_tags_no_op_on_a_plain_name():
    assert title_id.strip_release_tags("Celeste") == "Celeste"


def test_normalize_name_matches_titledb_punctuation_variants():
    """Real case (2026-09-15 cover-search investigation): TitleDB's own
    name has a colon a release filename dropped."""
    assert title_id.normalize_name("Rune Factory: Guardians of Azuma") == \
        title_id.normalize_name("Rune Factory Guardians of Azuma")


def test_normalize_name_matches_dbi_installed_games_trademark_glyphs():
    """Real hardware data (DBI's "Installed games" MTP node, 2026-09-15):
    DBI's own display name carries trademark glyphs a release filename
    typically doesn't."""
    assert title_id.normalize_name("Mortal Kombat™ 1") == title_id.normalize_name("Mortal Kombat 1")
    assert title_id.normalize_name("Tony Hawk's™ Pro Skater™ 3 + 4") == \
        title_id.normalize_name("Tony Hawks Pro Skater 3 + 4")


def test_normalize_name_treats_en_dash_and_hyphen_the_same():
    assert title_id.normalize_name("Grand Theft Auto Vice City – The Definitive Edition") == \
        title_id.normalize_name("Grand Theft Auto Vice City - The Definitive Edition")


def test_normalize_name_different_games_do_not_collide():
    assert title_id.normalize_name("Celeste") != title_id.normalize_name("Cocoon")
