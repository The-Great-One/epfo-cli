"""Captcha handling: OCR first, human entry as the guaranteed fallback.

The login captcha is a 6-character JPEG served as base64. This module drives the
tesseract *binary* rather than the ``pytesseract``/``Pillow`` Python bindings,
and that is a deliberate correction of a real failure: on this machine the
bindings resolved to a Pillow compiled for a different interpreter
(``ImportError: cannot import name '_imaging' from 'PIL'``), which made OCR
silently unavailable. Tesseract needs neither binding. Upscaling is handed to a
native tool when one exists and skipped when it does not.

OCR is never authoritative. A wrong answer costs a login attempt *and* rotates
the server token, so ``solve`` raises rather than guessing, the answer is
validated against the captcha's own shape before it is submitted, and a human
answer remains the guaranteed path.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .session import Captcha

# The portal's captcha is alphanumerics only. Constraining the answer here means
# a misread "!" or "?" can never reach the server.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# tesseract's most frequent confusions on this specific image. Applied last, so
# they only ever refine an answer that is already the right shape.
_CONFUSIONS = {"O": "0", "Q": "0", "D": "0", "I": "1", "l": "1", "|": "1",
               "S": "5", "B": "8", "Z": "2", "G": "6"}

# Every captcha observed from this portal is six characters. Kept as a range
# rather than an equality so a seven-character captcha is not thrown away.
_MIN_LENGTH, _MAX_LENGTH = 4, 8


class CaptchaOCRError(RuntimeError):
    """Raised when OCR is unavailable or produced nothing usable."""


def _which(binary: str) -> str | None:
    from shutil import which
    return which(binary)


def ocr_available() -> bool:
    """True when the tesseract binary can be executed."""
    return bool(_which("tesseract"))


def _native_scaler() -> list[str] | None:
    """Command prefix for an image upscaler, or None when there is none.

    ``sips`` ships with macOS; ImageMagick covers most Linux boxes. Returning a
    command rather than running it keeps this testable and keeps the failure
    path (no scaler) a normal, handled state.
    """
    if _which("sips"):
        return ["sips", "-Z", "1400"]
    if _which("magick"):
        return ["magick", "__IN__", "-resize", "400%", "__OUT__"]
    if _which("convert"):
        return ["convert", "__IN__", "-resize", "400%", "__OUT__"]
    return None


def prepare_image(path: Path, *, scale: int = 800) -> Path:
    """Upscale a captcha to help OCR, returning the image to read.

    Measured against six live captchas, 400/800/1400 px gave identical
    tesseract readings, so 800 is used: it is the middle of a range that does
    not matter, at a third of the largest size. When no scaler is installed the
    original is returned unchanged - degraded accuracy is acceptable, a crash
    is not.
    """
    scaler = _native_scaler()
    if scaler is None:
        return path

    try:
        if scaler[0] == "sips":
            target = path.with_name(path.stem + "-big.png")
            subprocess.run([*scaler, str(scale), str(path), "--out", str(target)],
                           check=True, capture_output=True, timeout=30)
        else:
            target = path.with_name(path.stem + "-big.png")
            argv = [str(target) if a == "__OUT__" else
                    (str(path) if a == "__IN__" else a) for a in scaler]
            subprocess.run(argv, check=True, capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return path
    return target if target.exists() else path


# Page-segmentation modes to try, best first. This ordering is empirical, not
# arbitrary: across six live captchas ``--psm 7`` returned an EMPTY string on
# four of them while ``--psm 8`` read all six. psm 7 is kept as a fallback
# because it did read the two that are the most tightly cropped.
PSM_MODES = (8, 7)


def run_ocr(path: Path, whitelist: str, psm: int = 8) -> str:
    """Run tesseract over an image and return its raw text.

    The binary is exec'd directly. A non-zero exit is turned into a
    ``CaptchaOCRError`` so the caller degrades to asking a human instead of
    treating an empty string as an answer.
    """
    binary = _which("tesseract")
    if not binary:
        raise CaptchaOCRError("tesseract is not installed")
    try:
        completed = subprocess.run(
            [binary, str(path), "stdout", "--psm", str(psm),
             "-c", f"tessedit_char_whitelist={whitelist}"],
            capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        raise CaptchaOCRError(f"tesseract could not run: {exc}") from exc
    if completed.returncode != 0:
        raise CaptchaOCRError(
            f"tesseract exited {completed.returncode}: "
            f"{completed.stderr.strip()[:200]}")
    return completed.stdout


def looks_plausible(text: str) -> bool:
    """Whether an OCR result could be a captcha answer at all.

    Guards the two failure modes seen live: tesseract returning prose because
    the upscaled image confused it, and returning a single stray glyph. Either
    would otherwise be POSTed to the portal as a real answer.
    """
    return (text.isalnum()
            and _MIN_LENGTH <= len(text) <= _MAX_LENGTH)


def solve(captcha: Captcha, *, whitelist: str = ALPHABET) -> str:
    """Best-effort OCR of a captcha. Raises rather than guessing.

    Orchestration only - the scaling and tesseract calls sit behind
    ``prepare_image`` and ``run_ocr`` so this decision logic is testable without
    either tool installed.
    """
    if not ocr_available():
        raise CaptchaOCRError(
            "OCR unavailable: install tesseract (brew install tesseract), or "
            "answer the captcha manually")

    with tempfile.TemporaryDirectory() as tmp:
        raw = captcha.write_png(Path(tmp) / "captcha.jpg")
        image = prepare_image(raw)
        candidates = []
        for psm in PSM_MODES:
            try:
                text = run_ocr(image, whitelist, psm=psm)
            except CaptchaOCRError:
                continue
            # Drop whitespace *and* any glyph outside the alphabet: tesseract
            # emits stray punctuation on this image and it must never reach the
            # server.
            cleaned = "".join(
                ch for ch in re.sub(r"\s+", "", text) if ch in whitelist)
            if cleaned:
                candidates.append(cleaned)

    if not candidates:
        raise CaptchaOCRError("OCR returned no characters")
    # Prefer a reading that could actually be a captcha; fall back to the first
    # non-empty one so the caller can decide and report precisely.
    for candidate in candidates:
        if looks_plausible(candidate):
            return candidate
    return candidates[0]


# -- backends -------------------------------------------------------------
#
# Two independent readers are wired up because they fail differently, and the
# gap is measured rather than assumed. Against eight live captchas they agreed
# on four and disagreed on four - every disagreement an O/9 or T/7 confusion:
#
#     tesseract  OXFF3U   XW6FOB   TEGKNY   ANBRDO
#     ddddocr    9XFF3U   XW6F9B   7EGKNY   ANBRD9
#
# All four disputed captchas were then inspected visually against the rendered
# images. ddddocr was correct in all four (9, 9, 7, 9 - each a closed loop or a
# clean diagonal, never an O or a T):
#
#     ddddocr   8/8 correct
#     tesseract 4/8 correct   (wrong on every disagreement)
#
# So ddddocr is the primary reader and is tried first. tesseract is kept as a
# genuine second opinion rather than a co-equal: it is what remains if the
# ONNX model will not load, and the CLI still alternates across login attempts
# so a second attempt is a different bet rather than a repeat of the first.
# Do not "simplify" this to tesseract alone - that halves the hit rate.

SOLVERS = ("ddddocr", "tesseract")


def ddddocr_available() -> bool:
    """True when the purpose-built captcha model can be loaded.

    Import failures are caught broadly on purpose: ddddocr depends on Pillow,
    numpy, onnxruntime and OpenCV, and any one of them being built for a
    different interpreter must degrade to tesseract rather than crash a login.
    """
    try:
        import ddddocr  # noqa: F401
    except Exception:
        return False
    return True


def available_solvers() -> list[str]:
    """Every reader that can actually run here, best-known first."""
    found = []
    if ddddocr_available():
        found.append("ddddocr")
    if ocr_available():
        found.append("tesseract")
    return found


_DDDDOCR = None


def _ddddocr():
    """A lazily-built, cached ddddocr instance.

    Construction loads an ONNX model, so it is done once per process rather
    than per attempt. The character range is set explicitly to lowercase
    alphanumerics: ddddocr answers in lowercase, and constraining it to the
    captcha's own alphabet is what stops it emitting a stray glyph.
    """
    global _DDDDOCR
    if _DDDDOCR is None:
        import ddddocr

        model = ddddocr.DdddOcr(show_ad=False)
        model.set_ranges("0123456789abcdefghijklmnopqrstuvwxyz")
        _DDDDOCR = model
    return _DDDDOCR


def solve_ddddocr(captcha: Captcha) -> str:
    """Read a captcha with ddddocr, returning uppercase to match the portal.

    ddddocr is case-insensitive in practice and answers in lowercase, while
    every answer this portal has accepted was uppercase, so the result is
    upcased here rather than at the call site.
    """
    if not ddddocr_available():
        raise CaptchaOCRError("ddddocr is not installed")
    try:
        raw = _ddddocr().classification(captcha.to_bytes())
    except Exception as exc:
        raise CaptchaOCRError(f"ddddocr failed: {exc}") from exc
    text = "".join(ch for ch in re.sub(r"\s+", "", raw) if ch in ALPHABET)
    if not text:
        raise CaptchaOCRError("ddddocr returned no characters")
    return text.upper()


def solve_with(captcha: Captcha, solver: str) -> str:
    """Read a captcha with one named backend."""
    if solver == "ddddocr":
        return solve_ddddocr(captcha)
    if solver == "tesseract":
        return solve(captcha)
    raise CaptchaOCRError(f"unknown captcha solver {solver!r}")


def correct_common_confusions(text: str) -> str:
    """Apply the documented OCR confusion substitutions to a candidate."""
    return "".join(_CONFUSIONS.get(ch, ch) for ch in text)


def auto_answer(captcha: Captcha, solver: str | None = None) -> str:
    """Return a captcha reading from one backend, or sweep them, or raise.

    With ``solver`` given, only that backend is tried. With ``solver=None``
    every available backend is swept in order until one produces a plausible
    reading - used when the caller has no better information than "read this
    somehow". The CLI instead names a specific backend per attempt so that
    consecutive attempts use *different* readers; see ``SOLVERS``.
    """
    solvers = [solver] if solver else available_solvers()
    if not solvers:
        raise CaptchaOCRError(
            "no captcha reader is available: install ddddocr "
            "(`pip install ddddocr`) or tesseract (`brew install tesseract`)")

    failures = []
    for name in solvers:
        try:
            guess = solve_with(captcha, name)
        except CaptchaOCRError as exc:
            failures.append(f"{name}: {exc}")
            continue
        if looks_plausible(guess):
            return guess
        failures.append(f"{name}: {guess!r} is not a plausible captcha answer")

    raise CaptchaOCRError("; ".join(failures))
