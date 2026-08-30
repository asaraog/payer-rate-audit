"""One-off generator for the synthetic 835 fixtures. Not used by the tests."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent / "era"


def isa(element: str, composite: str, segment: str) -> str:
    fields = [
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        "SYNTHPAYER     ",
        "ZZ",
        "SYNTHPRACTICE  ",
        "260115",
        "1200",
        "^",
        "00501",
        "000000001",
        "0",
        "P",
        composite,
    ]
    line = element.join(fields)
    assert len(line) + 1 == 106, len(line) + 1
    return line + segment


def build(name: str, body: list[list[str]], element="*", composite=":", segment="~") -> None:
    text = isa(element, composite, segment)
    text += segment.join(element.join(seg) for seg in body) + segment
    if segment != "\n":
        text = text.replace(segment, segment + "\n")
    (HERE / name).write_text(text, encoding="utf-8")


def envelope(inner: list[list[str]], payer="Alpha Health") -> list[list[str]]:
    return [
        ["GS", "HP", "SYNTHPAYER", "SYNTHPRACTICE", "20260115", "1200", "1", "X", "005010X221A1"],
        ["ST", "835", "0001"],
        ["BPR", "I", "500.00", "C", "ACH", "", "", "", "", "", "", "", "", "", "", "", "20260115"],
        ["TRN", "1", "SYNTH0001", "1999999999"],
        ["N1", "PR", payer],
        ["N1", "PE", "SYNTHETIC CLINIC", "XX", "0000000001"],
        *inner,
        ["SE", str(len(inner) + 6), "0001"],
        ["GE", "1", "1"],
        ["IEA", "1", "000000001"],
    ]


def claim(
    account: str, last: str, first: str, lines: list[list[str]], paid: str
) -> list[list[str]]:
    return [
        ["CLP", account, "1", "500.00", paid, "0.00", "12", "SYNTHCLAIM1", "11"],
        ["NM1", "QC", "1", last, first, "", "", "", "MI", "SYNTHMEMBER1"],
        ["DTM", "232", "20260112"],
        *lines,
    ]


build(
    "clean_single_claim.835",
    envelope(
        claim(
            "ACCT-10001",
            "DOEPATIENT",
            "JOHNPATIENT",
            [
                ["SVC", "HC:99213", "95.00", "120.00", "", "1"],
                ["DTM", "472", "20260112"],
                ["CAS", "CO", "45", "25.00"],
                ["SVC", "HC:70551", "410.00", "600.00", "", "1"],
                ["DTM", "472", "20260112"],
            ],
            "505.00",
        )
    ),
)

build(
    "multi_claim_modifiers.835",
    envelope(
        [
            *claim(
                "ACCT-20001",
                "ROEPATIENT",
                "JANEPATIENT",
                [
                    ["SVC", "HC:70551:26", "150.00", "220.00", "", "1"],
                    ["DTM", "472", "20260113"],
                    # SVC05 absent: units default to 1, never to 0.
                    ["SVC", "HC:99213:GP", "88.00", "120.00"],
                    ["DTM", "472", "20260113"],
                ],
                "238.00",
            ),
            *claim(
                "ACCT-20002",
                "POEPATIENT",
                "SAMPATIENT",
                [
                    ["SVC", "HC:45378", "425.00", "700.00", "", "2"],
                    ["DTM", "472", "20260114"],
                ],
                "425.00",
            ),
        ],
        payer="Beta Mutual",
    ),
)

build(
    "reversal.835",
    envelope(
        [
            *claim(
                "ACCT-30001",
                "MOEPATIENT",
                "ALPATIENT",
                [["SVC", "HC:99213", "95.00", "120.00", "", "1"], ["DTM", "472", "20260112"]],
                "95.00",
            ),
            # Take-back of the line above: negative dollars, positive units.
            *claim(
                "ACCT-30001",
                "MOEPATIENT",
                "ALPATIENT",
                [["SVC", "HC:99213", "-95.00", "-120.00", "", "1"], ["DTM", "472", "20260112"]],
                "-95.00",
            ),
        ]
    ),
)

build(
    "non_hc_qualifier.835",
    envelope(
        claim(
            "ACCT-40001",
            "NOEPATIENT",
            "KIMPATIENT",
            [
                ["SVC", "NU:0450", "300.00", "450.00", "", "1"],
                ["DTM", "472", "20260115"],
                ["SVC", "HC:99213", "95.00", "120.00", "", "1"],
                ["DTM", "472", "20260115"],
            ],
            "395.00",
        )
    ),
)

# Same content as the clean file, written with the delimiters a different
# clearinghouse uses. Both must parse identically.
build(
    "alt_delimiters.edi",
    envelope(
        claim(
            "ACCT-50001",
            "ZOEPATIENT",
            "LEEPATIENT",
            [["SVC", "HC>99213", "95.00", "120.00", "", "3"], ["DTM", "472", "20260112"]],
            "285.00",
        )
    ),
    element="|",
    composite=">",
    segment="\n",
)
