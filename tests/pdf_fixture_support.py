"""Synthetic PDF fixtures whose expectations are authored, not read back.

The test suite used to build its PDFs with PyMuPDF and then, in many modules,
asked PyMuPDF to read the page text back to learn what the fixture "said".
That made the fixture depend on the reader under test twice over: a reader
defect could hide inside the fixture that was meant to expose it, and the
fixtures could not outlive the reader, which #727 is replacing. This module
writes a minimal PDF directly - a cross-reference table, page objects with
media box, crop box and rotation, content streams that place text in the
standard non-embedded Helvetica, rules as stroked paths, and scanned pages as
image XObjects - and declares, alongside the bytes, the text, word boxes and
image samples a correct reader must recover from them (#730).

Word boxes are authored from the placement: the baseline the text was put on,
the font size, and the Helvetica advance widths published in Adobe's AFM file
for the standard 14 fonts. The same widths are written into the font
dictionary, so any conforming reader measures the glyphs the fixture declares.
Vertical extents use the AFM font bounding box (931 above and 225 below the
baseline per 1000 units); readers that take a substitute face's metrics report
slightly taller boxes, which is a reader convention, not a fixture fact.

Coordinates follow the convention the replaced fixtures and the production
readers share: points, origin at the top-left corner of the crop box, y
growing downward, on the unrotated page. A page's ``/Rotate`` only changes
how the page is displayed; declared boxes stay in the unrotated frame.

Only the standard library and Pillow are used. Nothing here imports a PDF
reader, and the module's own tests verify its output structurally, so the
builder keeps working with PyMuPDF absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import zlib

from PIL import Image, ImageDraw, ImageFont

# Helvetica, from the Adobe Core 14 AFM (Helvetica.afm, version 002.000):
# FontBBox -166 -225 1000 931, Ascender 718, Descender -207.
ASCENT = 0.931
DESCENT = 0.225
# Baseline-to-baseline distance for consecutive authored lines, as a multiple
# of the font size. A conventional single spacing; nothing in the AFM fixes it.
LINE_HEIGHT = 1.2

# Advance widths per 1000 units of font size, printable ASCII in code order.
_ASCII_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 222, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584,
    584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
    278, 278, 469, 556, 222, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222,
    500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500,
    500, 334, 260, 334, 584,
)
HELVETICA_WIDTHS: dict[str, int] = {
    chr(code): width for code, width in zip(range(32, 127), _ASCII_WIDTHS, strict=True)
}
# The WinAnsiEncoding characters the fixtures use beyond ASCII, same source.
HELVETICA_WIDTHS.update(
    {
        " ": 278,  # no-break space
        "§": 556,  # section
        "©": 737,  # copyright
        "®": 737,  # registered
        "°": 400,  # degree
        "±": 584,  # plusminus
        "·": 278,  # periodcentered
        "×": 584,  # multiply
        "–": 556,  # endash
        "—": 1000,  # emdash
        "‘": 222,  # quoteleft
        "’": 222,  # quoteright
        "“": 333,  # quotedblleft
        "”": 333,  # quotedblright
        "•": 350,  # bullet
        "…": 1000,  # ellipsis
        "€": 556,  # Euro
        "™": 1000,  # trademark
    }
)


def helvetica_width(text: str, fontsize: float) -> float:
    """The advance of ``text`` at ``fontsize`` points, from the AFM widths."""

    total = 0
    for character in text:
        try:
            total += HELVETICA_WIDTHS[character]
        except KeyError:
            raise ValueError(
                f"no Helvetica width is declared for {character!r}"
            ) from None
    return total * fontsize / 1000


class TextOverflow(ValueError):
    """The text does not fit its box at the requested size; nothing was placed."""


@dataclass(frozen=True)
class Word:
    """One whitespace-delimited word and the box its glyphs occupy."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    def fixed_point_box(self) -> tuple[int, int, int, int]:
        """The box in thousandths of a point, the production readers' unit."""

        return tuple(round(value * 1000) for value in (self.x0, self.y0, self.x1, self.y1))


@dataclass(frozen=True)
class Line:
    """One placed line of text: its exact characters and baseline origin."""

    text: str
    x0: float
    baseline: float
    fontsize: float
    x1: float


@dataclass(frozen=True)
class EmbeddedImage:
    """The exact samples an image XObject was built from and where it sits."""

    box: tuple[float, float, float, float]
    width: int
    height: int
    mode: str
    samples: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.samples).hexdigest()


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _win_ansi(text: str) -> bytes:
    try:
        return text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{text[exc.start:exc.end]!r} is outside WinAnsiEncoding; the fixture"
            " font is the standard Helvetica"
        ) from None


@dataclass
class PageFixture:
    """One page: what was drawn on it and what a reader must recover."""

    fixture: "PdfFixture"
    width: float
    height: float
    rotation: int
    cropbox: tuple[float, float, float, float]
    _content: list[str] = field(default_factory=list)
    _lines: list[Line] = field(default_factory=list)
    _words: list[Word] = field(default_factory=list)
    _images: list[EmbeddedImage] = field(default_factory=list)
    _rules: list[tuple] = field(default_factory=list)

    # -- what a reader must recover --------------------------------------

    @property
    def media_box(self) -> tuple[float, float, float, float]:
        return (0, 0, self.width, self.height)

    @property
    def crop_box(self) -> tuple[float, float, float, float]:
        """Top-left media-box coordinates, the frame ``cropbox`` was given in."""

        return self.cropbox

    @property
    def expected_text(self) -> str:
        """Every placed line in placement order, each ending in a newline."""

        return "".join(f"{line.text}\n" for line in self._lines)

    @property
    def expected_lines(self) -> tuple[Line, ...]:
        return tuple(self._lines)

    @property
    def expected_words(self) -> tuple[Word, ...]:
        return tuple(self._words)

    @property
    def embedded_images(self) -> tuple[EmbeddedImage, ...]:
        return tuple(self._images)

    @property
    def rules(self) -> tuple[tuple, ...]:
        return tuple(self._rules)

    # -- drawing -----------------------------------------------------------

    def text(self, point, text: str, *, fontsize: float = 11) -> tuple[Word, ...]:
        """Place ``text`` with its first baseline starting at ``point``.

        Authored line breaks start new lines ``LINE_HEIGHT`` font sizes apart;
        an empty authored line has no glyphs and so declares nothing.
        """

        x, y = point
        placed = [
            self._measure(line, x, y + index * LINE_HEIGHT * fontsize, fontsize)
            for index, line in enumerate(text.splitlines())
            if line
        ]
        return self._commit(placed)

    def text_box(self, box, text: str, *, fontsize: float = 11) -> tuple[Line, ...]:
        """Wrap ``text`` into ``box`` at word boundaries, top-left justified.

        Raises ``TextOverflow`` - and places nothing - when a word is wider
        than the box or the wrapped lines are taller than it.
        """

        x0, y0, x1, y1 = box
        wrapped: list[str] = []
        space = helvetica_width(" ", fontsize)
        for authored in text.splitlines():
            current = ""
            remaining = x1 - x0
            for word in authored.split(" "):
                advance = helvetica_width(word, fontsize)
                if remaining >= advance:
                    current += word + " "
                    remaining -= advance + space
                    continue
                if current:
                    wrapped.append(current.rstrip())
                if advance > x1 - x0:
                    raise TextOverflow(f"{word!r} is wider than the {x1 - x0}-point box")
                current = word + " "
                remaining = x1 - x0 - advance - space
            wrapped.append(current.rstrip())
        if len(wrapped) * LINE_HEIGHT * fontsize > y1 - y0:
            raise TextOverflow(
                f"{len(wrapped)} lines at {fontsize} points overflow the {y1 - y0}-point box"
            )
        placed = [
            self._measure(line, x0, y0 + ASCENT * fontsize + index * LINE_HEIGHT * fontsize, fontsize)
            for index, line in enumerate(wrapped)
            if line
        ]
        self._commit(placed)
        return tuple(line for line, _, _ in placed)

    def rect(self, box, *, width: float = 1.0) -> None:
        """Stroke the rectangle ``box`` in black."""

        x0, y0, x1, y1 = box
        self._content.append(
            "q\n0 G\n"
            f"{_number(width)} w\n"
            f"{_number(x0 + self.cropbox[0])} {_number(self._file_y(y1))}"
            f" {_number(x1 - x0)} {_number(y1 - y0)} re\nS\nQ\n"
        )
        self._rules.append(("rect", tuple(box), width))

    def line(self, start, end, *, width: float = 1.0) -> None:
        """Stroke a straight line from ``start`` to ``end`` in black."""

        (x0, y0), (x1, y1) = start, end
        self._content.append(
            "q\n0 G\n"
            f"{_number(width)} w\n"
            f"{_number(x0 + self.cropbox[0])} {_number(self._file_y(y0))} m\n"
            f"{_number(x1 + self.cropbox[0])} {_number(self._file_y(y1))} l\nS\nQ\n"
        )
        self._rules.append(("line", (x0, y0, x1, y1), width))

    def image(self, box, image: Image.Image) -> EmbeddedImage:
        """Embed ``image`` filling ``box``, as 8-bit gray or RGB samples."""

        if image.mode not in {"L", "RGB"}:
            image = image.convert("RGB")
        x0, y0, x1, y1 = box
        embedded = EmbeddedImage(
            box=tuple(box),
            width=image.width,
            height=image.height,
            mode=image.mode,
            samples=image.tobytes(),
        )
        self._images.append(embedded)
        self._content.append(
            "q\n"
            f"{_number(x1 - x0)} 0 0 {_number(y1 - y0)}"
            f" {_number(x0 + self.cropbox[0])} {_number(self._file_y(y1))} cm\n"
            f"/Im{len(self._images)} Do\nQ\n"
        )
        return embedded

    # -- internals -----------------------------------------------------------

    def _file_y(self, y: float) -> float:
        """A top-left, crop-box-relative y as the file's bottom-up y."""

        return self.height - (y + self.cropbox[1])

    def _measure(
        self, text: str, x0: float, baseline: float, fontsize: float
    ) -> tuple[Line, list[Word], str]:
        encoded = _win_ansi(text)
        line = Line(text, x0, baseline, fontsize, x0 + helvetica_width(text, fontsize))
        words: list[Word] = []
        cursor = x0
        for piece in _split_keeping_spaces(text):
            advance = helvetica_width(piece, fontsize)
            if not piece.isspace():
                words.append(
                    Word(piece, cursor, baseline - ASCENT * fontsize, cursor + advance, baseline + DESCENT * fontsize)
                )
            cursor += advance
        operators = (
            "BT\n"
            f"/F1 {_number(fontsize)} Tf\n"
            f"1 0 0 1 {_number(x0 + self.cropbox[0])} {_number(self._file_y(baseline))} Tm\n"
            f"<{encoded.hex().upper()}> Tj\nET\n"
        )
        return line, words, operators

    def _commit(self, placed) -> tuple[Word, ...]:
        words: list[Word] = []
        for line, line_words, operators in placed:
            self._lines.append(line)
            self._words.extend(line_words)
            self._content.append(operators)
            words.extend(line_words)
        return tuple(words)


def _split_keeping_spaces(text: str) -> list[str]:
    pieces: list[str] = []
    for character in text:
        if pieces and (pieces[-1][-1] == " ") == (character == " "):
            pieces[-1] += character
        else:
            pieces.append(character)
    return pieces


class PdfFixture:
    """A document of ``PageFixture`` pages and the bytes that encode them.

    The bytes are a pure function of what was drawn, so two fixtures with the
    same content are the same file and the same digest. A fixture that must
    be a distinct document despite identical content - five revisions of one
    matrix - says so with ``identity``, which becomes the trailer's ``/ID``.
    """

    def __init__(self, *, identity: str | None = None) -> None:
        self.pages: list[PageFixture] = []
        self.identity = identity

    def add_page(
        self,
        width: float = 595,
        height: float = 842,
        *,
        rotation: int = 0,
        cropbox: tuple[float, float, float, float] | None = None,
    ) -> PageFixture:
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("page rotation must be 0, 90, 180 or 270 degrees")
        if cropbox is None:
            cropbox = (0, 0, width, height)
        x0, y0, x1, y1 = cropbox
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("the crop box must lie inside the media box")
        page = PageFixture(self, width, height, rotation, tuple(cropbox))
        self.pages.append(page)
        return page

    def tobytes(self) -> bytes:
        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # the page tree, filled in once every page has a number
            _font_dictionary(),
            b"<< /Type /FontDescriptor /FontName /Helvetica /Flags 32"
            b" /FontBBox [-166 -225 1000 931] /ItalicAngle 0 /Ascent 718"
            b" /Descent -207 /CapHeight 718 /StemV 88 >>",
        ]
        page_numbers: list[int] = []
        for page in self.pages:
            content = "".join(page._content).encode("latin-1")
            objects.append(_stream(b"<< /Length %d >>" % len(content), content))
            contents_number = len(objects)
            xobjects = []
            for index, image in enumerate(page._images, start=1):
                data = zlib.compress(image.samples)
                colorspace = b"/DeviceGray" if image.mode == "L" else b"/DeviceRGB"
                objects.append(
                    _stream(
                        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d"
                        b" /ColorSpace %s /BitsPerComponent 8 /Filter /FlateDecode"
                        b" /Length %d >>"
                        % (image.width, image.height, colorspace, len(data)),
                        data,
                    )
                )
                xobjects.append(f"/Im{index} {len(objects)} 0 R")
            resources = "/Resources << /Font << /F1 3 0 R >>"
            if xobjects:
                resources += f" /XObject << {' '.join(xobjects)} >>"
            resources += " >>"
            entries = [
                "/Type /Page /Parent 2 0 R",
                f"/MediaBox [0 0 {_number(page.width)} {_number(page.height)}]",
            ]
            if page.cropbox != (0, 0, page.width, page.height):
                x0, y0, x1, y1 = page.cropbox
                entries.append(
                    "/CropBox ["
                    f"{_number(x0)} {_number(page.height - y1)}"
                    f" {_number(x1)} {_number(page.height - y0)}]"
                )
            if page.rotation:
                entries.append(f"/Rotate {page.rotation}")
            entries.append(resources)
            entries.append(f"/Contents {contents_number} 0 R")
            objects.append(f"<< {' '.join(entries)} >>".encode("latin-1"))
            page_numbers.append(len(objects))
        kids = " ".join(f"{number} 0 R" for number in page_numbers)
        objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode()

        out = BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for number, body in enumerate(objects, start=1):
            offsets.append(out.tell())
            out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
        startxref = out.tell()
        out.write(f"xref\n0 {len(objects) + 1}\n".encode())
        out.write(b"0000000000 65535 f \n")
        for offset in offsets:
            out.write(f"{offset:010d} 00000 n \n".encode())
        trailer = f"/Size {len(objects) + 1} /Root 1 0 R"
        if self.identity is not None:
            digest = sha256(self.identity.encode("utf-8")).hexdigest()[:32].upper()
            trailer += f" /ID [<{digest}> <{digest}>]"
        out.write(
            f"trailer\n<< {trailer} >>\nstartxref\n{startxref}\n%%EOF\n".encode()
        )
        return out.getvalue()

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_bytes(self.tobytes())
        return path


def _stream(dictionary: bytes, data: bytes) -> bytes:
    return dictionary + b"\nstream\n" + data + b"\nendstream"


def _font_dictionary() -> bytes:
    widths = " ".join(
        str(HELVETICA_WIDTHS.get(bytes([code]).decode("cp1252", "replace"), 0))
        for code in range(32, 256)
    )
    return (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
        b" /Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255"
        b" /Widths [" + widths.encode() + b"] /FontDescriptor 4 0 R >>"
    )


def scan_image(
    width: float,
    height: float,
    *,
    dpi: int,
    lines,
) -> Image.Image:
    """A grayscale raster of a ``width`` by ``height`` point page at ``dpi``.

    ``lines`` are ``((x, y), text, fontsize)`` triples in the same top-left
    point convention as ``PageFixture.text``; the text is rasterised with
    Pillow's bundled scalable face so a scanned fixture needs no system font.
    The result is what an OCR engine sees, and the fixture embeds it exactly.
    """

    scale = dpi / 72
    image = Image.new("L", (round(width * scale), round(height * scale)), 255)
    draw = ImageDraw.Draw(image)
    for (x, y), text, fontsize in lines:
        font = ImageFont.load_default(size=fontsize * scale)
        draw.text((x * scale, y * scale), text, fill=0, font=font, anchor="ls")
    return image
