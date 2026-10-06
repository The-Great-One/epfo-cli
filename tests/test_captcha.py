"""Captcha tests: OCR is a convenience, never an authority."""

from pathlib import Path

import pytest

from epfo.captcha import (
    CaptchaOCRError, auto_answer, correct_common_confusions, looks_plausible,
    ocr_available, solve,
)
from epfo.session import Captcha

# A 1x1 JPEG, so tests never depend on a live captcha image.
_TINY_JPEG = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


def test_solve_raises_rather_than_guessing_when_ocr_is_unavailable(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ocr_available", lambda: False)
    with pytest.raises(CaptchaOCRError, match="OCR unavailable"):
        captcha_module.solve(Captcha(image_base64=_TINY_JPEG, token="t"))


def test_solve_raises_when_ocr_returns_nothing(monkeypatch):
    """Whitespace-only OCR output must raise, never become an empty answer.

    The Pillow and tesseract calls are patched out: this asserts the decision
    logic, which is the part that must not guess. Both libraries are absent or
    broken on the test interpreter, and the seam is what makes that irrelevant.
    """
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "prepare_image", lambda path, scale=3: path)
    monkeypatch.setattr(captcha_module, "run_ocr",
                        lambda path, whitelist, psm=8: "   \n\t ")
    with pytest.raises(CaptchaOCRError, match="no characters"):
        captcha_module.solve(Captcha(image_base64=_TINY_JPEG, token="t"))


def test_solve_returns_the_cleaned_ocr_text(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "prepare_image", lambda path, scale=3: path)
    monkeypatch.setattr(captcha_module, "run_ocr",
                        lambda path, whitelist, psm=8: " Ab 3dEf\n ")
    result = captcha_module.solve(Captcha(image_base64=_TINY_JPEG, token="t"))
    assert result == "Ab3dEf"


@pytest.mark.parametrize("raw,expected", [
    ("O0Il", "0011"),
    ("S8Z2", "5822"),
    ("Ab3d", "Ab3d"),
])
def test_common_confusions_are_corrected(raw, expected):
    assert correct_common_confusions(raw) == expected


def test_ocr_availability_is_a_boolean():
    assert isinstance(ocr_available(), bool)


def test_captcha_writes_a_decodable_image(tmp_path):
    path = Captcha(image_base64=_TINY_JPEG, token="t").write_png(tmp_path / "c.jpg")
    assert path.exists()
    assert path.read_bytes().startswith(b"\xff\xd8")  # JPEG SOI marker


def test_auto_answer_returns_the_reading_untouched(monkeypatch):
    """A plausible reading is returned exactly as OCR produced it.

    It is deliberately not de-confused: the captcha may genuinely contain an
    "O" or an "I", so rewriting characters would corrupt a correct read.
    """
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "solve", lambda c: "OI58Z2")
    assert captcha_module.auto_answer(Captcha(image_base64="", token="t")) == "OI58Z2"


def test_auto_answer_raises_on_a_wrong_length_read(monkeypatch):
    """Length is the only thing that can make a reading implausible, and it
    must raise rather than let a short read reach the portal."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "solve", lambda c: "OI")
    with pytest.raises(CaptchaOCRError, match="not a plausible"):
        captcha_module.auto_answer(Captcha(image_base64="", token="t"))


def test_auto_answer_raises_when_neither_reading_is_plausible(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "solve", lambda c: "!!!!")
    with pytest.raises(CaptchaOCRError, match="not a plausible"):
        captcha_module.auto_answer(Captcha(image_base64="", token="t"))


@pytest.mark.parametrize("text,ok", [
    ("abc123", True),        # the normal case
    ("ABCDEFGH", True),      # upper bound
    ("abcd", True),          # lower bound
    ("abc", False),          # too short: one stray glyph is not an answer
    ("abcdefghi", False),    # too long: tesseract returned prose
    ("ab c1", False),        # whitespace means it read more than the captcha
    ("abc-12", False),       # punctuation is outside the captcha alphabet
    ("", False),
])
def test_looks_plausible_rejects_impossible_answers(text, ok):
    """This is the gate that stops a bad OCR read reaching the portal."""
    assert looks_plausible(text) is ok


def test_native_scaler_is_optional_not_required(monkeypatch, tmp_path):
    """With no scaler installed the original image is used, not an exception."""
    import epfo.captcha as captcha_module

    original = tmp_path / "c.jpg"
    original.write_bytes(b"\xff\xd8fake")
    monkeypatch.setattr(captcha_module, "_which", lambda b: None)
    assert captcha_module.prepare_image(original) == original


def test_run_ocr_reports_a_missing_binary_rather_than_returning_nothing(monkeypatch):
    """A vanished tesseract must raise: '' would be submitted as an answer."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "_which", lambda b: None)
    with pytest.raises(CaptchaOCRError, match="not installed"):
        captcha_module.run_ocr(Path("x.jpg"), "ABC")


def test_ocr_availability_reflects_the_binary(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "_which", lambda b: "/usr/bin/tesseract")
    assert captcha_module.ocr_available() is True
    monkeypatch.setattr(captcha_module, "_which", lambda b: None)
    assert captcha_module.ocr_available() is False


def test_solve_tries_psm8_before_psm7(monkeypatch):
    """psm 7 returned empty on four of six live captchas; psm 8 read all six.

    Pinning the order keeps a future tidy-up from restoring the setting that
    made automatic reading fail.
    """
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "prepare_image",
                        lambda path, scale=800: path)
    seen = []

    def record(path, whitelist, psm=8):
        seen.append(psm)
        return "AB3DEF" if psm == 8 else ""

    monkeypatch.setattr(captcha_module, "run_ocr", record)
    assert captcha_module.solve(Captcha(image_base64="", token="t")) == "AB3DEF"
    assert seen and seen[0] == 8
    assert captcha_module.PSM_MODES[0] == 8


def test_solve_falls_back_to_psm7_when_psm8_reads_nothing(monkeypatch):
    """psm 7 stays as a fallback for the two captchas it reads better."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "prepare_image",
                        lambda path, scale=800: path)
    calls = []

    def only_psm7(path, whitelist, psm=8):
        calls.append(psm)
        return "" if psm == 8 else "ZX9W4T"

    monkeypatch.setattr(captcha_module, "run_ocr", only_psm7)
    assert captcha_module.solve(Captcha(image_base64="", token="t")) == "ZX9W4T"
    assert calls == [8, 7]


# -- backends ---------------------------------------------------------------

def test_solve_with_rejects_an_unknown_backend():
    import epfo.captcha as captcha_module

    with pytest.raises(CaptchaOCRError, match="unknown captcha solver"):
        captcha_module.solve_with(Captcha(image_base64="", token="t"), "magic")


def test_available_solvers_reports_only_what_can_run(monkeypatch):
    """A missing backend must be omitted, never reported as usable."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "ocr_available", lambda: False)
    assert captcha_module.available_solvers() == ["ddddocr"]

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: False)
    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    assert captcha_module.available_solvers() == ["tesseract"]

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: True)
    monkeypatch.setattr(captcha_module, "ocr_available", lambda: True)
    assert captcha_module.available_solvers() == ["ddddocr", "tesseract"]


def test_auto_answer_sweeps_backends_until_one_reads_plausibly(monkeypatch):
    """A backend returning an implausible read must not stop the sweep."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "available_solvers",
                        lambda: ["ddddocr", "tesseract"])

    def fake(captcha, solver):
        if solver == "ddddocr":
            raise CaptchaOCRError("model unavailable")
        return "AB3DEF"

    monkeypatch.setattr(captcha_module, "solve_with", fake)
    assert captcha_module.auto_answer(Captcha(image_base64="", token="t")) == "AB3DEF"


def test_auto_answer_raises_when_no_backend_is_available(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "available_solvers", lambda: [])
    with pytest.raises(CaptchaOCRError, match="no captcha reader"):
        captcha_module.auto_answer(Captcha(image_base64="", token="t"))


def test_solve_ddddocr_upcases_and_validates(monkeypatch):
    """ddddocr answers lowercase; the portal accepts uppercase."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: True)

    class _Model:
        def classification(self, raw):
            return "ab3def"

    monkeypatch.setattr(captcha_module, "_ddddocr", lambda: _Model())
    assert captcha_module.solve_ddddocr(
        Captcha(image_base64="", token="t")) == "AB3DEF"


def test_solve_ddddocr_strips_glyphs_outside_the_alphabet(monkeypatch):
    """A stray glyph must never reach the portal as part of an answer."""
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: True)

    class _Model:
        def classification(self, raw):
            return "ab-3d.ef!"

    monkeypatch.setattr(captcha_module, "_ddddocr", lambda: _Model())
    assert captcha_module.solve_ddddocr(
        Captcha(image_base64="", token="t")) == "AB3DEF"


def test_solve_ddddocr_raises_on_empty_output(monkeypatch):
    import epfo.captcha as captcha_module

    monkeypatch.setattr(captcha_module, "ddddocr_available", lambda: True)

    class _Model:
        def classification(self, raw):
            return "   "

    monkeypatch.setattr(captcha_module, "_ddddocr", lambda: _Model())
    with pytest.raises(CaptchaOCRError, match="no characters"):
        captcha_module.solve_ddddocr(Captcha(image_base64="", token="t"))


def test_captcha_to_bytes_is_the_decoded_image():
    """Both backends must read the same bytes."""
    from epfo.session import Captcha

    import base64

    payload = base64.b64encode(b"\xff\xd8hello").decode()
    assert Captcha(image_base64=payload, token="t").to_bytes() == b"\xff\xd8hello"
